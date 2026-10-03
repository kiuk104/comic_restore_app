# Fokus Viewer 작업 가이드 (인수인계) — 2026-10-02

> 이 문서는 2026-10-02 오전 세션(Fable)에서 결정·구현한 내용과 **다음 세션이 이어서 할 일**을 적은 것이다.
> 갱신: 같은 날 Opus 세션이 **3단계(하이라이트)·2.5단계(문단 합치기·나누기)** 를 구현하고 기록을 덧붙였다(병행 세션 기록 병합본).
> 저장소: `E:\Coding\comic_restore_app`. 프로젝트 메모리 `library-roadmap`·`fokus-family`에도 같은 결정이 있다.

---

## 0. 한 줄 요약

- **eBook_viewer(E:\Coding\eBook_viewer)는 리뉴얼하지 않고 은퇴.** 역할을 검수 뷰어 스택이 흡수한다.
- 뷰어의 새 이름은 **Fokus Viewer**(짧게 Viewer) = `ebook_shell/`(폰 PWA, 라이브러리 앱으로 승격 예정) + `edit_ui.html`(읽기·교정 엔진).
- 레이아웃 재구성은 뷰어가 아니라 **PC 가져오기 공정**(`ebook_import.py`: txt/md → book.json)이 맡는다.
- 사용자 동의한 순서: ①가져오기 툴 ✅ ②edit_ui 평문 책 대응 ✅ ②.5 문단 합치기·나누기 ✅ ③하이라이트 승격 ✅ ④셸 → 라이브러리 승격 ✅(GAS 확장·카탈로그·가져오기 큐 — OAuth 대신, 사용자 결정) ⑤eBook_viewer 아카이브 ✅(10-03, 자동 시작 끄기는 사용자 PC에서 스크립트 실행).

## 1. 왜 이렇게 결정했나 (근거)

| eBook_viewer가 하려던 것 | 검수 뷰어 스택 |
|---|---|
| 교정 편집 동기화 | ✅ edits.json·fp 게이트·book.json 단일 기준, PC↔폰 왕복 |
| 데스크탑+모바일 접속 | ✅ PC edit 서버 + PWA 셸 |
| 문단 정렬·교정 | ✅ 양쪽정렬+하이픈·자간 튜닝·문장 싱크 |
| 레이아웃 재구성 | ⬜ → `ebook_import.py` (오늘 구현) |
| Drive 라이브러리 관리 | ✅ 4단계 — PC 카탈로그 + GAS 릴레이 + 폰 서고 탭·가져오기 요청 (OAuth 불필요) |
| 하이라이트 | ✅ 5색·book.json 단일 기준·PC↔폰 동기화 (3단계) |

사용자 확인 사항: eBook_viewer는 실사용 안 함(모바일 접근 어려움), 핵심 의도는 "기존 라이브러리 레이아웃 재구성 + Drive 관리"였으나 교정·문단 정렬이 의도대로 안 됐음. **하이라이트는 필요**.

## 2. 오늘 바뀐 파일

### 이름·아이콘 (Fokus 패밀리 통일)
- `ebook_shell/manifest.json` — name **Fokus Viewer** / short_name **Viewer**, theme_color `#2e2140`, maskable 아이콘 추가
- `ebook_shell/index.html` — 제목·헤더·설정 화면 "Fokus Viewer", `--deep:#2e2140`, `--acc:#bf8729`(녹색→골드), favicon.svg·apple-touch-icon 링크
- `ebook_shell/` 아이콘 5종 교체·추가: icon-192 · icon-512 · icon-maskable-512 · apple-touch-icon · favicon.svg
- `ebook_mobile_icon.png` — GAS 직접 열기용 홈 아이콘(192)
- `fokus_viewer.ico` — PC용 예비(아직 어디에도 연결 안 함)
- `edit_ui.html` `<title>` → "책이름 — Fokus Viewer"
- `ebook_translate_web.py` — 아이콘 재전송: 서버에 없거나 **sha 마커(`ebook_mobile_icon.sent`)와 다르면** 다시 올림
- `ebook_shell/README.md` — 파일 8개 목록·아이콘 규칙

**아이콘 규칙(패밀리 2세대)**: 512 라운드(rx 96) + 크림 F(`#f3ede2`, 경로 `M 69 103 H 295 V 172 H 161 V 218 H 275 V 287 H 161 V 399 H 69 Z`) + **오른쪽에 골드(`#d4a843`) 픽토그램**(중심 ≈ 378,305). Lesen=말풍선, Alltag=해, **Viewer=펼친 책**(두 페이지, 표지 외곽선 없음, 오른쪽 페이지에만 글줄 3개, 아래로 14px 연장). 1세대(DE·Karten)는 골드 막대+F 안 작은 글자 — 신규 앱은 2세대로. 원본 SVG 생성 스크립트는 세션 스크래치에만 있었으므로 `favicon.svg`가 원본.

### 1단계 — 가져오기 툴
- **`ebook_import.py`** (새 모듈, 핵심 py에 안 넣음)
  - `read_text_any`(BOM→utf-8→문자분포 점수) · `strip_html` · `aozora_clean`(루비 strip/paren/keep, ［＃］주석, 見出し→`## `) · `analyze`/`reflow`(auto|wrap|lines) · `mark_headings`(제N장·第N章·Chapter·프롤로그…) · `import_text`
  - 출력: `<파일 폴더>/<제목>_book/_work/book.json` + `xlat.json`(빈 {}) + `<제목>_src.txt`
  - **재가져오기 시 `bmks/pos/off/pos_ts/cover/hi` 보존**
  - **재가져오기 시 `hi_ts`도 보존** (3단계)
  - CLI: `python ebook_import.py <txt> [--title] [--out] [--split auto|wrap|lines|blank] [--ruby strip|paren|keep] [--preview N]` — `_src.txt`를 주면 자동 blank 모드·원래 `_book` 폴더(왕복)
- `ebook_translate.py`
  - `resolve_out`: 소스가 `.txt/.md/.markdown` 파일이면 `_book` 폴더 (`PLAIN_EXTS`, `is_plain_src`)
  - `edit_data`에 `"kind": book.get("kind") or "xlat"` 추가
- `ebook_translate_web.py`
  - `Api.import_text(cfg, path="")` — 파일 대화상자(txt/md) → 가져오기 → 폼 src/title/source_lang 갱신 → 최근 작업 등록 → `edit_url` 리셋
  - 버튼 **[📚 텍스트 책 가져오기]** (`b_import`, JS `doImportBook()`) — [📝 편집 페이지] 옆. ※ `doImport()`는 기존 [📥 파일 반영](폰 JSON)의 함수라 **이름 충돌 주의**(오늘 한 번 당함)
  - `start()` 가드: txt 소스로 [▶ 번역 시작] 누르면 안내 후 중단
  - 가져오기 후 `c_mode`를 "book"으로 강제(문서/성경 모드였어도)

### 2단계 — edit_ui 평문 책 대응
- `const PLAIN=D.kind==='plain'` → `body.plain`
- 숨김: 번역 textarea·라벨(`.fldl-t/.fldl-o`), "번역 실패" 배지·`fail` 클래스, 원문 토글(`_srcBtn`)·미리보기(`hpv`), 재번역 버튼, 페이지 머리글(`.pgh`), 헤더 "실패 N"
- `nosrc`/`rdsrc` 강제 해제, 본문 textarea 색 `var(--fg)`/`var(--card)`, `.rd.untr` 색 inherit
- 기본 모드: 평문은 데스크톱도 읽기 모드
- 검증: 클라우드 컨테이너 Playwright — 690문단, fail 0, badge 0, 본문 textarea만 표시

## 3. 데이터 계약 (중요)

- 평문 책 = `book.json {title, source_lang, kind:"plain", page_labels:[], paras:[{src,page:""}], origin:{file,encoding,split,p95,term,trail}}`
- **본문은 `src` 슬롯**, `xlat`은 비움. 이유: `book_fingerprint`(src crc)·`_edit_save`의 src.txt 동기화·`export_outputs`(`done.get(i) or paras[i]`)·읽기 모드 폴백(`번역||원문`)이 전부 그대로 동작.
- 폰 수정분은 `edits:{i:{src}}`로 들어와 `_edit_save` → book.json. fp는 src가 바뀌면 바뀜(번역책의 src 수정과 동일 경로).
- 셸·GAS·`_push_c`는 변경 없음 — `edit_data` 스냅샷에 `kind`가 실리므로 폰에서도 PLAIN 동작.

## 3-1. 평문 책 교정 방법 (사용자 안내용)

| 고칠 것 | 방법 |
|---|---|
| 오타·문장 | 읽기 모드에서 문단 탭 → 본문 칸 수정 → PC 💾 / 폰 자동 저장 → [☁ 수정 반영] |
| 장 제목 | 편집 카드 **[§ 제목]** 토글 (`## ` 마커, EPUB 목차·장 분할 기준). 자동 감지가 틀렸으면 같은 버튼으로 해제 |
| 소제목 | **[§ 제목]**을 한 번 더 → `### ` 소제목(목차 2단, 장은 안 나눔). 많으면 필터 「소제목 후보 찾기」에서 확인 후 지정. 「목차」로 이동 |
| 책 제목 | 가져오기 전 웹앱 "책 제목" 칸. 이후엔 `_book` 폴더 삭제 후 재가져오기(북마크 없을 때) |
| 문단 합치기·나누기 (몇 군데) | **PC 편집 페이지** (여러 문단은 읽기 모드에서 드래그 선택 → [⤵ 문단 N개 합치기]) 편집 카드 **[⤵ 합치기]**(다음 문단을 이어 붙임) / **[✂ 나누기]**(본문 칸 커서 위치에서). 합치면 띄어쓰기 하나로 이어지고 그 이음새에 커서가 놓임 — 잘린 단어("하"+"지만")면 ⌫ 한 번 → 💾. 그 다음 [☁ 업로드]. ※ 폰에 반영 대기 수정이 있으면 **먼저 [☁ 수정 반영]** (구조를 바꾸면 fp가 바뀌어 옛 수정 큐는 거부됨) |
| 문단 합치기·나누기 (대량) | **`_src.txt` 왕복**: `<제목>_book/<제목>_src.txt`(빈 줄 = 문단 경계)를 편집기에서 고친 뒤 `python ebook_import.py "<…>_src.txt"` → 자동으로 `blank` 모드·같은 폴더·북마크/위치/표지 보존·`origin.file` 유지(`origin.resplit` 기록). 그 다음 [📝 편집 페이지](재생성) → [☁ 업로드]. ※ 이 경로는 문단 번호 보정을 하지 않음 — 북마크·하이라이트가 어긋날 수 있음 |
| 잘린 문단·제목·PC통신 머리 (자동) | 가져오기 창 「자동 교정」(규칙/규칙+AI). 이미 가져온 책은 편집 페이지 필터 「자동 교정 기록」 → 다시 검사. 잘못 고친 곳은 [되돌리기] (다시 검사해도 그곳은 건너뜀) |
| 자동 판별이 틀림(전부 붙었거나 전부 쪼개짐) | 웹앱 [📚 텍스트 책 가져오기] 창에서 같은 원본을 고르고 **줄바꿈 교정** 방식을 바꿔 미리보기 확인 후 다시 가져오기 (이전 book.json은 `_work/reimport_…` 사본). CLI는 `--split wrap/lines` |

### 2.5단계 — 편집 카드 문단 [⤵ 합치기] / [✂ 나누기] ✅ (Opus 세션)
- (원 설계 메모) 번역책은 전사 단계에서 문단이 정해져 UI에 이 기능이 없었음. 평문 책에선 필수. 문단 수가 바뀌므로 **fp가 바뀜** → 폰 수정 큐와 충돌 → PC(SRV)에서만 허용, 폰(CLOUD)에서는 버튼 숨김.
- **core**(`ebook_translate.py`): `restructure_book(book, done, op, i, off)`(순수 함수) + `_edit_restructure` + 라우트 **`/api/restructure {op:'merge'|'split', i, off}`** → book.json·xlat.json·`_src.txt` 재작성 → `write_edit_html`.
  - merge: `a.rstrip() + 이음 + b.lstrip()`(b의 `## ` 마커 제거). 이음 = 공백 1칸, 단 a 끝·b 앞이 둘 다 한자·가나·전각부호(`_cjk_tight`, 한글 제외)면 붙임. 번역 키 i+1 이후 -1(둘 다 있으면 번역도 공백으로 이음). page_end 보존. 응답에 `caret`(이음새 뒤 커서 위치).
  - split: 원문 off에서 자르고 양쪽 공백 정리, 앞/뒤가 비면 오류. 페이지 걸침(page_end)은 뒤 문단이 끝 페이지를 가짐. 번역은 앞 문단에 남김(평문 책 전용이라 실제로 비어 있음).
  - 보정: **bmks**(합치면 i+1→i·뒤 -1, 나누면 뒤 +1) · **pos/off**(합쳐진 문단 안이면 off += 앞 문단 표시 길이, 나눈 지점 뒤면 다음 문단으로) · **hi**(표시 좌표 — `_disp_text`가 edit_ui `stripMark`+`fmtHtml`의 글자를 흉내: `##`·`**`·`~~`·`++`·`*`·`>>`·줄바꿈 제거; 나눔 지점에 걸친 범위는 둘로).
  - `struct_ts`(ms) 기록 + `hi_ts`=지금. 재전사(rescan)도 문단 수가 바뀌면 `struct_ts`.
- **폰 표식 보호**(웹앱 `ebook_translate_web.py`): GAS의 북마크·하이라이트·위치는 옛 번호라 — `_pull_marks`는 `struct_ts > struct_sent`이면 **회수 보류**(로그 안내), `_push_c`(업로드)는 그때 GAS `op:'bmk'/'hi'/'state'`를 **PC 값으로 덮어쓰고** `core.mark_struct_sent`. GAS 변경 없음.
- **edit_ui**: `SRV&&PLAIN`일 때만 카드에 `.edmerge`(마지막 문단 제외)·`.edsplit`(mousedown preventDefault로 본문 칸 커서 유지). `restruct()` = 커서 검사 → `doSave` → **`marksFlush()`**(디바운스 대기 중 hi/bmk/pos 저장을 순서대로 즉시 전송 — 편집 서버는 동시 요청 409) → `window._marksLock=true`(새로고침까지 `/api/marks`·`_bmkSet`·`posFlush` 차단 — 안 막으면 옛 번호 저장이 구조 변경 **뒤에** 도착해 덮어씀, 실제로 재현됨) → 요청 → `sessionStorage edFocus/edScroll` → reload → 450ms 뒤 해당 카드 열고 커서.
- **검증**: `restructure_book` 단위(합치기→나누기 왕복 원복, 걸친 하이라이트 분할, CJK 이음, 오류 3종) / 웹앱 단위(보류→업로드 덮어쓰기→재개) / 클라우드 컨테이너에서 **실제 편집 서버**(`run_edit_server`, `comic_retype_pipeline` 스텁) + Playwright: 합치기→⌫→나누기(자동 저장 후)→하이라이트·북마크·src.txt 확인.

## 4. 서고(라이브러리) 실태와 재구성 규칙

- 위치: PC `C:\Users\kiuk1\내 드라이브\_Ebook_Library` (Drive 스트리밍). **txt 12,004권 2.8GB** + epub 78 · mobi 69. 2006년판 한국 txt 아카이브(README 2006-12-29) + `[영어, 일본어, 중국어]`.
- **전부 UTF-8 BOM**으로 재인코딩돼 있음(인코딩 판별 코드는 안전망).
- 두 부류: 하드 줄바꿈(한국 책 대부분, 줄 ~40자, 문장종결 줄 12~40%) / 한 줄=한 문단(번역 라노벨·중국어·青空文庫, p95>120 또는 종결 ≥72%).
- wrap 규칙(실측 확정): 새 문단 = 들여쓰기가 **본문 이어쓰기 들여쓰기**(8% 이상 나타나는 들여쓰기 중 최솟값 — 최빈값을 쓰면 '우동 한그릇'처럼 전부 붙음)보다 깊은 줄 / 빈 줄 뒤 / 직전 줄이 p95의 60% 미만. **줄 끝 공백 = 단어 경계 신호**(공백 없이 끝나면 "혹평했다. 하"+"지만" → 붙여 잇기; 파일에 줄 끝 공백이 15% 미만이면 문자 종류로 판단: CJK끼리 붙이고 나머지 띄움). 이중 공백은 하나로.
- 검증한 샘플: Jude1(비운의 주드)·우동 한그릇·mr2(거울의길)·돈의 역사(목차)·에메랄드(lines)·芃羽(중국어, 第一章)·Alice(Gutenberg)·앵무새 죽이기(6,357문단·제목 9).

## 5. 지금 열려 있는 이슈 (다음 세션이 먼저 볼 것)

1. ~~GAS 동기화 설정이 비어 있음~~ → **10:29 사용자가 재입력, 모바일 동기화 성공 확인.** (기록용) — `ebook_config.json`의 `gas_url`/`gas_key`가 "" → [☁ 업로드] 실패("동기화 URL을 설정하세요"). 로컬 어디에도 값이 없음(`.gs`의 SECRET은 'CHANGE_ME' 플레이스홀더). 복구: Apps Script 편집기 → 배포 관리의 웹 앱 URL + 온라인 코드의 `var SECRET`. 폰 셸 localStorage(`ebookShellCfg`)에도 있음 — 셸 ⚙ 설정 화면이 저장된 URL·키를 미리 채우고 👁로 키를 보이게 수정함(10:20, Pages 교체 후부터 폰에서 읽어 PC에 옮길 수 있음). 확인 결과 오늘 변경분에는 gas 필드를 지우는 경로가 없음(`_default_cfg`→`fillForm`→`save`/`_SAVE_KEYS` 모두 보존) → 앱 시작 전에 이미 비어 있었음(9/24 병렬 세션 복구 때 초기화 의심). 언제 비었는지는 불명 — 오늘 06:09Z에 ATLAS edits.json이 갱신됐으므로 아침까지는 어떤 앱이 동기화를 했음.
0. ~~GAS 재배포 필요(4단계)~~ → **12:5x 재배포됨(카탈로그 ver 9a05c5da3616 업로드 확인)**. 기록: — `ebook_gas_sync.gs`에 `catalog`·`importq` op와 putdata `kind/src`가 추가됨. Apps Script 편집기에 최신본 붙여넣기(**SECRET 줄은 기존 값 유지**) → 배포 관리 → ✏ → **새 버전**(URL 유지). 재배포 전에는 PC 로그에 "서고 기능은 재배포해야 동작" 1회 안내 후 10분 간격으로만 재확인(기존 업로드·수정 반영은 영향 없음).
2. ~~Pages 저장소 미반영~~ → **12:59 Claude가 `kiuk104/ebook-shell`에 푸시(61af585), 배포 확인.** 셸 8개 파일 교체·추가, `fonts/`·`fonts.css`(edit_ui가 링크하는 읽기 폰트)는 유지. 폰: 홈 화면 아이콘 삭제→재추가해야 새 이름·아이콘이 적용됨(설치 시점에 굳음). 이후 셸 수정도 이 저장소에 푸시하면 됨(세션에 저장소 추가 필요).
3. ~~`_book` 폴더 위치 결정 대기~~ → **사용자 결정(11:3x): `_Ebook_Library/_books/<제목>_book`** — `core.plain_book_dir` 한 곳에서 결정(서고 밖 원본은 옆, 예전 위치에 이미 있으면 그대로).
4. **git 미커밋** — 오늘 변경분(Fable·Opus 두 세션) + **다른 세션 변경분**(`comic_restore_app.py`, `comic_restore_web.py`, `comic_retype_pipeline.py`, bible_*.bat/py 등)이 함께 워킹트리에 있음. 프로젝트 preferences 규칙대로 **사용자 확인 후** 커밋하고, 커밋 전 git status로 남의 변경 확인.
5. 테스트 산출물 `_to_delete/fv_test/edit_plain.html`·`edit_hi.html` — 지워도 됨. 수정 전 백업 `_to_delete/hi_backup/`(3·2.5단계 직전 사본).
6. `fokus_viewer.ico` → 저장소의 **`Fokus Viewer.url`**(Pages 셸 바로가기, 아이콘 연결)을 바탕화면에 복사해 쓰면 됨. Pages 주소가 `https://kiuk104.github.io/ebook-shell/`가 아니면 파일 안 URL 수정.
7. **병행 세션 주의(실제 발생)**: 10:3x 다른 세션이 `ebook_import.py`·이 문서를 통째로 다시 써서 3단계의 `hi_ts` 보존 줄과 기록이 사라졌다가 복구됨. 같은 파일을 만지는 세션이 둘이면 쓰기 직전에 다시 읽고(read-modify-write) 고유 이름 grep으로 확인.

## 6. 다음 단계 설계 메모

### 3단계 — 하이라이트 승격 ✅ (2026-10-02 오후, Opus 세션)
- **데이터**: book.json `hi = {"문단idx": [[s,e,c],…]}` + `hi_ts`(ms). c = 색 0~4(노랑·초록·파랑·분홍·보라), 구형 `[s,e]`는 c=0. 오프셋은 **읽기 화면에 그려진 글**(번역책=번역, 평문=src) 기준.
- **core**(`ebook_translate.py`): `HI_COLORS`, `clean_hi(hi,n)`(범위·색 검증, `_` 메타키 무시), `hi_count`. `save_marks(..., hi=None, hi_ts=None)` — hi_ts 없으면 현재 시각. `/api/marks {hi,hi_ts}`. `edit_data`에 `hi`·`hi_ts`. 재전사(rescan)로 문단 수가 바뀌면 hi 키도 시프트(바뀐 블록 안은 버림). `import time` 추가.
- **웹앱** `_pull_marks`: GAS `op:'hi'` 회수 → `_ts` ≥ book.hi_ts이고 내용이 다르면 채택(구형 둘 다 0이면 폰 우선). 로그 "☁ 폰 하이라이트 N개 반영".
- **GAS 변경 없음(재배포 불필요)**: 맵에 `_ts` 키를 동봉해 그대로 보관. 구 클라이언트는 `_ts` 키를 무시(문단 키만 읽음).
- **edit_ui**: `HI`/`HI_TS` 초기값 = 스냅샷 `D.hi` 기준, 로컬 캐시가 더 최신이거나 구버전에서 이 기기에만 그린 것(서버 비어 있고 ts 없음)이면 로컬 채택 후 1회 올림(`_hiMigrate`). 저장 `hiPersist()` → SRV `/api/marks` · CLOUD `op:'hi'`(디바운스 700ms). `hiCloudPull`: 클라우드 없음→올림 / 이쪽 ts가 더 최신→올림 / 아니면 클라우드 채택.
- **UI**: 선택 시 색 동그라미 5개 팝업(마지막 색 강조, `localStorage edHiColor`) → 한 번 탭으로 칠함. 기존 하이라이트 탭 → 색 바꾸기 + [지우기]. 다른 색 위에 칠하면 그 부분만 덮어씀(`_hiCut`), 같은 색끼리 닿으면 합침(`_hiJoin`). 테마별(dark/dim/sepia) 색 톤.
- **편집 추적(2.5단계 때 추가)**: 문단 글을 고치면 `hiTrack(i)`(`mark()`에서 호출)가 편집 전/후 **화면 글**을 비교해(공통 앞·뒤 제외) 바뀐 구간 뒤 범위를 길이 차만큼 이동·걸친 범위는 조정 — `_hd` 기준은 `hiPaint`·textarea focus 때 잡음(카드 생성 시점엔 `HI`가 TDZ라 못 잡음).
- **검증**: py_compile·node --check / core·_pull_marks 단위 테스트(최신·구형·범위 밖 처리) / 클라우드 Playwright — 칠하기·색 변경·겹침 분할·지우기·POST 페이로드·리로드 시 로컬 최신 채택. ※ Playwright로 상단 문단을 클릭하면 헤더 버튼이 가려 클릭을 가로챔 — 문단을 화면 가운데로 스크롤한 뒤 좌표 클릭.
- **반영**: PC 앱 재시작 → [📝 편집 페이지]로 바로 적용(PC). 폰은 [☁ 업로드] 1회(새 UI·스냅샷 hi). (§5-1 GAS 설정은 10:29 복구됨)
- 미구현(선택): 셸 목록의 "하이라이트 N개"(op:list 확장 = gs 수정·재배포 필요), 하이라이트 모아보기 목록.

### 4단계 — 셸 → 라이브러리 앱 ✅ (Opus 세션, 2026-10-02 11:2x~)
- **사용자 결정 3가지**: ① Drive 접근 = **GAS 확장**(이미 '나'로 실행·Drive 전체 접근 — 폰 OAuth 로그인·1시간 토큰 만료 없음; google_drive.js 이식 안 함) ② 미가져온 txt = **PC 가져오기 큐** ③ `_book` 위치 = **`_Ebook_Library/_books/`**.
- **서고 실측**(전체 스캔): 텍스트 12,010 · 폴더 5,074(텍스트 있는 것만) · 최상위 10개. **여러 파일이 한 책**인 폴더가 많음(드래곤라자 324개, D&D1 142개, `k1216.txt`·`LANT1127.TXT` 같은 무의미한 파일명) → 카탈로그는 폴더 트리, **폴더 통째 가져오기** 지원.
- **`ebook_import.py`**: `import_folder`(하위 폴더 포함, 자연 정렬 k2<k10, 파일마다 따로 재구성, 첫 3문단에 제목이 없으면 `## 파일명` 장 제목 삽입) · `import_text(src가 폴더면 위임)` · `default_title`(폴더는 폴더명, 파일은 '폴더에 텍스트가 그 하나뿐 + 폴더명이 `[...]` 전체 괄호(분류 폴더)가 아니면' 폴더명, 아니면 파일명) · `list_texts`·`natural_key` · 저장부 `_save_book`로 분리. `default_out` → `core.plain_book_dir`. CLI: 폴더 인자 + `--preview N`(파일 목록).
- **core**: `LIB_NAME`·`BOOKS_DIR`·`lib_root(p)`·`plain_book_dir(src,title)`; `resolve_out`이 txt 파일·(가져온) 폴더 소스를 `plain_book_dir`로.
- **`ebook_library.py`(새)**: `find_lib_root(cfg)`(cfg `lib_root` → `~/내 드라이브|My Drive/_Ebook_Library` → G~J 드라이브) · `scan_catalog`(os.walk, `_books`·`*_book`·`*_한글번역`·`.` 제외, 빈 가지 제거, `_src.txt` 제외, `imp` = `_books/*/book.json` origin + 옛 위치 `_book`) · `catalog_json`(ver = 내용 sha1 12자, gen 제외) · `catalog_gz_b64` · `save_local`(`_books/catalog.json`) · `cat_path`. **형식 v2**: `{v,gen,root,n,ver, d:[[부모idx,이름]…], f:[[폴더idx,파일명,바이트]…], imp:{상대경로:제목}}` — 657KB → 전송 gzip+b64 **302KB**. VM 경유 스캔 44초(PC 직접은 더 빠를 것).
- **GAS**(`ebook_gas_sync.gs`, 재배포 필요): `catalog`(set=gz b64 문자열 그대로 `_catalog.gz.b64`·`_catalog.ver` 저장 / 조회는 ver 같으면 `same`) · `importq`(`_importq.json`, LockService, add는 같은 경로 대기 중이면 중복 안 함, done=PC 결과 기록, clear=끝난 항목 정리, 60개 초과 시 오래된 완료분 정리) · putdata에 `kind`·`src` 저장 → `list` info에 `kind`·`src`.
- **웹앱**: `_libq_loop`(별도 스레드, 동기화 URL만 있으면 동작 — 자동 반영 설정과 무관, 45초 주기; 시작 8초 후 1회 카탈로그 스캔·버전 다르면 업로드) · `_process_importq`(서고 밖 경로·없는 원본 거부, `_unique_title`로 같은 제목 다른 원본이면 ' (2)', import → `_push_c` → done → 카탈로그 imp 갱신 재업로드) · `lib_sync` + 버튼 **[📚 서고 올리기]**(`b_lib`, `doLibSync`) · `_push_c`가 putdata에 `kind`·서고 상대경로 `src` · `_san_title` · `_old_gas()` — 구 GAS 응답(book 누락/알 수 없는 op/JSON 아님)이면 재배포 안내 1회 + 10분 백오프(사용자 화면에서 실제로 '(서고 카탈로그 생략: book(책 제목) 누락)' 로그가 떠서 추가).
- **셸**(`ebook_shell/index.html` 14→34KB, sw.js는 캐시 없음): 홈에 탭 **[읽는 중]**(올라온 책 + 평문/번역 칩 + 정렬: 최근 연 순 `fvOpened`/제목/진행률 `fvSort`) · **[서고]**(카탈로그 `fvCat`{ver,gz} 캐시 → `DecompressionStream('gzip')` → 트리·하위 개수 누적, 경로 빵부스러기, 폴더 행·파일 행(크기, 가져옴/요청됨 칩), 폴더 상단에 [📖 열기] 또는 [📥 이 폴더를 한 권으로], 검색 250ms 디바운스·150개 제한) · **[요청]**(대기/완료/실패, 완료는 [열기], 끝난 항목 지우기, 배지). 요청 시트에서 책 제목 수정 가능(기본값은 PC `default_title`과 같은 규칙). 대기 요청이 있으면 30초마다 importq 확인, 끝나면 list 갱신·토스트. 마지막 탭 `fvTab`.
- **검증**: 웹앱 단위(가짜 GAS + 임시 서고: 폴더 1·2·10 자연 순, 단일 파일 제목=폴더명, 없는 원본·`../` 거부, imp 갱신, putdata kind/src) / 실제 서고로 `import_folder`(D&D1 142파일 → 15,984문단, 1.4초) / 셸 Playwright(실제 카탈로그 12,010개 + 가짜 GAS: 탭·정렬·탐색·폴더 요청·검색·가져온 폴더 열기·요청 완료→열기·리로드 시 ver 같으면 재다운로드 없음).
- **함정(10-02 실제)**: 새 GAS가 업로드는 처리하고도 HTML 오류 페이지를 돌려준 적 있음 → 처음엔 'JSON 아님'을 구 GAS로 판정해 '재배포 필요'를 잘못 안내. 지금은 '알 수 없는 op'/'book 누락'만 구 GAS로 보고, JSON 아닌 응답은 `<title>`·본문 앞부분을 로그에 같이 찍음. **원인 확정(13:19)**: 로그에 doGet의 '인증 실패 — URL 뒤에 &key=' 문구가 찍힘 → Google이 간헐적으로 /exec POST를 다른 script.google.com 주소로 302 → urllib이 POST를 GET으로 바꿔 본문(op·key)이 사라짐. 수정: `_KeepPostRedirect`(목적지가 script.google.com이면 POST·본문 유지, googleusercontent echo는 GET) + JSON 아니면 2회까지 재시도. 셸 `gas()`도 JSON 아니면 1회 재시도(Pages b14280b).
- **남은 것(선택)**: 개인 카테고리/태그·책장(지금은 서고 폴더 = 분류), 카탈로그 자동 재스캔 주기(지금은 앱 시작 시 1회 + 버튼), 웹앱 [📚 텍스트 책 가져오기]의 폴더 선택 대화상자, epub/mobi(카탈로그 제외).

### 4단계 — (원 설계 메모, 참고용)
- `ebook_shell/index.html`(14KB)에 ① Drive OAuth(GIS) + 폴더 목록(`E:\Coding\eBook_viewer\src\js\google_drive.js` 471줄 중 인증·목록만 이식, Pages origin을 OAuth 클라이언트에 등록) ② 카탈로그 JSON(Drive) — 책 종류 태그: 번역/원서/일반, 카테고리·정렬 개인화 ③ 평문책 입구: Drive txt 선택 → PC 가져오기 큐(또는 PC 웹앱에서 가져온 `_book`을 ☁업로드) ④ PC에서도 셸을 여는 바로가기(`fokus_viewer.ico`).
- 셸이 커지면 모듈 분리 먼저 고려. GAS 콜드스타트는 평문책을 Drive 직통으로 읽으면 피할 수 있으나 북마크/위치 저장소(GAS `state` vs Drive 사이드카) 결정 필요.
- 셸은 이중 관리(개발본 `ebook_shell/` + 배포본 Pages 저장소) — 수정 시 양쪽 갱신.

### 5단계 — eBook_viewer 보관 ✅ (2026-10-03 Opus 세션)
- `E:\Coding\eBook_viewer` 점검: 실제 미커밋 작업 없음(`git status`의 M 표시는 마운트의 권한·줄바꿈 차이 — `-c core.fileMode=false -c core.autocrlf=true`로 보면 깨끗), origin과 동일했음.
- 커밋 **46bb3c7**(로컬, origin보다 1 앞섬 — push는 PC에서 `git push`): `ARCHIVED.md`(후속 Fokus Viewer 대응표), README 첫 줄 보관 표시, **`disable_autostart.ps1`**. 마운트 git 잠금은 `.git/index.lock`→`.stale.*`로 mv(메모리 git-on-mount 절차).
- **"HTTP server online." 로그의 출처**: 저장소 코드·스크립트 어디에도 그 문구를 쓰는 곳이 없음. 형식 `2026-10-03 오전 9:15:`는 PowerShell `Get-Date -Format g` → **저장소 밖의 Windows 자동 시작(작업 스케줄러/시작프로그램/레지스트리 Run)** 이 부팅마다 씀(1/30부터 443줄, 하루 1~3회). 이 세션은 AppData(시작프로그램) 접근 불가·작업 스케줄러 조회 불가 → 사용자가 스크립트 실행.
- `disable_autostart.ps1`(UTF-8 BOM, pwsh 7.4 파서로 문법 확인): ① 작업 스케줄러 ② 시작프로그램 폴더(사용자·공용, .lnk 대상) ③ HKCU/HKLM Run ④ 포트 8000 리스너 — 패턴 `eBook_viewer|HTTP_Server_Log|HTTP server online|start_server|http.server`, 실행 줄이 부르는 .ps1/.bat/.cmd/.vbs **파일 내용까지** 검사(`Expand`). 항목마다 Y/N, 삭제 없음(작업=사용 안 함, 바로가기=`_autostart_backup`으로 이동, Run=.reg 백업 후 제거). 못 찾으면 진단용 명령 2줄 안내.
- **원인 확정(10-03 09:45)**: 스크립트는 '해당 없음' → 범위를 넓혀 확인 → **Everything(voidtools)의 HTTP 서버**. `%APPDATA%\Everything\Everything.ini`: `http_server_enabled=1`, `http_server_port=8081`(0.0.0.0·:: 전체 수신 — 같은 네트워크에서 PC 파일 목록 노출), `http_server_log_file_name=E:\Coding\eBook_viewer\HTTP_Server_Log.txt`, `http_server_logging_enabled=1`. Everything이 로그인 때 `-startup`으로 뜨며 'HTTP server online.' 기록 — 실행 줄에 경로가 없어 시작프로그램 검사로는 못 잡음(1/30 eBook_viewer 개발 때 로컬 서버로 켠 것으로 추정). 해결: Everything → 도구 → 옵션 → HTTP 서버 → 'HTTP 서버 사용' 해제 (사용자 조치). 확인: `Get-NetTCPConnection -State Listen -LocalPort 8081`이 비면 꺼짐.
- 오진 기록: Claude `claude_desktop_config.json`의 eBook_viewer 3곳은 **폴더 허락 기록**(remoteSessionFolderGrants·remoteChatFolderGrants)이라 무관. MCP 서버는 BlenderMCP 하나. PowerShell 5.1 `ConvertFrom-Json` 오류는 한글을 ANSI로 읽어서 생긴 것(파일 정상) — 그 파일은 수정하지 말 것.
- eBook_viewer `ARCHIVED.md`에 원인 기록 → 커밋 **e3f572f**(origin보다 1 앞섬, PC에서 `git push`). 마운트 git 잠금은 `index.lock`뿐 아니라 **`HEAD.lock`**도 남음 → `.git` 아래 `*.lock` 전부 `.stale.*`로 mv 후 커밋.
- 남은 선택: 폴더를 `E:\Coding\_archive\`로 이동, GitHub `kiuk104/eBook_viewer` 저장소 Archive(설정 → Archive this repository).

### 6단계 — 예전 eBook_viewer 기능 흡수 ✅ (2026-10-03 Opus 세션)
사용자 요청: 예전 뷰어의 ① 원본 txt 줄바꿈 교정 ② 제목·소제목 설정 ③ 문단 구분. (예전 구현: `utils.js cleanUpText` — 문장부호로 안 끝나는 줄은 무조건 붙이고, 50자 미만·종결어미 없는 줄은 전부 h3 소제목으로 자동 지정 / 선택 → h2 변환 / "줄바꿈: 자동·원본"은 CSS pre↔pre-wrap 표시 전환뿐)
- **소제목 `### `** (장 제목 `## `와 구분)
  - core: `_SUB_MARK`, `_is_subhead`; `_strip_mark`가 둘 다 제거; `_is_heading`은 `### `에 False(장 분할 안 함); `_should_join`은 소제목도 병합 금지. `export_outputs`가 EPUB용 문단에만 `### ` 유지 → `_xhtml`이 `<h3 id="sN">`, `write_epub` nav에 장 아래 `<ol>` 2단(`chNNN.xhtml#sN`). TXT는 마커 제거.
  - 하이라이트 좌표: `_disp_text`·JS `stripMark` 둘 다 `^#{2,3}\s*` (안 맞추면 소제목 문단 하이라이트가 밀림). `restructure_book` merge/split의 마커 정규식도 같이.
  - edit_ui: `headLv()`; [§ 제목] 버튼이 본문→`## `→`### `→본문 순환(소제목일 때 라벨 "§ 소제목", 회색); 읽기 `.rd.rdsh`; 출력 미리보기 h3; 기호 도움말 표에 `### `.
  - edit_ui 필터 select에 **「목차 (제목·소제목)」**(누르면 이동) · **「소제목 후보 찾기」** — `#hov` 전체화면 오버레이. 후보 규칙(`subCands`): 30자 이하·한 줄·문장끝 기호/한국어 종결어미 없음·따옴표류로 시작 안 함·`* * *` 제외·앞뒤 문단이 max(25, 2×길이) 이상·제목 바로 뒤 줄 제외, 500개 상한. 자동 적용 없음 — 항목별 [§ 소제목]/[§ 장 제목]/[해제] 또는 [모두 소제목으로]. 적용은 원문 마커 수정 = 일반 수정 경로(폰은 수정 큐, PC는 💾).
  - ebook_import: 마크다운 제목 `#`·`##` → `## `, `###`~ → `### ` (`_MD_HEAD`, reflow에서 제목 줄 앞뒤 새 문단). 青空文庫 小見出し → `### `. 소제목 자동 판별은 하지 않음(짧은 대사 오인).
- **가져오기 창** (웹앱 [📚 텍스트 책 가져오기] → 모달 `#imp`): 파일/폴더 고르기(`Api.import_pick`), 책 제목, **줄바꿈 교정 방식**(자동/wrap/lines/blank) + 루비, `Api.import_preview` → `ebook_import.preview()`(저장 없이 앞 40문단·판별 정보·저장 위치, 폴더는 앞 파일부터 40문단까지). 이미 가져온 책이면 경고(문단 수·하이라이트·북마크 수). `Api.import_text(cfg, path, split, ruby, title)` — title=None이면 예전 동작.
  - 재가져오기 안전장치: `_save_book`이 기존 book.json을 `_work/reimport_YYYYmmdd_HHMMSS/`에 사본(편집 페이지에서 고친 본문·제목 지정이 새 재구성으로 덮이므로).
  - `import_folder`의 `## 파일명` 삽입을 `_file_label`로 분리(미리보기와 공용).
- **여러 문단 한 번에 합치기** (PC·평문 책): 읽기 모드에서 여러 문단에 걸쳐 드래그 선택 → 팝업 **[⤵ 문단 N개 합치기]**(`onSelMerge`, 하이라이트 팝업 대신). 20개 이상이거나 사이에 제목이 있으면 확인. `/api/restructure {op:'merge', i, n}` — `_edit_restructure`가 `restructure_book` merge를 n번 반복(번역·북마크·위치·하이라이트 보정 재사용), 로그 "문단 합치기 (N개)". 폰은 여전히 불가(fp).
- 검증: py_compile·node --check(edit_ui·WEB_HTML) / 단위: reflow·mark_headings(md·青空), EPUB nav 2단·h3·TXT, `_is_heading('### ABC')`=False, merge 시 `###` 제거·하이라이트 좌표 / 컨테이너 실제 편집 서버 + Playwright: 후보 1개 검출→소제목 지정→저장(book.json `### `), 목차 l2/l3·이동, 버튼 순환, 3문단 드래그 합치기(15→13문단) / 웹앱 모달 가짜 API: 판별 정보·경고·방식 변경 재미리보기·제목 수정·인자 전달.
- 반영: PC 앱 재시작 → [📝 편집 페이지]. 폰은 [☁ 업로드] 1회(새 edit_ui).

### 7단계 — 평문 책 자동 교정 (규칙 + AI) ✅ (2026-10-03 Opus 세션)
사용자 요청: "수작업이 아닌 자동 교정" → 방식 C(규칙으로 확실한 것 + 애매한 곳만 AI) 한 번에.
- **실측 근거** (서고 14권 샘플 `_to_delete/af_samples/s01~s14.txt` — 원본 경로는 같은 폴더 복사 로그 참고): 오류 유형 ① 더블 스페이스(줄마다 빈 줄, s08 분향논검 → 줄마다 문단) ② 이중 줄바꿈 조각("서비"/"스", s10) ③ 낱말 중간 잘림("죄송합니"+"다.", "걱정"+" 을") ④ 덜 나뉨(들여쓰기·빈 줄 없음, s02 태백산맥 750KB가 63문단) ⑤ 게시물 하나에 형식이 섞임(새 문단 4칸/이어쓰기 2칸 ↔ 2칸/1칸) ⑥ 제목 꼴 `[ 여행`·`5`·`(7) 제목`·`제 5 판 서문` ⑦ PC통신 머리(`#3705 홍춘식 (Sunburst)`, `… 288 line`, `보낸이:… 조회:`, `제 목:/지은이:/옮긴이:`, `차 례: X`).
- **재구성(reflow) 자체 개선** — `ebook_import.reflow` wrap 모드, 자동 교정 설정과 무관하게 항상: 꽉 찬 미종결 줄 뒤 빈 줄은 soft(다음 줄이 이어지면 잇기) / 조각 줄(≤max(3, p95×12%), 꽉 찬 미종결 줄 뒤)은 이어쓰기 + 짧음을 문단 끝 신호로 안 씀 / 서술 끝→여는 따옴표, 닫는 대사→서술(인용 조사 하고·라고… 제외)이면 새 문단 / 들여쓰기 기준을 최근 60줄의 15% 이상 들여쓰기 중 최솟값으로 / `_join`이 `ko_nospace`(줄 첫머리가 조사·어미 조각 `KO_FRAG`, 또는 앞 줄이 `KO_CUT_END`로 끝남)면 붙여 씀. 결과: s02 63→417문단(미종결 52%→6%), s08 미종결 60%→15%, s03 7%→3%, s10 40%→29%. 공용 도우미 `KO_FRAG`·`KO_CUT_END`·`ko_nospace`·`is_term`·`_OPEN_Q`·`_QUOTATIVE`.
- **`ebook_autofix.py` (새)**: `analyze(paras, p95)` → ops(확실) + asks(애매) + stats / `ai_decide(P, asks, cfg, log, call=)` / `apply_ops(book, done, ops)` (문단 번호 내림차순, 같은 문단은 merge→head→split→drop, AI 판정이 규칙보다 우선) / `undo_item` / `estimate` / `run(book, done, mode, cfg, log)`.
  - 규칙 ops: merge(낱말 중간 잘림 → 붙여 씀, 대사 따옴표 열린 채 끝남 → 띄어 씀), head(제목 꼴 외톨이 줄 — 앞뒤 둘 다 짧으면 목차로 보고 제외, 문장처럼 끝나면 제외, `[ ]` 줄이 20개 이상이면 [대사] 문체로 보고 닫힌 괄호 꼴 제외; ## 제목이 없고 같은 꼴이 3번 이상이면 장 제목, 아니면 소제목), drop(PC통신 서명 줄 ±4 안의 서명·메타 줄; `차 례: X` → `## X`), split(평균 문단 400자 이상인 책만 — 대사 경계).
  - AI asks: J 경계(merge/merge0/keep — 대사 합치기는 띄어쓰기만 묻는 `sp`), H 짧은 외톨이 줄(head/sub/body/merge), S 1200자 이상 덩어리의 문장 번호. 6,000자 묶음, `PROMPT_AF`, 응답 `[{id,a}]`만 — 본문을 고쳐 쓰지 않음, 범위 밖·모르는 판정은 버림. 엔진 = 번역 엔진 설정(backend): Claude는 `af_claude_model`(기본 claude-haiku-4-5), Gemini·Kimi·Ollama는 번역과 같은 모델. 사용량 집계 파트 "교정".
  - 기록 `book["af"] = {mode, ts, items:[{t:merge|split|head|drop, i, off?, sep?, a?, b?, prev?, new?, text?, by:rule|ai, why}], no:[되돌린 곳 서명]}`. `restructure_book`이 구조 변경 때 `_af_remap`으로 items 번호·오프셋을 함께 옮김(제자리 갱신 `[:]` — 목록을 새로 만들면 호출부 참조가 끊김, 실제 버그였음). 되돌린 곳은 `no`에 서명(`m:앞12|뒤12`, `h:`, `d:`)으로 남겨 재검사 때 건너뜀.
- **core**: `restructure_book(…, sep=None, text="")` + op `insert`/`drop`(`_shift_marks`), `_struct_save`(합치기·나누기·자동 교정·되돌리기 공용 저장), `/api/autofix {mode: rule|ai, dry}`·`/api/af_undo {k}`, `edit_data`에 `af`.
- **ebook_import**: `import_text/import_folder(…, autofix="off"|"rule"|"ai", cfg=)` — `_src.txt` 왕복은 교정 안 함. `preview(…, autofix=)`는 규칙 교정을 적용해 보여 주고 AI 예상치(곳·토큰·엔진)만. CLI `--autofix`(ai는 ebook_config.json 키 사용). 로그 "자동 교정 적용: 합치기 · 나누기 · 제목 · 잡음 줄 제거".
- **웹앱**: 가져오기 창 **「자동 교정」**(규칙만(무료)/규칙 + AI(유료)/끄기) + 결과·예상 토큰 줄. 선택은 cfg `af_mode`(숨은 `c_af_mode`, `_SAVE_KEYS`, 기본 rule)로 저장 → 폰 요청 가져오기(`_process_importq`)도 같은 방식.
- **edit_ui**: 필터 **「자동 교정 기록」** → `#hov` 목록(종류·AI 필터, 800건 표시, 누르면 이동), PC(SRV&&PLAIN)는 **[되돌리기]** + 「다시 검사」 상자(예상치 → [규칙으로 고치기] / [규칙 + AI로 고치기] — AI는 토큰 확인 창). 실행 전 저장·`marksFlush`·`_marksLock`, 끝나면 새로고침 후 목록 다시 열림(`edAfOpen`).
- **검증**: 14권 전후 통계 / 문자 보존(공백·제목 기호 빼고 본문 글자 수 동일) / 가짜 AI 모델로 묶음·검증·띄어쓰기 덮어쓰기·S 나누기 / 컨테이너 실제 편집 서버 + Playwright(s08 가져오기 120건 기록 → 되돌리기 → 재검사가 되돌린 곳 건너뜀 → 규칙+AI 실행) / 웹앱 창(가짜 API). 기존 테스트(소제목·목차·여러 문단 합치기) 재통과.
- **실제 AI 호출은 미검증**: 사용자 PC 샌드박스에선 Anthropic만 닿고 ebook_config.json의 Claude 키가 401(앱은 환경변수 키를 쓰는 듯). 첫 실사용 때 로그의 "AI 판정 n/n 묶음"과 결과를 확인할 것.
- **함정(세션 도구)**: `device_commit_files`는 outputs의 같은 경로를 다시 쓰면 **처음 올린 옛 사본**을 보낼 수 있음 → 매번 새 폴더(fv2…)에 담고, 커밋 후 md5로 확인.

- **추가(19:4x) — 원본 손상 감지**: 사용자가 '앵무새 죽이기'를 가져왔더니 애매한 곳 1,028곳 → 조사 결과 **원본 txt 자체가 약 40% 지점부터 깨져 있음**(한글 사이에 엉뚱한 한자·기호, "크리스마만찬銖?때맨毬ち?"; CP949 재정렬로도 복구 불가 — 글자 일부 소실). 샘플 s07(무협)도 4%. `ebook_import.is_garbled(s, lang)`(괄호 병기·「」 제외, PUA·상자선·원문자·한글 사이 한자·"가?나" 비율 > 6%)·`damage(paras)` → 가져오기 로그 ⚠, `origin.damage`, 미리보기 `damage`(웹앱 창 ⛔ 줄), edit_ui 「자동 교정·다시 가져오기」 맨 위 경고. `ebook_autofix.analyze(…, lang)`는 깨진 문단을 규칙·AI 대상에서 제외(stats `garbled`) → 앵무새 애매한 곳 1,028 → 202, 합치기 197 → 24. 정상 샘플 13권은 0%.
- **추가 — 옵션만 바꿔 다시 적용**: `origin.opt = {split, ruby, autofix}` 저장. 웹앱 [📚 텍스트 책 가져오기]는 지금 소스가 이미 가져온 책이면 그 책으로 열리고(이전 옵션·제목 복원, 버튼 "🔁 이 옵션으로 다시 적용"), 다른 책은 [파일 고르기]. 편집 페이지 「자동 교정·다시 가져오기」에 **원본에서 다시 만들기**(줄바꿈 교정·자동 교정·(일본어면 루비) → 미리 계산 → 다시 적용) — `/api/reimport {split, ruby, autofix, dry}` → `ebook_import.import_text(origin.file, out=같은 폴더, title=책 제목)` + `_struct_save`. edit_data에 `imp`(opt·damage·split·folder)·`source_lang`. 미리 계산 요청은 `apiQ`로 줄 세움(편집 서버 동시 요청 409 — 실제로 걸렸음).
- **추가 — 원서 언어 빈칸**: 평문 책이 ko/zh로 저장되는데 웹앱 목록(core.LANGS)에 없어 빈칸 → 웹앱 `langs`에 "한국어/중국어 — 평문 책 (번역 없음)" 추가.

## 7. 검증 방법·함정

- py: `python -m py_compile ebook_translate.py ebook_translate_web.py ebook_import.py`
- JS: `edit_ui.html`/`ebook_shell/index.html`/웹앱 `WEB_HTML`에서 `<script>` 추출 → `node --check`
- 렌더: 디바이스 VM(device_bash)에는 playwright 없음 → 클라우드 컨테이너에서 `python -m http.server` + Playwright (file://는 localStorage 불가, http로 서빙)
- Drive 스트리밍 폴더에서 `find` 반복은 2분 타임아웃 → 인덱스 1회 생성해 재사용
- 평문 미리보기: `python ebook_import.py <txt> --preview 30`
- 반영 체인: core/웹앱 수정 → PC 앱 재시작(+[☁ 업로드]) / gs 수정 → 새 버전 배포 / 셸 수정 → Pages 저장소 교체
- 편집 서버 실물 테스트(컨테이너): `ebook_translate.py`+`edit_ui.html` 복사, `comic_retype_pipeline.py` 스텁(`safe_write_text`·`usage_summary`·`check_free`·`_clean_ws`), `core.resolve_out`·`_apply_keys` 몽키패치 후 `run_edit_server({}, print)`. 서버 종료는 `pkill -f srv.py` 금지(자기 셸까지 죽음) — `ps`로 PID 골라 kill.
- Playwright 클릭 함정: 화면 위쪽 문단은 헤더 버튼이 가려 클릭을 가로챔 → 문단을 가운데로 스크롤 후 좌표 클릭.

## 8. 파일 지도

```
comic_restore_app/
  ebook_import.py          ★ 평문 가져오기 (새) — 파일·폴더(import_folder)·default_title
  ebook_library.py         ★ 서고 카탈로그 생성 (4단계, 새)
  Fokus Viewer.url         PC 바로가기 (Pages 셸 + fokus_viewer.ico)
  ebook_translate.py       핵심 — resolve_out/edit_data · save_marks(hi) · clean_hi · restructure_book · /api/restructure
  ebook_translate_web.py   PC 웹앱 — import_text·b_import·start 가드·아이콘 sha · _pull_marks(hi·구조 보류) · _push_c(구조 동기화)
  edit_ui.html             읽기·교정 엔진 — PLAIN 분기 · 5색 하이라이트 · hiTrack · 합치기/나누기
  ebook_shell/             PWA 셸 (Fokus Viewer) — 이름·색·아이콘 · 읽는 중/서고/요청 탭
  ebook_gas_sync.gs        GAS 릴레이 — 4단계 catalog·importq·kind/src (★ 재배포 필요)
  ebook_mobile_icon.png    GAS 홈 아이콘(새)   fokus_viewer.ico  PC용 예비
  docs/FokusViewer_작업가이드_2026-10-02.md  ← 이 문서
```
