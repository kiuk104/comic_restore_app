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
import time
import webbrowser
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))
import ebook_translate as core            # noqa: E402  (코어 재사용)
import comic_retype_pipeline as retype    # noqa: E402

CONFIG_PATH = core.CONFIG_PATH
RECENTS_PATH = CONFIG_PATH.parent / "ebook_recents.json"   # 최근 작업·즐겨찾기
WINSIZE_PATH = CONFIG_PATH.parent / "ebook_winsize.json"   # 창 크기 기억
_winsize_timer = None


def _load_winsize() -> tuple:
    try:
        d = json.loads(WINSIZE_PATH.read_text(encoding="utf-8"))
        w = max(560, min(int(d.get("w", 760)), 3200))
        h = max(560, min(int(d.get("h", 780)), 2400))
        return w, h
    except Exception:
        return 760, 780


def _write_winsize(w, h) -> None:
    try:
        WINSIZE_PATH.write_text(
            json.dumps({"w": int(w), "h": int(h)}), encoding="utf-8")
    except Exception:
        pass


def _save_winsize(w, h) -> None:                # 리사이즈 폭주 → 디바운스 저장
    global _winsize_timer
    try:
        if _winsize_timer:
            _winsize_timer.cancel()
        _winsize_timer = threading.Timer(0.4, _write_winsize, args=(w, h))
        _winsize_timer.daemon = True
        _winsize_timer.start()
    except Exception:
        pass
# 최근 항목에 담을 필드 (키·비밀 제외 — 폴더/제목/엔진만 복원)
_RECENT_KEYS = ("src", "title", "source_lang", "ocr", "backend", "glossary",
                "page_range", "claude_model", "gemini_model",
                "deepseek_model", "deepseek_url", "kimi_model")
_RECENT_MAX = 15                       # 고정(pin) 제외 최근 항목 보관 수
# run_book 로그의 "전사 12/305" / "번역 40/1394문단" → 진행률
_PROG_RE = re.compile(r"(전사|번역)\s+(\d+)/(\d+)")
_SAVE_KEYS = ("src", "title", "source_lang", "ocr", "backend",
              "claude_model", "gemini_model", "gemini_key",
              "deepseek_model", "deepseek_key", "deepseek_url",
              "kimi_model", "kimi_key",
              "ollama_model", "glossary", "api_key",
              "gas_url", "gas_key", "gas_auto_push",
              "gas_auto_pull")   # Tk 동일+동기화


def _default_cfg() -> dict:
    d = {
        "src": "", "title": "", "source_lang": "auto",
        "ocr": "claude", "backend": "claude",
        "claude_model": "claude-sonnet-4-5",
        "gemini_model": core.GEMINI_MODEL, "gemini_key": "",
        "deepseek_model": core.DEEPSEEK_MODEL, "deepseek_key": "",
        "deepseek_url": "",
        "kimi_model": core.KIMI_MODEL, "kimi_key": "",
        "ollama_model": retype.OLLAMA_MODEL,
        "glossary": "", "page_range": "", "api_key": "",
        "gas_url": "", "gas_key": "", "gas_auto_push": "",
        "gas_auto_pull": "",
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

    _PULL_EVERY = 40                      # 자동 반영 폴링 주기(초)

    def __init__(self):
        self._window = None
        self._state = {"running": False, "cancel": False, "edit_url": None}
        self._lines: list = []
        self._lock = threading.Lock()
        self._cloud_lock = threading.Lock()   # 수동/자동 pull 동시 실행 방지
        self._prog = None                 # [단계, i, n]
        self._cfg = _default_cfg()
        self._last_pull_err = None
        threading.Thread(target=self._autopull_loop, daemon=True).start()

    # ---------- 폰 수정 자동 반영(주기 폴링) ----------
    def _autopull_loop(self):
        """gas_auto_pull이 켜져 있으면 주기적으로 폰 수정 큐를 확인·반영.
        PC 앱이 열려 있는 동안 '폰에서 고침 → 알아서 반영'을 만든다."""
        while True:
            time.sleep(self._PULL_EVERY)
            try:
                c = normalize_cfg(self._cfg)
                if not c.get("gas_auto_pull"):
                    continue
                if self._state["running"] or not (c.get("gas_url") or "").strip():
                    continue
                if not c.get("src"):
                    continue
                if not self._cloud_lock.acquire(blocking=False):
                    continue                       # 이미 반영 중
                try:
                    r = self._pull_c(c, quiet=True)
                finally:
                    self._cloud_lock.release()
                if r.get("src") or r.get("text"):
                    self._log("☁ 자동 반영: 원문 %d · 번역 %d건 "
                              "(폰에서 다시 열면 최신본)"
                              % (r.get("src", 0), r.get("text", 0)))
                self._last_pull_err = None
            except Exception as e:
                msg = str(e)                       # 같은 오류 반복 로그 방지
                if getattr(self, "_last_pull_err", None) != msg:
                    self._last_pull_err = msg
                    self._log(f"(자동 반영 일시 실패 — {msg})")

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
            "kimi_models": ["kimi-k2.5", "kimi-k2.6"],
            "recents": self.list_recents(),
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

    # ---------- 최근 작업 / 즐겨찾기 ----------
    def _load_recents(self) -> list:
        try:
            d = json.loads(RECENTS_PATH.read_text(encoding="utf-8"))
            return d if isinstance(d, list) else []
        except Exception:
            return []

    def _save_recents(self, items: list) -> None:
        try:
            RECENTS_PATH.write_text(
                json.dumps(items, ensure_ascii=False, indent=1),
                encoding="utf-8")
        except OSError:
            pass

    def _resolve_out(self, c: dict):
        """src로부터 실제 출력 폴더·제목 계산 (부작용 없음)."""
        try:
            out, title = core.resolve_out(c)
            return str(out), title
        except Exception:
            return "", (c.get("title") or "")

    def list_recents(self) -> list:
        """고정 먼저, 그다음 최근순. UI 표시용 out(출력폴더)도 포함."""
        items = self._load_recents()
        items.sort(key=lambda e: (not e.get("pin"), -e.get("ts", 0)))
        return items

    def add_recent(self, cfg: dict) -> bool:
        """책을 실제로 쓸 때(번역/업로드) 호출 — src 기준 갱신·중복 제거."""
        c = normalize_cfg(cfg)
        src = (c.get("src") or "").strip()
        if not src:
            return False
        out, title = self._resolve_out(c)
        entry = {k: c.get(k, "") for k in _RECENT_KEYS}
        entry["title"] = title or entry.get("title") or ""
        entry["out"] = out
        items = self._load_recents()
        keep = [e for e in items if (e.get("src") or "").strip() != src]
        old = next((e for e in items
                    if (e.get("src") or "").strip() == src), {})
        entry["pin"] = old.get("pin", False)
        entry["ts"] = time.time()
        keep.insert(0, entry)
        pinned = [e for e in keep if e.get("pin")]
        unpinned = [e for e in keep if not e.get("pin")][:_RECENT_MAX]
        self._save_recents(pinned + unpinned)
        return True

    def pin_recent(self, src: str, pin: bool):
        items = self._load_recents()
        for e in items:
            if (e.get("src") or "").strip() == (src or "").strip():
                e["pin"] = bool(pin)
        self._save_recents(items)
        return {"ok": True, "recents": self.list_recents()}

    def del_recent(self, src: str):
        items = [e for e in self._load_recents()
                 if (e.get("src") or "").strip() != (src or "").strip()]
        self._save_recents(items)
        return {"ok": True, "recents": self.list_recents()}

    def preview_out(self, cfg: dict):
        """현재 폼 기준 출력 폴더 미리보기 — 잘못된 폴더로 생성 방지용 표시."""
        c = normalize_cfg(cfg)
        if not (c.get("src") or "").strip():
            return {"out": "", "exists": False, "msg": "소스 미지정"}
        out, title = self._resolve_out(c)
        try:
            from pathlib import Path as _P
            exists = bool(out) and _P(out).exists()
        except Exception:
            exists = False
        return {"out": out, "title": title, "exists": exists,
                "msg": ("기존 폴더 — 이어서 작업"
                        if exists else "새 폴더 — 처음 실행 시 생성됨")}

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
        self.add_recent(self._cfg)          # 최근 작업에 기록

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
        import urllib.error
        url = (c.get("gas_url") or "").strip()
        if not url:
            raise RuntimeError("동기화 URL(Apps Script 배포 …/exec)을 "
                               "설정하세요 — docs/모바일검수_설계안.md 참고")
        body = json.dumps(dict(payload, key=(c.get("gas_key") or "").strip()),
                          ensure_ascii=False).encode("utf-8")
        raw = None
        for attempt in range(3):          # GAS 간헐 404/5xx·리다이렉트 → 재시도
            req = urllib.request.Request(
                url, data=body,
                headers={"Content-Type": "text/plain;charset=utf-8"})
            try:
                with urllib.request.urlopen(req, timeout=90) as r:
                    raw = r.read().decode("utf-8")
                break
            except urllib.error.HTTPError as e:
                if e.code in (404, 429, 500, 502, 503) and attempt < 2:
                    time.sleep(1.3 * (attempt + 1))
                    continue
                if e.code == 404:
                    raise RuntimeError(
                        "동기화 서버를 찾을 수 없습니다 (404, 재시도해도 실패) — "
                        "배포 URL(…/exec)이 정확한지 확인하세요. 재배포 때 "
                        "'새 배포'를 만들면 주소가 바뀝니다(URL 유지하려면 배포 "
                        "관리 → ✏ → 새 버전). 주소가 바뀌었다면 [모바일 동기화]의 "
                        "URL을 갱신하세요.")
                raise RuntimeError(f"동기화 서버 오류 {e.code}: {e.reason}")
            except urllib.error.URLError as e:
                if attempt < 2:
                    time.sleep(1.3 * (attempt + 1))
                    continue
                raise RuntimeError(f"동기화 서버 연결 실패: {e.reason}")
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
            self.add_recent(self._cfg)      # 업로드한 책도 최근에 기록
            return {"ok": True}
        except Exception as e:
            return {"err": str(e)}

    def _pull_c(self, c: dict, quiet: bool = False) -> dict:
        """폰 수정 큐 → fp 검증 → 반영 → 큐 비움 → 새 스냅샷 재업로드.
        수동(cloud_pull)·자동(_autopull_loop) 공용. 수정 없으면 재업로드 안 함."""
        out, title = core.resolve_out(c)
        book = core.load_book(out)
        if not book:
            raise RuntimeError("편집 데이터(book.json)가 없습니다.")
        bk = book.get("title") or title
        q = self._gas_call(c, {"op": "get", "book": bk})
        if not (q or {}).get("edits"):
            if not quiet:
                self._log("☁ 폰 수정분이 없습니다")
            return {"ok": True}
        r = core.apply_mobile_edits(out, q, self._log)
        self._gas_call(c, {"op": "clear", "book": bk, "fp": q.get("fp")})
        self._push_c(c)                  # 반영본으로 스냅샷 갱신
        return {"ok": True, **r}

    def cloud_pull(self, cfg: dict):
        if self._state["running"]:
            return {"err": "번역 작업 실행 중 — 완료 후 반영하세요."}
        self._cfg = dict(cfg)
        c = normalize_cfg(cfg)
        if not c.get("src"):
            return {"err": "소스 폴더 또는 PDF를 지정하세요."}
        self.save(self._cfg)
        with self._cloud_lock:           # 자동 폴러와 겹치지 않게
            try:
                return self._pull_c(c)
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
 /* 스크롤바 — 앱 테마색으로 통일 (창 전체·로그 등 내부 포함) */
 ::-webkit-scrollbar{width:12px;height:12px}
 ::-webkit-scrollbar-track{background:var(--panel)}
 ::-webkit-scrollbar-thumb{background:var(--line2);border-radius:7px;
   border:3px solid var(--panel)}
 ::-webkit-scrollbar-thumb:hover{background:var(--tx3)}
 ::-webkit-scrollbar-corner{background:var(--panel)}
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
 .engblk{border:1px solid var(--line);border-radius:var(--r);
   padding:6px 10px;margin:6px 0}
 .engblk.inuse{border-color:var(--pri);
   background:color-mix(in srgb, var(--pri) 5%, var(--field))}
 .engblk .bhead{display:flex;align-items:center;gap:8px;margin:2px 0 4px}
 .engblk .bname{font-weight:600;color:var(--tx)}
 .badge{font-size:11px;border-radius:99px;padding:1px 8px;
   background:var(--pri);color:#fff}
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
 .tabs{display:flex;gap:4px}
 .tab{padding:9px 20px;background:var(--panel);color:var(--tx2);
   border:1px solid var(--line);border-bottom:0;border-radius:9px 9px 0 0;
   cursor:pointer;user-select:none;font-size:14px;font-weight:600;
   border-top:3px solid transparent;transition:background .12s,color .12s}
 .tab:hover{background:var(--panel2);color:var(--tx)}
 .tab.on{background:var(--field);color:var(--pri);
   border-top:3px solid var(--pri);border-bottom:1px solid var(--field);
   margin-bottom:-1px;position:relative;z-index:1;
   box-shadow:0 -2px 6px rgba(30,40,60,.06)}
 .tabbody{border:1px solid var(--line);border-radius:0 10px 10px 10px;
   padding:14px;display:none;background:var(--field);margin-bottom:10px}
 .tabbody.on{display:block}
 .tabbody fieldset:last-child{margin-bottom:0}
</style></head><body>

<div class="tabs">
 <div class="tab on" data-t="run">실행</div>
 <div class="tab" data-t="eng">엔진·API 키</div>
 <div class="tab" data-t="sync">모바일 동기화</div>
</div>

<div class="tabbody on" id="tab-run">
<fieldset><legend>소스</legend>
 <div class="row"><label>최근 작업</label>
  <select id="recents" onchange="applyRecent()" style="flex:1"
   title="이전에 쓴 폴더·제목·엔진을 그대로 불러옵니다 (오타로 엉뚱한 폴더 지정 방지)">
   <option value="">— 최근 작업 선택 —</option></select>
  <button onclick="pinRecent()" id="b_pin" title="즐겨찾기 고정/해제">★</button>
  <button onclick="delRecent()" title="이 항목을 최근 목록에서 삭제">🗑</button>
 </div>
 <div class="row"><label>소스 (폴더/PDF)</label><input type="text" id="c_src">
  <button onclick="pickSrcDir()">폴더</button>
  <button onclick="pickSrcPdf()">PDF</button></div>
 <div class="row"><label>책 제목 (출력 파일명)</label>
  <input type="text" id="c_title" placeholder="비우면 소스명"></div>
 <div class="row"><label>출력 폴더</label>
  <span id="outinfo" style="flex:1;font-size:12px;color:#5b6270;
   word-break:break-all;padding:6px 0">소스를 지정하면 출력 위치가 표시됩니다</span></div>
 <div class="row"><label>원서 언어</label><select id="c_source_lang"></select>
  <label style="flex:0 0 auto">페이지 범위</label>
  <input type="text" id="c_page_range" class="w90" placeholder="예: 5-20">
 </div>
</fieldset>

<fieldset><legend>전사 (이미지 → 원문 읽기)</legend>
 <div class="row"><label>전사 방식</label><select id="c_ocr"></select></div>
 <div class="hint" id="hint_ocr"></div>
</fieldset>

<fieldset><legend>번역 (원서 → 한글)</legend>
 <div class="row"><label>번역 엔진</label><select id="c_backend"></select></div>
 <div class="row"><label>용어집 (선택)</label>
  <input type="text" id="c_glossary">
  <button onclick="pickGlossary()">찾아보기</button></div>
 <div class="hint" id="hint_xlat"></div>
</fieldset>

 <div id="summary"></div>
</div>

<div class="tabbody" id="tab-eng">
<fieldset><legend>엔진별 모델·API 키 — 위 선택에 따라 쓰이는 것만 표시</legend>
 <div class="row" style="margin:0 0 4px">
  <span class="hint" style="flex:1">키는 코믹스 앱과 공유됩니다
   (비어 있으면 코믹스 앱에 저장된 키를 자동으로 가져옵니다)</span>
  <label class="hint" style="flex:0 0 auto;cursor:pointer">
   <input type="checkbox" id="showkeys" onchange="toggleKeys()"> 🔑 키 표시</label>
 </div>
 <div class="engblk" id="blk_claude">
  <div class="bhead"><span class="bname">Claude</span>
   <span class="badge" id="bdg_claude"></span></div>
  <div class="row"><label>모델</label>
   <select id="c_claude_model" style="flex:0 0 200px"></select>
   <label style="flex:0 0 auto">ANTHROPIC 키</label>
   <input type="password" data-pw="1" id="c_api_key"
    title="코믹스 앱과 공유됩니다"></div>
 </div>
 <div class="engblk" id="blk_gemini">
  <div class="bhead"><span class="bname">Gemini</span>
   <span class="badge" id="bdg_gemini"></span></div>
  <div class="row"><label>모델</label>
   <select id="c_gemini_model" style="flex:0 0 200px"></select>
   <label style="flex:0 0 auto">GEMINI 키</label>
   <input type="password" data-pw="1" id="c_gemini_key"></div>
 </div>
 <div class="engblk" id="blk_deepseek">
  <div class="bhead"><span class="bname">DeepSeek</span>
   <span class="badge" id="bdg_deepseek"></span></div>
  <div class="row"><label>모델</label>
   <select id="c_deepseek_model" style="flex:0 0 200px"
     title="비전 전사는 DeepInfra URL + deepseek-ai/DeepSeek-OCR 조합 권장 —
공식 api.deepseek.com은 아직 이미지 입력 미지원"></select>
   <label style="flex:0 0 auto">URL</label>
   <select id="c_deepseek_url"
     title="공식 api.deepseek.com은 이미지 입력 미지원 —
비전 전사는 https://api.deepinfra.com/v1/openai + DeepInfra 키를 쓰세요">
   </select></div>
  <div class="row"><label>DEEPSEEK 키</label>
   <input type="password" data-pw="1" id="c_deepseek_key"></div>
  <div class="hint">비전(이미지 전사)은 DeepInfra URL + DeepSeek-OCR
   모델 + DeepInfra 키 조합을 쓰세요 (공식 API는 이미지 미지원)</div>
 </div>
 <div class="engblk" id="blk_kimi">
  <div class="bhead"><span class="bname">Kimi (Moonshot)</span>
   <span class="badge" id="bdg_kimi"></span></div>
  <div class="row"><label>모델</label>
   <select id="c_kimi_model" style="flex:0 0 200px"></select>
   <label style="flex:0 0 auto">MOONSHOT 키</label>
   <input type="password" data-pw="1" id="c_kimi_key"></div>
 </div>
 <div class="engblk" id="blk_ollama">
  <div class="bhead"><span class="bname">Ollama (로컬)</span>
   <span class="badge" id="bdg_ollama"></span></div>
  <div class="row"><label>모델</label>
   <input type="text" id="c_ollama_model" style="flex:0 0 200px">
   <span class="hint">API 비용 0 — Ollama가 실행 중이어야 합니다</span></div>
 </div>
</fieldset>

</div>

<div class="tabbody" id="tab-sync">
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
 <div class="row"><label style="flex:0 0 auto"
   title="이 앱이 열려 있는 동안 약 40초마다 폰 수정분을 자동으로 내려받아 반영">
   <input type="checkbox" id="c_gas_auto_pull"> 폰 수정 자동 반영(약 40초 주기)</label>
  <span class="hint">켜두면 [☁ 수정 반영]을 누르지 않아도 폰 편집이 PC에 반영됩니다</span>
 </div>
</fieldset>
</div>

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
  "deepseek_key","deepseek_url","kimi_model","kimi_key",
  "ollama_model","glossary","api_key",
  "gas_url","gas_key","gas_auto_push","gas_auto_pull"];

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
// 프리셋 + '직접 입력…' 셀렉트 — 저장값이 목록에 없으면 옵션으로 추가,
// '직접 입력…' 선택 시 임시 텍스트 입력으로 전환 (Enter/포커스아웃 확정)
function optCustom(id, values, cur){
  const sel = $(id);
  const list = values.slice();
  if (cur && !list.includes(cur)) list.unshift(cur);
  opt(id, list, cur);
  const co = document.createElement("option");
  co.value = "__custom__"; co.textContent = "직접 입력…";
  sel.appendChild(co);
  sel.dataset.prev = sel.value;
  sel.addEventListener("change", ev => {
    if (sel.value !== "__custom__") { sel.dataset.prev = sel.value; return; }
    ev.stopPropagation();
    sel.value = sel.dataset.prev || list[0] || "";
    const inp = document.createElement("input");
    inp.type = "text";
    inp.id = sel.id;
    inp.style.cssText = sel.style.cssText;
    inp.placeholder = "값 입력 후 Enter";
    const parent = sel.parentNode;
    parent.replaceChild(inp, sel);
    inp.focus();
    const done = () => {
      const v = inp.value.trim();
      parent.replaceChild(sel, inp);
      if (v){
        if (![...sel.options].some(o => o.value === v)){
          const o = document.createElement("option");
          o.value = v; o.textContent = v;
          sel.insertBefore(o, sel.firstChild);
        }
        sel.value = v;
        sel.dataset.prev = v;
        sel.dispatchEvent(new Event("change", {bubbles: true}));
      }
    };
    inp.addEventListener("blur", done);
    inp.addEventListener("keydown", e => {
      if (e.key === "Enter") { e.preventDefault(); inp.blur(); }
    });
  });
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
    if (c[k] === "__custom__") c[k] = "";   // '직접 입력…' 센티널 방어
  }
  return c;
}
function toggleKeys(){
  const show = $("showkeys").checked;
  document.querySelectorAll("input[data-pw]").forEach(i => {
    i.type = show ? "text" : "password";
  });
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
  updateEngineUI();
}

// ── 전사/번역 엔진 선택에 따라 쓰이는 모델·키 블록만 표시 ──
function updateEngineUI(){
  const eng = $("c_ocr").value;      // 전사 방식
  const be = $("c_backend").value;   // 번역 엔진
  const roles = {
    claude:   [eng === "claude" && "전사", be === "claude" && "번역"],
    gemini:   [eng === "gemini" && "전사", be === "gemini" && "번역"],
    deepseek: [eng === "deepseek" && "전사"],
    kimi:     [be === "kimi" && "번역"],
    ollama:   [be === "ollama" && "번역"],
  };
  for (const [k, rs] of Object.entries(roles)){
    const use = rs.filter(Boolean);
    const blk = $("blk_" + k), bdg = $("bdg_" + k);
    if (!blk) continue;
    blk.style.display = use.length ? "" : "none";
    blk.classList.toggle("inuse", !!use.length);
    bdg.textContent = use.length ? use.join("·") + "에 사용" : "";
  }
  const engName = {claude:"Claude", gemini:"Gemini", deepseek:"DeepSeek",
                   winocr:"Windows OCR", tesseract:"Tesseract"}[eng] || eng;
  const local = !["claude", "gemini", "deepseek"].includes(eng);
  $("hint_ocr").textContent = local
    ? "로컬 엔진 — 모델·API 키 불필요 (무료, 인쇄 스캔에 적합)"
    : "→ 아래 " + engName + " 블록의 모델·키를 사용합니다";
  const beName = {claude:"Claude", gemini:"Gemini",
                  kimi:"Kimi", ollama:"Ollama"}[be] || be;
  let hx;
  if (eng === "gemini" && be === "gemini")
    hx = "Gemini가 전사+번역을 한 요청으로 처리합니다 (요청 수 최소)";
  else if (["gemini", "deepseek"].includes(eng))
    hx = engName + "가 원문만 전사하고, 번역은 " + beName
       + "가 별도로 수행합니다 (분리 조합)";
  else if (eng === "claude")
    hx = "Claude가 이미지에서 전사+번역을 처리합니다";
  else
    hx = "전사된 원문을 " + beName + "가 텍스트로 번역합니다";
  hx += " · 끝나면 파트별 예상 요금 표시";
  $("hint_xlat").textContent = hx;
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
function save(){ updSummary(); api().save(collectCfg()); refreshOut(); }

// ── 최근 작업 / 즐겨찾기 ──
let RECENTS = [];
function renderRecents(list){
  RECENTS = list || [];
  const sel = $("recents");
  const cur = $("c_src").value.trim();
  sel.innerHTML = '<option value="">— 최근 작업 선택 —</option>';
  RECENTS.forEach((e, i) => {
    const o = document.createElement("option");
    o.value = String(i);
    const nm = e.title || (e.src || "").split(/[\\/]/).pop();
    o.textContent = (e.pin ? "★ " : "") + nm + "  —  " + (e.src || "");
    if ((e.src || "").trim() === cur) o.selected = true;
    sel.appendChild(o);
  });
  syncPin();
}
function curRecent(){
  const v = $("recents").value;
  return v === "" ? null : RECENTS[+v];
}
function syncPin(){
  const e = curRecent();
  $("b_pin").style.opacity = (e && e.pin) ? "1" : ".4";
}
function applyRecent(){
  const e = curRecent();
  if (!e){ syncPin(); return; }
  for (const k of FIELDS){
    if (k in e){
      const el = $("c_" + k);
      if (el && el.type !== "checkbox") el.value = e[k] == null ? "" : e[k];
    }
  }
  save(); syncPin();
}
async function pinRecent(){
  const e = curRecent(); if (!e) return;
  const r = await api().pin_recent(e.src, !e.pin);
  renderRecents(r.recents);
}
async function delRecent(){
  const e = curRecent(); if (!e) return;
  const r = await api().del_recent(e.src);
  $("recents").value = "";
  renderRecents(r.recents);
}
async function refreshRecents(){
  try { renderRecents(await api().list_recents()); } catch (e) {}
}
let _outT = null;
function refreshOut(){
  clearTimeout(_outT);
  _outT = setTimeout(async () => {
    try {
      const r = await api().preview_out(collectCfg());
      const el = $("outinfo");
      if (!r || !r.out){
        el.textContent = "소스를 지정하면 출력 위치가 표시됩니다";
        el.style.color = "#5b6270"; return;
      }
      el.textContent = r.out + "   ·   " + (r.msg || "");
      el.style.color = r.exists ? "#2e7d55" : "#9a6a00";
    } catch (e) {}
  }, 250);
}

function logLine(s){
  const el = $("log");
  el.textContent += s + "\n";
  el.scrollTop = el.scrollHeight;
}
async function doStart(){
  const r = await api().start(collectCfg());
  if (r && r.err) alert(r.err);
  else refreshRecents();
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
    else refreshRecents();
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
  opt("c_kimi_model", INIT.kimi_models, INIT.cfg.kimi_model);
  optCustom("c_deepseek_model",
            ["deepseek-ai/DeepSeek-OCR", "deepseek-ai/DeepSeek-OCR-2",
             "deepseek-v4-flash", "deepseek-v4-pro"],
            INIT.cfg.deepseek_model);
  optCustom("c_deepseek_url",
            ["https://api.deepinfra.com/v1/openai",
             "https://api.deepseek.com"],
            INIT.cfg.deepseek_url);
  fillForm(INIT.cfg);
  renderRecents(INIT.recents || []);
  ["c_src", "c_title", "c_page_range"].forEach(id =>
    $(id).addEventListener("input", refreshOut));
  refreshOut();
  document.body.addEventListener("change", save);
  tick();
}
document.querySelectorAll(".tab").forEach(t => t.onclick = () => {
  document.querySelectorAll(".tab").forEach(x => x.classList.remove("on"));
  document.querySelectorAll(".tabbody").forEach(x => x.classList.remove("on"));
  t.classList.add("on");
  $("tab-" + t.dataset.t).classList.add("on");
  updSummary();
});
window.addEventListener("pywebviewready", boot);
</script></body></html>"""


def _set_win_icon(title: str, ico: Path) -> None:
    """Windows 한정 — 아이콘 + 제목줄 색을 앱 테마(라이트)와 통일.

    pywebview는 Windows(EdgeChromium)에서 icon 인자를 지원하지 않아
    WM_SETICON으로 직접 지정한다. 제목줄은 Windows 11의
    DWMWA_CAPTION_COLOR로 본문 배경(#f4f5f7)과 같은 밝은 색으로 —
    Windows 10 이하는 미지원이라 호출이 조용히 무시된다.
    """
    if sys.platform != "win32":
        return
    import ctypes
    import time
    u32 = ctypes.windll.user32
    WM_SETICON, LR_LOADFROMFILE, IMAGE_ICON = 0x80, 0x10, 1
    for _ in range(40):                    # 창 생성까지 최대 ~4초 대기
        hwnd = u32.FindWindowW(None, title)
        if hwnd:
            if ico.exists():
                for big, size in ((0, 16), (1, 32)):
                    h = u32.LoadImageW(None, str(ico), IMAGE_ICON,
                                       size, size, LR_LOADFROMFILE)
                    if h:
                        u32.SendMessageW(hwnd, WM_SETICON, big, h)
            try:   # 제목줄을 라이트 테마 색으로 (Windows 11 22000+)
                dwm = ctypes.windll.dwmapi
                DWMWA_CAPTION_COLOR = 35
                DWMWA_TEXT_COLOR = 36
                cap = ctypes.c_uint(0x00F7F5F4)    # #f4f5f7 (COLORREF BGR)
                txt = ctypes.c_uint(0x002B2421)    # #21242b
                dwm.DwmSetWindowAttribute(hwnd, DWMWA_CAPTION_COLOR,
                                          ctypes.byref(cap),
                                          ctypes.sizeof(cap))
                dwm.DwmSetWindowAttribute(hwnd, DWMWA_TEXT_COLOR,
                                          ctypes.byref(txt),
                                          ctypes.sizeof(txt))
            except OSError:
                pass
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
    w0, h0 = _load_winsize()                     # 지난 창 크기 복원
    win = webview.create_window(
        title, html=WEB_HTML, js_api=api,
        width=w0, height=h0, background_color="#f4f5f7")
    api._window = win
    try:                                          # 리사이즈 시 크기 기억
        win.events.resized += _save_winsize
    except Exception:
        pass
    ico = Path(__file__).parent / "ebook_web_icon.ico"
    if not ico.exists():                  # 웹 아이콘 없으면 본편 아이콘
        ico = Path(__file__).parent / "ebook_icon.ico"
    threading.Thread(target=_set_win_icon, args=(title, ico),
                     daemon=True).start()
    webview.start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
