/**
 * ebook_gas_sync.gs — 이북 모바일 검수 동기화 릴레이 (Google Apps Script)
 * =====================================================================
 * 역할: "멍청한 릴레이" — PC가 올린 검수 페이지(edit.html)를 Drive에 저장해
 * 폰에 서빙하고, 폰이 보낸 수정분을 큐(JSON)에 병합해 PC가 가져가게 한다.
 * UI·데이터 로직은 전부 ebook_translate.py 쪽 단일 소스.
 *
 * ── 1회 설정 절차 ────────────────────────────────────────────────
 *  1) https://script.google.com → 새 프로젝트 → 이 파일 내용 붙여넣기
 *  2) 아래 SECRET 값을 직접 정한 비밀키로 변경 (긴 임의 문자열 권장)
 *  3) 배포 > 새 배포 > 유형 "웹 앱"
 *       - 실행 계정: 나
 *       - 액세스 권한: 링크가 있는 모든 사용자
 *  4) 배포 URL(…/exec)과 SECRET을 이북 앱 [모바일 동기화] 설정에 입력
 *  5) PC에서 [☁ 업로드] 후, 폰 북마크:
 *       <배포 URL>?key=<SECRET>            → 검수 목록(서재)
 *       <배포 URL>?book=<책제목>&key=<SECRET> → 특정 책 바로 열기
 *  ※ 코드 수정 시 "배포 관리 → 버전 수정"으로 재배포해야 반영된다.
 *
 * 저장 위치: 내 Drive / ebook_review_sync /
 *   {책제목}.html        — 검수 페이지 스냅샷 (PC가 업로드)
 *   {책제목}_edits.json  — 수정 큐 {v,fp,count,edits:{i:{src?,text?,ts}},
 *                          snap_fp,snap_count,state}
 *
 * op 목록 (doPost / google.script.run apiCall):
 *   upload : 스냅샷 교체 (snap_fp/snap_count 갱신, 큐는 유지)
 *   edits  : 수정분 병합 (같은 문단은 last-write-wins,
 *            다른 fp 기준 큐가 남아 있으면 새 fp 기준으로 교체)
 *   get    : 수정 큐 반환 (PC [☁ 모바일 수정 반영])
 *   clear  : 반영 완료 후 큐 비움 (fp 일치할 때만)
 *   state  : 읽던 위치 저장 (뷰어 확장 예약)
 *   icon   : 홈 화면 아이콘 업로드 (PC가 1회 전송 → 공개 링크로 파비콘)
 *   list   : 책 목록+진행 정보 {name,count,pos,pending} (PWA 셸 서재용)
 */

var SECRET = 'CHANGE_ME';               // ★ 반드시 직접 정한 값으로 변경
var FOLDER = 'ebook_review_sync';

// ---------------------------------------------------------------- 유틸
function _san(b) {
  return String(b || '').replace(/[\\\/:*?"<>|]/g, '_').trim();
}

var _FCACHE = null;                      // 실행 1회만 폴더 조회(왕복 절감)
function _folder() {
  if (_FCACHE) return _FCACHE;
  var it = DriveApp.getFoldersByName(FOLDER);
  _FCACHE = it.hasNext() ? it.next() : DriveApp.createFolder(FOLDER);
  return _FCACHE;
}

function _bust() {                       // 목록 캐시 무효화(쓰기 시 호출)
  try { CacheService.getScriptCache().remove('list'); } catch (e) {}
}

function _find(name) {
  var it = _folder().getFilesByName(name);
  return it.hasNext() ? it.next() : null;
}

function _write(name, text) {
  var f = _find(name);
  if (f) f.setContent(text);
  else _folder().createFile(name, text, 'text/html');
}

function _readq(book) {
  var f = _find(book + '_edits.json');
  var d = {v: 1, fp: null, count: null, edits: {},
           snap_fp: null, snap_count: null, state: null, hi: null};
  if (!f) return d;
  try {
    var q = JSON.parse(f.getBlob().getDataAsString('UTF-8'));
    for (var k in d) if (q[k] !== undefined) d[k] = q[k];
  } catch (e) {}
  return d;
}

function _writeq(book, q) {
  _write(book + '_edits.json', JSON.stringify(q));
}

function _writeIcon(b64) {
  var f = _find('icon.png');
  if (f) f.setTrashed(true);            // 바이너리는 setContent 불가 — 재생성
  var nf = _folder().createFile(
    Utilities.newBlob(Utilities.base64Decode(b64), 'image/png', 'icon.png'));
  nf.setSharing(DriveApp.Access.ANYONE_WITH_LINK, DriveApp.Permission.VIEW);
}

function _app(out) {                     // 서빙 페이지 공통 마감
  var tags = [['mobile-web-app-capable', 'yes'],
              ['apple-mobile-web-app-capable', 'yes'],
              ['apple-mobile-web-app-status-bar-style', 'default']];
  for (var i = 0; i < tags.length; i++) {
    try { out.addMetaTag(tags[i][0], tags[i][1]); } catch (e) {}
  }
  try {                                  // PWA 셸(iframe)이 품을 수 있게
    out.setXFrameOptionsMode(HtmlService.XFrameOptionsMode.ALLOWALL);
  } catch (e) {}
  try {           // 키보드가 화면을 밀지 않고 줄이게 — 편집 중 헤더 유지
    out.addMetaTag('viewport', 'width=device-width, initial-scale=1, '
                   + 'interactive-widget=resizes-content');
  } catch (e) {}
  return _fav(out);
}

function _fav(out) {                     // 홈 화면 추가용 파비콘 적용
  var f = _find('icon.png');
  if (!f) return out;
  var urls = ['https://lh3.googleusercontent.com/d/' + f.getId(),
              'https://drive.google.com/uc?export=view&id=' + f.getId()
              + '&x=.png'];
  for (var i = 0; i < urls.length; i++) {
    try { return out.setFaviconUrl(urls[i]); } catch (e) {}
  }
  return out;
}

function _json(o) {
  return ContentService.createTextOutput(JSON.stringify(o))
    .setMimeType(ContentService.MimeType.JSON);
}

// ---------------------------------------------------------------- 핵심
function handleOp(q) {
  if (!q || q.key !== SECRET) return {err: '인증 실패 — 동기화 키 불일치'};
  if (q.op === 'list') {                 // book 불필요 — 서재 목록
    var cache = null;
    try { cache = CacheService.getScriptCache(); } catch (e) {}
    if (cache) {                         // 45초 캐시 — 콜드스타트 외 재조회 즉시
      var hit = cache.get('list');
      if (hit) { try { return JSON.parse(hit); } catch (e) {} }
    }
    var names = [], lfs = _folder().getFiles();
    while (lfs.hasNext()) {
      var lnm = lfs.next().getName();
      if (lnm.slice(-5) === '.html') names.push(lnm.slice(0, -5));
    }
    names.sort();
    var info = [];                       // 책별 진행 정보 (서재 표시용)
    for (var li = 0; li < names.length; li++) {
      var e2 = _readq(names[li]);
      info.push({name: names[li], count: e2.snap_count,
                 pos: (e2.state && e2.state.pos != null)
                      ? e2.state.pos : null,
                 pending: Object.keys(e2.edits || {}).length});
    }
    var res = {ok: true, books: names, info: info};
    if (cache) { try { cache.put('list', JSON.stringify(res), 45); } catch (e) {} }
    return res;
  }
  var book = _san(q.book);
  if (!book) return {err: 'book(책 제목) 누락'};

  if (q.op === 'upload') {
    _write(book + '.html', q.html || '');
    var e = _readq(book);
    e.snap_fp = (q.snap_fp === undefined) ? null : q.snap_fp;
    e.snap_count = (q.snap_count === undefined) ? null : q.snap_count;
    if (q.clear_edits) { e.edits = {}; e.fp = null; e.count = null; }
    _writeq(book, e); _bust();
    return {ok: true, icon: !!_find('icon.png')};
  }
  if (q.op === 'edits') {
    var e = _readq(book);
    if (e.fp !== null && e.fp !== q.fp) e.edits = {};  // 구버전 큐 교체
    e.fp = q.fp; e.count = q.count;
    var ed = q.edits || {};
    for (var k in ed) e.edits[k] = ed[k];              // last-write-wins
    e.ts = Date.now();
    _writeq(book, e); _bust();
    return {ok: true, n: Object.keys(e.edits).length, snap: e.snap_fp};
  }
  if (q.op === 'get') return _readq(book);
  if (q.op === 'clear') {
    var e = _readq(book);
    if (e.fp !== null && q.fp !== e.fp)
      return {err: 'fp 불일치 — 큐가 다른 버전 기준입니다'};
    e.edits = {}; e.fp = null; e.count = null;
    _writeq(book, e); _bust();
    return {ok: true};
  }
  if (q.op === 'state') {
    var e = _readq(book);
    e.state = {pos: q.pos, ts: Date.now()};
    _writeq(book, e); _bust();
    return {ok: true};
  }
  if (q.op === 'icon') {
    if (!q.png) return {err: 'png(base64) 누락'};
    _writeIcon(q.png);
    return {ok: true};
  }
  if (q.op === 'hi') {                    // 하이라이트 공유 저장/조회
    var e = _readq(book);
    if (q.set !== undefined) { e.hi = q.set; _writeq(book, e); }
    return {ok: true, hi: e.hi};          // null=기록없음(최초), {}=비운 것
  }
  return {err: '알 수 없는 op: ' + q.op};
}

// 검수 페이지 안 JS가 google.script.run.apiCall(JSON문자열)로 호출
function apiCall(s) {
  var q = {};
  try { q = JSON.parse(s); } catch (e) {}
  return JSON.stringify(handleOp(q));
}

// ---------------------------------------------------------------- HTTP
function doPost(e) {
  var q = {};
  try { q = JSON.parse(e.postData.contents); } catch (err) {}
  return _json(handleOp(q));
}

function doGet(e) {
  var p = (e && e.parameter) || {};
  if (p.key !== SECRET)
    return ContentService.createTextOutput(
      '인증 실패 — URL 뒤에 &key=동기화키 를 붙이세요');
  if (p.api === 'ping') return _json({ok: true});
  if (p.api === 'edits') return _json(_readq(_san(p.book)));

  if (p.book) {                          // 검수 페이지 서빙
    var f = _find(_san(p.book) + '.html');
    if (!f)
      return ContentService.createTextOutput(
        '스냅샷이 없습니다 — PC 이북 앱에서 [☁ 업로드]를 먼저 하세요');
    var html = f.getBlob().getDataAsString('UTF-8')
      .split('__GASURL__').join(ScriptApp.getService().getUrl())
      .split('__GASKEY__').join(p.key);
    return _app(HtmlService.createHtmlOutput(html)
      .setTitle(_san(p.book) + ' — 검수')
      .addMetaTag('viewport', 'width=device-width, initial-scale=1'));
  }

  // book 없음 → 검수 목록 (서재 자리 — 나중에 카탈로그로 확장)
  var url = ScriptApp.getService().getUrl();
  var out = ['<!doctype html><html lang="ko"><head><meta charset="utf-8">',
             '<meta name="viewport" content="width=device-width, ',
             'initial-scale=1"><title>이북 검수 목록</title></head>',
             '<body style="font-family:sans-serif;padding:24px 20px;',
             'font-size:17px;line-height:2.2"><h3>📚 검수 목록</h3>'];
  var fs = _folder().getFiles(), n = 0;
  while (fs.hasNext()) {
    var f2 = fs.next(), nm = f2.getName();
    if (nm.slice(-5) !== '.html') continue;
    n++;
    var t = nm.slice(0, -5);
    out.push('<p><a target="_top" href="' + url + '?book='
             + encodeURIComponent(t) + '&key=' + encodeURIComponent(p.key)
             + '">' + t + '</a></p>');
  }
  if (!n) out.push('<p>아직 업로드된 책이 없습니다 — PC에서 [☁ 업로드]</p>');
  out.push('</body></html>');
  return _app(HtmlService.createHtmlOutput(out.join(''))
    .setTitle('이북 검수 목록')
    .addMetaTag('viewport', 'width=device-width, initial-scale=1'));
}
