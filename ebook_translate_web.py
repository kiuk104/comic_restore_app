r"""
ebook_translate_web.py — 스캔 이북 한글 번역 웹앱 (pywebview)

Tkinter GUI(ebook_translate.py의 _gui)와 같은 코어(run_book·편집 서버)를
공유하는 웹 UI. 코믹스 웹앱(comic_restore_web.py)과 같은 라이트 테마
토큰을 사용한다.

실행:
    pip install pywebview
    python ebook_translate_web.py      (또는 run_ebook_web.bat)

구조:
    comic_retype_pipeline.py  ← API 호출·요금 집계 (재사용)
    ebook_translate.py        ← 코어: run_book·편집 서버 (GUI는 안 씀)
    ebook_translate_web.py    ← 이 파일: pywebview 창 + JS 브리지(Api)
"""

from __future__ import annotations

import json
import re
import threading
import webbrowser
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))
import ebook_translate as core            # noqa: E402  (코어 재사용)
import comic_retype_pipeline as retype    # noqa: E402

CONFIG_PATH = core.CONFIG_PATH
# run_book 로그의 "전사 12/305" / "번역 40/1394문단" → 진행률
_PROG_RE = re.compile(r"(전사|번역)\s+(\d+)/(\d+)")
_SAVE_KEYS = ("src", "title", "source_lang", "ocr", "backend",
              "claude_model", "gemini_model", "gemini_key",
              "deepseek_model", "deepseek_key", "deepseek_url",
              "ollama_model", "glossary", "api_key",
              "gas_url", "gas_key", "gas_auto_push")   # Tk 동일+동기화


def _default_cfg() -> dict:
    d = {
        "src": "", "title": "", "source_lang": "auto",
        "ocr": "claude", "backend": "claude",
        "claude_model": "claude-sonnet-4-5",
        "gemini_model": core.GEMINI_MODEL, "gemini_key": "",
        "deepseek_model": core.DEEPSEEK_MODEL, "deepseek_key": "",
        "deepseek_url": "",
        "ollama_model": retype.OLLAMA_MODEL,
        "glossary": "", "page_range": "", "api_key": "",
        "gas_url": "", "gas_key": "", "gas_auto_push": "",
    }
    saved = core.load_defaults()          # ebook_config + 코믹스 키 공유
    for k in d:
        if saved.get(k):
            d[k] = saved[k]
    return d


def normalize_cfg(cfg: dict) -> dict:
    """웹 폼 값 → run_book이 기대하는 형태 (Tk collect() 동등)."""
    c = {k: (v.strip() if isinstance(v, str) else v)
         for k, v in dict(cfg).items()}
    c["ollama_url"] = retype.OLLAMA_URL
    return c


class Api:
    """pywebview js_api — 데이터 속성은 전부 `_` 접두사 (브리지 순회 함정)."""

    def __init__(self):
        self._window = None
        self._state = {"running": False, "cancel": False, "edit_url": None}
        self._lines: list = []
        self._lock = threading.Lock()
        self._prog = None                 # [단계, i, n]
        self._cfg = _default_cfg()

    # ---------- 로그 ----------
    def _log(self, msg) -> None:
        s = str(msg)
        m = _PROG_RE.search(s)
        if m and self._state["running"]:
            self._prog = [m.group(1), int(m.group(2)), int(m.group(3))]
        with self._lock:
            self._lines.append(s)

    def poll(self):
        with self._lock:
            lines, self._lines = self._lines, []
        return {"lines": lines, "running": self._state["running"],
                "progress": self._prog if self._state["running"] else None}

    # ---------- 초기화·설정 ----------
    def get_init(self):
        return {
            "cfg": self._cfg,
            "version": core.__version__,
            "langs": [{"label": lb, "key": k} for lb, k in core.LANGS],
            "ocr_modes": [{"label": lb, "key": k}
                          for lb, k in core.OCR_MODES],
            "backends": [{"label": lb, "key": k}
                         for lb, k in core.BACKENDS],
            "claude_models": ["claude-sonnet-4-5", "claude-haiku-4-5"],
            "gemini_models": ["gemini-3.1-flash-lite",
                              "gemini-2.5-flash-lite", "gemini-3.5-flash"],
        }

    def save(self, cfg: dict):
        self._cfg = dict(cfg)
        c = normalize_cfg(cfg)
        try:
            CONFIG_PATH.write_text(
                json.dumps({k: c.get(k, "") for k in _SAVE_KEYS},
                           ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass
        return True

    # ---------- 파일 대화상자 ----------
    def _dialog(self, kind, filters=None):
        import webview
        FD = getattr(webview, "FileDialog", None)
        if kind == "dir":
            mode = FD.FOLDER if FD else webview.FOLDER_DIALOG
        else:
            mode = FD.OPEN if FD else webview.OPEN_DIALOG
        try:
            r = self._window.create_file_dialog(
                mode, file_types=tuple(filters or ()))
        except Exception:
            r = self._window.create_file_dialog(mode)
        if not r:
            return ""
        return r[0] if isinstance(r, (list, tuple)) else str(r)

    def browse_dir(self):
        return self._dialog("dir")

    def browse_pdf(self):
        return self._dialog("open", ["PDF (*.pdf)"])

    def browse_txt(self):
        return self._dialog("open", ["텍스트 (*.txt)"])

    # ---------- 실행 ----------
    def start(self, cfg: dict):
        if self._state["running"]:
            return {"err": "이미 실행 중입니다."}
        self._cfg = dict(cfg)
        c = normalize_cfg(cfg)
        if not c.get("src"):
            return {"err": "소스 폴더 또는 PDF를 지정하세요."}
        self.save(self._cfg)

        def worker():
            try:
                core.run_book(c, self._log, lambda: self._state["cancel"])
                self._log("=== 작업 완료 ===")
                if c.get("gas_auto_push") and (c.get("gas_url") or "").strip():
                    try:
                        self._push_c(c)
                    except Exception as e:
                        self._log(f"!! 자동 업로드 실패: {e}")
            except core.Cancelled:
                self._log("=== 취소됨 (재실행하면 이어서 합니다) ===")
            except Exception as e:
                self._log(f"!! 오류: {e}")
            finally:
                u = retype.usage_summary()   # 취소·오류 시에도 요금 표시
                if u:
                    self._log(u)
                self._state["running"] = False

        self._state["running"], self._state["cancel"] = True, False
        self._prog = None
        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True}

    def stop(self):
        self._state["cancel"] = True
        return True

    # ---------- 편집 모드 ----------
    def open_edit(self, cfg: dict):
        self._cfg = dict(cfg)
        c = normalize_cfg(cfg)
        if not c.get("src"):
            return {"err": "소스 폴더 또는 PDF를 지정하세요."}
        try:
            if self._state["edit_url"]:      # 재클릭 — 최신 데이터로 재생성
                core.write_edit_html(core.resolve_out(c)[0])
            else:
                self._state["edit_url"] = core.run_edit_server(
                    c, self._log, is_busy=lambda: self._state["running"])
        except Exception as e:
            return {"err": f"편집 페이지 열기 실패: {e}"}
        if not self._state["edit_url"]:
            return {"err": "편집할 데이터가 아직 없습니다.\n"
                           "[▶ 번역 시작]으로 전사·병합이 끝난 뒤 다시 "
                           "눌러보세요."}
        webbrowser.open(self._state["edit_url"] + "/edit.html")
        return {"ok": True}

    # ---------- 모바일 동기화 (Apps Script 릴레이) ----------
    def _gas_call(self, c: dict, payload: dict) -> dict:
        """배포 URL(…/exec)에 op 요청 — GAS의 302 리다이렉트는 urllib이 처리."""
        import urllib.request
        url = (c.get("gas_url") or "").strip()
        if not url:
            raise RuntimeError("동기화 URL(Apps Script 배포 …/exec)을 "
                               "설정하세요 — docs/모바일검수_설계안.md 참고")
        body = json.dumps(dict(payload, key=(c.get("gas_key") or "").strip()),
                          ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "text/plain;charset=utf-8"})
        with urllib.request.urlopen(req, timeout=90) as r:
            raw = r.read().decode("utf-8")
        try:
            out = json.loads(raw)
        except ValueError:
            raise RuntimeError("동기화 서버 응답이 JSON이 아닙니다 — "
                               "배포 URL(…/exec)·액세스 설정을 확인하세요")
        if isinstance(out, dict) and out.get("err"):
            raise RuntimeError(out["err"])
        return out

    def _push_c(self, c: dict) -> str:
        """스냅샷(edit.html) 업로드 — cloud_push·완료 자동 업로드 공용."""
        out, title = core.resolve_out(c)
        fp = core.write_edit_html(out)
        if not fp:
            raise RuntimeError("업로드할 편집 데이터가 없습니다 — "
                               "[▶ 번역 시작]으로 전사·병합 후 다시 시도")
        book = core.load_book(out)
        bk = book.get("title") or title
        r = self._gas_call(c, {
            "op": "upload", "book": bk,
            "html": fp.read_text(encoding="utf-8"),
            "snap_fp": core.book_fingerprint(book),
            "snap_count": len(book["paras"])})
        if not (r or {}).get("icon"):    # 홈 화면 아이콘 1회 전송
            try:
                ic = Path(__file__).parent / "ebook_mobile_icon.png"
                if ic.exists():
                    import base64
                    self._gas_call(c, {"op": "icon", "book": bk,
                                       "png": base64.b64encode(
                                           ic.read_bytes()).decode()})
                    self._log("☁ 홈 화면 아이콘 업로드 — 폰에서 홈 화면에 "
                              "다시 추가하면 적용됩니다")
            except Exception as e:
                self._log(f"(아이콘 업로드 생략: {e})")
        self._log(f"☁ 업로드 완료: {bk} — 폰에서 배포URL?book={bk}&key=… "
                  "(목록은 ?key=… 만)")
        return bk

    def cloud_push(self, cfg: dict):
        self._cfg = dict(cfg)
        c = normalize_cfg(cfg)
        if not c.get("src"):
            return {"err": "소스 폴더 또는 PDF를 지정하세요."}
        self.save(self._cfg)
        try:
            self._push_c(c)
            return {"ok": True}
        except Exception as e:
            return {"err": str(e)}

    def cloud_pull(self, cfg: dict):
        """폰 수정 큐 → fp 검증 → 반영 → 큐 비움 → 새 스냅샷 재업로드."""
        if self._state["running"]:
            return {"err": "번역 작업 실행 중 — 완료 후 반영하세요."}
        self._cfg = dict(cfg)
        c = normalize_cfg(cfg)
        if not c.get("src"):
            return {"err": "소스 폴더 또는 PDF를 지정하세요."}
        self.save(self._cfg)
        try:
            out, title = core.resolve_out(c)
            book = core.load_book(out)
            if not book:
                return {"err": "편집 데이터(book.json)가 없습니다."}
            bk = book.get("title") or title
            q = self._gas_call(c, {"op": "get", "book": bk})
            if not (q or {}).get("edits"):
                self._log("☁ 폰 수정분이 없습니다")
                return {"ok": True}
            r = core.apply_mobile_edits(out, q, self._log)
            self._gas_call(c, {"op": "clear", "book": bk, "fp": q.get("fp")})
            self._push_c(c)              # 반영본으로 스냅샷 갱신
            return {"ok": True, **r}
        except Exception as e:
            return {"err": str(e)}

    def import_edits_file(self, cfg: dict):
        """오프라인 폴백 — 폰 [📤]로 내보낸 edits JSON 파일을 반영."""
        if self._state["running"]:
            return {"err": "번역 작업 실행 중 — 완료 후 반영하세요."}
        c = normalize_cfg(cfg)
        if not c.get("src"):
            return {"err": "소스 폴더 또는 PDF를 지정하세요."}
        p = self._dialog("open", ["수정분 JSON (*.json)"])
        if not p:
            return {"ok": False}
        try:
            q = json.loads(Path(p).read_text(encoding="utf-8"))
            out, _ = core.resolve_out(c)
            core.apply_mobile_edits(out, q, self._log)
            return {"ok": True}
        except Exception as e:
            return {"err": str(e)}


WEB_HTML = r"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><title>스캔 이북 한글 번역</title>
<style>
 :root{
  --bg:#f4f5f7; --panel:#e9ebef; --panel2:#e1e4e9; --field:#ffffff;
  --line:#d5d9e0; --line2:#c2c8d3;
  --tx:#21242b; --tx2:#5b6270; --tx3:#8a919e;
  --pri:#2f6fed; --pri-h:#245cd0; --btn2:#e3e6ec; --btn2-h:#d2d7e0;
  --acc:#0ea5c4; --ok:#1f9d43; --warn:#e07f00; --danger:#c04545;
  --rs:5px; --r:8px;
 }
 *{box-sizing:border-box}
 body{font-family:'Malgun Gothic','Segoe UI',sans-serif;margin:0;
      background:var(--bg);color:var(--tx);font-size:13px;padding:14px}
 .row{display:flex;align-items:center;gap:8px;margin:7px 0}
 .row label{flex:0 0 138px;color:var(--tx)}
 input[type=text],input[type=password],select{
   background:var(--field);color:var(--tx);border:1px solid var(--line2);
   border-radius:var(--rs);padding:6px 8px;font-size:13px;min-width:0;flex:1}
 input:focus-visible,select:focus-visible{outline:1.5px solid var(--acc)}
 input.w90{flex:0 0 96px}
 button{background:var(--btn2);color:var(--tx);border:1px solid var(--line2);
   border-radius:var(--rs);padding:6px 13px;cursor:pointer;font-size:13px;
   transition:background .12s}
 button:hover{background:var(--btn2-h)}
 button:disabled{opacity:.45;cursor:default}
 button.pri{background:var(--pri);color:#fff;border-color:var(--pri);
   font-weight:700;padding:8px 18px}
 button.pri:hover{background:var(--pri-h)}
 fieldset{border:1px solid var(--line);border-radius:var(--r);
   margin:0 0 10px;padding:8px 12px}
 legend{color:var(--tx2);padding:0 6px;font-size:12px}
 .hint{color:var(--tx3);font-size:12px}
 .btns{display:flex;align-items:center;gap:8px;margin:12px 0 4px}
 .statusbar{display:flex;align-items:center;gap:10px;margin-top:8px}
 .prog{flex:1;height:14px;background:var(--panel2);
   border:1px solid var(--line);border-radius:4px;overflow:hidden}
 .prog i{display:block;height:100%;width:0;background:var(--acc);
   transition:width .3s}
 .prog.busy i{width:100%;opacity:.35;
   animation:pulse 1.2s ease-in-out infinite}
 @keyframes pulse{50%{opacity:.75}}
 #log{height:230px;overflow-y:auto;background:var(--field);
   border:1px solid var(--line);border-radius:var(--rs);margin-top:8px;
   padding:8px 10px;color:var(--tx2);font-size:12px;line-height:1.65;
   white-space:pre-wrap}
 #summary{color:var(--tx3);margin-top:6px;font-size:12px}
 #statetxt{color:var(--tx2);font-size:12px;min-width:110px;text-align:right}
</style></head><body>

<fieldset><legend>소스</legend>
 <div class="row"><label>소스 (폴더/PDF)</label><input type="text" id="c_src">
  <button onclick="pickSrcDir()">폴더</button>
  <button onclick="pickSrcPdf()">PDF</button></div>
 <div class="row"><label>책 제목 (출력 파일명)</label>
  <input type="text" id="c_title" placeholder="비우면 소스명"></div>
 <div class="row"><label>원서 언어</label><select id="c_source_lang"></select>
  <label style="flex:0 0 auto">페이지 범위</label>
  <input type="text" id="c_page_range" class="w90" placeholder="예: 5-20">
 </div>
</fieldset>

<fieldset><legend>전사·번역</legend>
 <div class="row"><label>전사 방식</label><select id="c_ocr"></select></div>
 <div class="row"><label>번역 엔진</label><select id="c_backend"></select></div>
 <div class="row"><span class="hint" style="margin-left:146px">
  ※ 전사·번역은 따로 조합 — 가성비: Gemini 비전 전사 + Claude 번역
  (끝나면 파트별 예상 요금 표시)</span></div>
 <div class="row"><label>Claude 모델</label>
  <select id="c_claude_model"></select></div>
 <div class="row"><label>Gemini 모델</label>
  <select id="c_gemini_model"></select></div>
 <div class="row"><label>Gemini API 키 (선택)</label>
  <input type="password" id="c_gemini_key"></div>
 <div class="row"><label>DeepSeek 모델</label>
  <input type="text" id="c_deepseek_model"></div>
 <div class="row"><label>DeepSeek API 키 (선택)</label>
  <input type="password" id="c_deepseek_key"></div>
 <div class="row"><label>DeepSeek URL (선택)</label>
  <input type="text" id="c_deepseek_url"
   placeholder="비우면 공식 API — 비전 미지원 시 호환 서버로 교체"></div>
 <div class="row"><label>Ollama 모델</label>
  <input type="text" id="c_ollama_model"></div>
 <div class="row"><label>용어집 (선택)</label>
  <input type="text" id="c_glossary">
  <button onclick="pickGlossary()">찾아보기</button></div>
 <div class="row"><label>API 키 (코믹스 공유)</label>
  <input type="password" id="c_api_key"></div>
</fieldset>

<fieldset><legend>모바일 동기화 (선택 — Apps Script 릴레이)</legend>
 <div class="row"><label>동기화 URL</label>
  <input type="text" id="c_gas_url"
   placeholder="Apps Script 배포 주소(…/exec) — 설정법: docs/모바일검수_설계안.md"></div>
 <div class="row"><label>동기화 키</label>
  <input type="password" id="c_gas_key">
  <label style="flex:0 0 auto" title="번역 완료 시 검수 페이지를 자동 업로드">
   <input type="checkbox" id="c_gas_auto_push"> 완료 시 자동 업로드</label>
  <button onclick="doImport()"
   title="폰 [📤]로 내보낸 수정분 JSON 파일 반영 (오프라인 폴백)">📥 파일 반영</button>
 </div>
</fieldset>

<div id="summary"></div>

<div class="btns">
 <button class="pri" id="b_start" onclick="doStart()">▶ 번역 시작</button>
 <button id="b_stop" onclick="api().stop()" disabled>■ 중지</button>
 <button id="b_edit" onclick="doEdit()">📝 편집 페이지</button>
 <button id="b_push" onclick="doPush()"
  title="검수 페이지를 동기화 서버(Drive)에 업로드 — 폰에서 열람·수정">☁ 업로드</button>
 <button id="b_pull" onclick="doPull()"
  title="폰 수정분을 내려받아 반영하고 새 스냅샷을 재업로드">☁ 수정 반영</button>
 <span class="hint">같은 출력 폴더로 다시 실행하면 이어서 합니다</span>
</div>

<div class="statusbar">
 <div class="prog" id="prog"><i id="progbar"></i></div>
 <span id="statetxt">대기 중</span>
</div>
<div id="log"></div>

<script>
"use strict";
const $ = id => document.getElementById(id);
const api = () => window.pywebview.api;
let INIT = null;

const FIELDS = ["src","title","source_lang","page_range","ocr","backend",
  "claude_model","gemini_model","gemini_key","deepseek_model",
  "deepseek_key","deepseek_url","ollama_model","glossary","api_key",
  "gas_url","gas_key","gas_auto_push"];

function opt(id, items, val){
  const s = $(id); s.innerHTML = "";
  for (const it of items){
    const o = document.createElement("option");
    if (typeof it === "string"){ o.value = it; o.textContent = it; }
    else { o.value = it.key; o.textContent = it.label; }
    s.appendChild(o);
  }
  if (val !== undefined && val !== null) s.value = val;
  if (s.selectedIndex < 0) s.selectedIndex = 0;
}
function fillForm(c){
  for (const k of FIELDS){
    const el = $("c_" + k);
    if (!el) continue;
    if (el.type === "checkbox") el.checked = !!c[k];
    else el.value = c[k] == null ? "" : c[k];
  }
  updSummary();
}
function collectCfg(){
  const c = {};
  for (const k of FIELDS){
    const el = $("c_" + k);
    c[k] = el.type === "checkbox" ? (el.checked ? "1" : "") : el.value;
  }
  return c;
}
function updSummary(){
  try{
    const c = collectCfg();
    const lb = id => ($(id).selectedOptions[0] || {textContent:""})
      .textContent.split(" (")[0];
    const parts = [lb("c_source_lang"),
                   "전사 " + lb("c_ocr"), "번역 " + lb("c_backend")];
    if (c.page_range) parts.push("범위 " + c.page_range);
    $("summary").textContent = "이번 실행 설정:  " + parts.join(" · ");
  }catch(e){}
}

async function pickSrcDir(){
  const p = await api().browse_dir();
  if (p) { $("c_src").value = p; save(); }
}
async function pickSrcPdf(){
  const p = await api().browse_pdf();
  if (p) { $("c_src").value = p; save(); }
}
async function pickGlossary(){
  const p = await api().browse_txt();
  if (p) { $("c_glossary").value = p; save(); }
}
function save(){ updSummary(); api().save(collectCfg()); }

function logLine(s){
  const el = $("log");
  el.textContent += s + "\n";
  el.scrollTop = el.scrollHeight;
}
async function doStart(){
  const r = await api().start(collectCfg());
  if (r && r.err) alert(r.err);
}
async function doEdit(){
  const r = await api().open_edit(collectCfg());
  if (r && r.err) alert(r.err);
}
async function doPush(){
  const b = $("b_push"); b.disabled = true;
  b.textContent = "☁ 업로드 중…";
  try{
    const r = await api().cloud_push(collectCfg());
    if (r && r.err) alert(r.err);
  } finally { b.disabled = false; b.textContent = "☁ 업로드"; }
}
async function doPull(){
  const b = $("b_pull"); b.disabled = true;
  b.textContent = "☁ 반영 중…";
  try{
    const r = await api().cloud_pull(collectCfg());
    if (r && r.err) alert(r.err);
  } finally { b.disabled = false; b.textContent = "☁ 수정 반영"; }
}
async function doImport(){
  const r = await api().import_edits_file(collectCfg());
  if (r && r.err) alert(r.err);
}

let running = false;
async function tick(){
  try{
    const st = await api().poll();
    for (const l of st.lines) logLine(l);
    if (st.running !== running){
      running = st.running;
      $("b_start").disabled = running;
      $("b_stop").disabled = !running;
      $("statetxt").textContent = running ? "작업 실행 중…" : "대기 중";
      $("prog").classList.toggle("busy", running);
      if (!running) $("progbar").style.width = "0";
    }
    if (st.progress){
      $("prog").classList.remove("busy");
      $("progbar").style.width =
        (st.progress[1] / st.progress[2] * 100) + "%";
      $("statetxt").textContent = st.progress[0] + " "
        + st.progress[1] + "/" + st.progress[2];
    }
  }catch(e){}
  setTimeout(tick, 500);
}

async function boot(){
  INIT = await api().get_init();
  document.title = "스캔 이북 한글 번역 v" + INIT.version + " — 웹앱";
  opt("c_source_lang", INIT.langs, INIT.cfg.source_lang);
  opt("c_ocr", INIT.ocr_modes, INIT.cfg.ocr);
  opt("c_backend", INIT.backends, INIT.cfg.backend);
  opt("c_claude_model", INIT.claude_models, INIT.cfg.claude_model);
  opt("c_gemini_model", INIT.gemini_models, INIT.cfg.gemini_model);
  fillForm(INIT.cfg);
  document.body.addEventListener("change", save);
  tick();
}
window.addEventListener("pywebviewready", boot);
</script></body></html>"""


def _set_win_icon(title: str, ico: Path) -> None:
    """Windows 한정 — 타이틀바·작업표시줄 아이콘 (comic_restore_web과 동일)."""
    if sys.platform != "win32" or not ico.exists():
        return
    import ctypes
    import time
    u32 = ctypes.windll.user32
    WM_SETICON, LR_LOADFROMFILE, IMAGE_ICON = 0x80, 0x10, 1
    for _ in range(40):
        hwnd = u32.FindWindowW(None, title)
        if hwnd:
            for big, size in ((0, 16), (1, 32)):
                h = u32.LoadImageW(None, str(ico), IMAGE_ICON,
                                   size, size, LR_LOADFROMFILE)
                if h:
                    u32.SendMessageW(hwnd, WM_SETICON, big, h)
            return
        time.sleep(0.1)


def main() -> int:
    try:
        import webview
    except ImportError:
        print("pywebview가 필요합니다:  pip install pywebview")
        return 1
    api = Api()
    title = f"스캔 이북 한글 번역 v{core.__version__} — 웹앱"
    win = webview.create_window(
        title, html=WEB_HTML, js_api=api,
        width=760, height=780, background_color="#f4f5f7")
    api._window = win
    ico = Path(__file__).parent / "ebook_web_icon.ico"
    if not ico.exists():                  # 웹 아이콘 없으면 본편 아이콘
        ico = Path(__file__).parent / "ebook_icon.ico"
    threading.Thread(target=_set_win_icon, args=(title, ico),
                     daemon=True).start()
    webview.start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
