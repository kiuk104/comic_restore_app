r"""
comic_restore_web.py — 만화 한글 복원 웹앱 (pywebview)

Tkinter 앱(comic_restore_app.py)과 같은 코어(run_job·make_args·검수 서버
로직·리소스 측정)를 공유하는 웹 UI. 디자인은 검수 페이지와 같은
'Comic Restore UI' 토큰(claude.ai/design 프로젝트)을 그대로 사용한다.

실행:
    pip install pywebview
    python comic_restore_web.py      (또는 run_webapp.bat)

구조:
    comic_retype_pipeline.py  ← 파이프라인 (양쪽 UI 공유)
    comic_restore_app.py      ← 코어 함수 재사용 (main()은 호출 안 함)
    comic_restore_web.py      ← 이 파일: pywebview 창 + JS 브리지(Api)
"""

from __future__ import annotations

import json
import os
import re
import threading
import webbrowser
import zlib
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))
import comic_restore_app as core          # noqa: E402  (코어 재사용)
import comic_retype_pipeline as retype    # noqa: E402

CONFIG_PATH = core.CONFIG_PATH
_PRESET_SKIP = {"api_key", "save_key", "preset_name"}
_PROG_RE = re.compile(r"\[(\d+)/(\d+)\]")


def _default_cfg() -> dict:
    """Tkinter 앱 v-dict와 같은 기본값으로 설정 로드."""
    cfg0 = {}
    if CONFIG_PATH.exists():
        try:
            cfg0 = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            cfg0 = {}
    auto_exe, auto_models = core.find_upscayl()
    d = {
        "src": "", "out": "",
        "upscayl_exe": auto_exe, "upscayl_models": auto_models,
        "upscayl_model": "digital-art-4x", "out_scale": 2,
        "skip_upscale": False, "resume": True, "use_batch": True,
        "preserve_bg": True, "skip_retype": False, "ink_boost": 0,
        "ocr_engine": "claude", "source_lang": "ko",
        "translate_mode": "vision", "translate_backend": "claude",
        "translate_consensus": False,
        "ollama_model": retype.OLLAMA_MODEL,
        "gemini_model": retype.GEMINI_MODEL, "gemini_api_key": "",
        "deepseek_model": retype.DEEPSEEK_MODEL,
        "deepseek_url": retype.DEEPSEEK_URL, "deepseek_api_key": "",
        "kimi_model": retype.KIMI_MODEL, "kimi_api_key": "",
        "glossary": "",
        "caption_preset": "본문과 동일", "caption_font": "",
        "caption_font_index": 0,
        "shout_preset": "본문과 동일", "shout_font": "",
        "shout_font_index": 0,
        "retype_sfx": False, "text_backing": True, "erase_fill": False,
        "sfx_preset": "자동 감지",
        "sfx_font": core.find_default_sfx_font(), "sfx_font_index": 0,
        "preset_name": "",
        "font": core.find_default_font()[0], "font_index": 0,
        "font_preset": "자동 감지",
        "retype_hand": False,
        "hand_font": core.find_default_hand_font(),
        "hand_preset": "자동 감지",
        "api_key": "", "save_key": False,
        "claude_model": "claude-sonnet-4-5",
        "limit": 0, "page_range": "", "sample_index": 3,
        "zip_preset": "",
    }
    for k in d:
        if k in cfg0 and cfg0[k] is not None:
            d[k] = cfg0[k]
    d["save_key"] = bool(cfg0.get("api_key"))
    d["_presets"] = dict(cfg0.get("presets") or {})
    return d


def normalize_cfg(cfg: dict) -> dict:
    """웹 폼 값 → run_job/make_args가 기대하는 형태 (collect_cfg 동등)."""
    c = dict(cfg)
    c.pop("_presets", None)

    def _i(k, dv=0):
        try:
            c[k] = int(float(c.get(k) or dv))
        except (ValueError, TypeError):
            c[k] = dv

    def _f(k, dv=0.0):
        try:
            c[k] = float(c.get(k) or dv)
        except (ValueError, TypeError):
            c[k] = dv
    _i("limit"); _i("caption_font_index"); _i("shout_font_index")
    _i("sfx_font_index"); _i("font_index")
    _f("out_scale", 2.0); _f("ink_boost", 0.0)
    c["page_range"] = str(c.get("page_range") or "").strip()
    c["glossary"] = str(c.get("glossary") or "").strip()
    c["ollama_model"] = str(c.get("ollama_model") or "").strip()
    for k in ("ollama_model", "gemini_model", "deepseek_model",
              "deepseek_url", "kimi_model"):   # '직접 입력…' 센티널 방어
        if c.get(k) == "__custom__":
            c[k] = ""
    c["font_auto_match"] = str(c.get("font_preset", "")).startswith("자동 매칭")
    c["sample_index"] = 0        # 전체 실행 기본 — 샘플에서만 지정
    return c


class Api:
    """pywebview js_api — 프런트에서 window.pywebview.api.* 로 호출."""

    def __init__(self):
        self._window = None
        self._state = {"running": False, "cancel": False,
                      "server_url": None, "server_cfg": None}
        self._lines: list = []
        self._lock = threading.Lock()
        self._prog = None            # [i, n]
        self._res = ""               # CPU/GPU 표시줄
        self._cfg = _default_cfg()
        self._presets: dict = self._cfg.pop("_presets")

    # ---------- 로그 ----------
    def _log(self, msg) -> None:
        s = str(msg)
        m = _PROG_RE.search(s)
        if m and self._state["running"]:
            self._prog = [int(m.group(1)), int(m.group(2))]
        with self._lock:
            self._lines.append(s)

    def poll(self):
        """프런트 주기 폴링 — 새 로그·실행 상태·진행률·리소스."""
        with self._lock:
            lines, self._lines = self._lines, []
        return {"lines": lines, "running": self._state["running"],
                "progress": self._prog if self._state["running"] else None,
                "res": self._res if self._state["running"] else ""}

    # ---------- 초기화·설정 ----------
    def get_init(self):
        fonts = [{"label": lb, "path": p, "index": i}
                 for lb, p, i in retype.resolve_presets(retype.FONT_PRESETS)]
        hands = [{"label": lb, "path": p, "index": i}
                 for lb, p, i in retype.resolve_presets(retype.HAND_PRESETS)]
        return {
            "cfg": self._cfg,
            "version": retype.__version__,
            "presets": sorted(self._presets),
            "fonts": fonts, "hands": hands,
            "models": core.list_models(self._cfg.get("upscayl_models") or ""),
            "ocr_engines": core.OCR_ENGINES,
            "src_langs": core.SRC_LANGS,
            "xlat_modes": core.XLAT_MODES,
            "xlat_backends": core.XLAT_BACKENDS,
            "zip_presets": [lb for lb, _ in retype.ZIP_PRESETS],
            "defaults": {"font": core.find_default_font()[0],
                         "hand": core.find_default_hand_font(),
                         "sfx": core.find_default_sfx_font()},
        }

    def save(self, cfg: dict):
        self._cfg = dict(cfg)
        c = normalize_cfg(cfg)
        if not cfg.get("save_key"):
            c["api_key"] = ""
        c["presets"] = self._presets
        CONFIG_PATH.write_text(
            json.dumps(c, ensure_ascii=False, indent=2), encoding="utf-8")
        return True

    # ---------- 작품 프리셋 ----------
    def preset_save(self, name: str, cfg: dict):
        name = (name or "").strip()
        if not name:
            return {"err": "프리셋 이름을 입력하세요 (예: 카이지)"}
        self._presets[name] = {k: v for k, v in cfg.items()
                              if k not in _PRESET_SKIP}
        self.save(cfg)
        return {"presets": sorted(self._presets)}

    def preset_load(self, name: str):
        return self._presets.get((name or "").strip()) or {}

    def preset_delete(self, name: str, cfg: dict):
        self._presets.pop((name or "").strip(), None)
        self.save(cfg)
        return {"presets": sorted(self._presets)}

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

    def browse_file(self, desc="파일", pattern="*.*"):
        return self._dialog("open", [f"{desc} ({pattern})"])

    # ---------- 실행 ----------
    def _resolve_out(self, c: dict):
        if not c.get("src"):
            return "원본 폴더를 지정하세요."
        if not c.get("out"):
            c["out"] = str(Path(c["src"]) / "복원출력")
            self._cfg["out"] = c["out"]
        return None

    def _spawn(self, fn, *a):
        self._state["running"], self._state["cancel"] = True, False
        self._prog = None
        threading.Thread(target=self._resmon, daemon=True).start()
        threading.Thread(target=fn, args=a, daemon=True).start()
        return {"ok": True}

    def _resmon(self):
        cpu = core._cpu_sampler()
        cpu()
        c = normalize_cfg(self._cfg)
        use_ol = (c.get("translate_backend") == "ollama"
                  and c.get("translate_mode") == "local-ocr"
                  and c.get("source_lang") != "ko")
        n = 0
        import time
        while self._state["running"]:
            time.sleep(1.5)
            parts = []
            v = cpu()
            if v is not None:
                parts.append(f"CPU {v:.0f}%")
            g = core._gpu_sample()
            if g:
                parts.append(f"GPU {g[0]:.0f}%")
                parts.append(f"VRAM {g[1] / 1024:.1f}/{g[2] / 1024:.1f}GB")
            if use_ol and n % 4 == 0:
                ol = core._ollama_gpu_load(
                    getattr(retype, "OLLAMA_URL", "http://localhost:11434"))
                if ol is not None:
                    parts.append(f"Ollama GPU적재 {ol}%")
            n += 1
            self._res = " · ".join(parts)
        self._res = ""

    def start(self, cfg: dict):
        if self._state["running"]:
            return {"err": "이미 실행 중입니다."}
        self._cfg = dict(cfg)
        c = normalize_cfg(cfg)
        err = self._resolve_out(c)
        if err:
            return {"err": err}
        self.save(self._cfg)

        def worker():
            try:
                core.run_job(c, self._log, lambda: self._state["cancel"])
                self._log("=== 작업 종료 ===")
            except core.Cancelled:
                self._log("=== 사용자가 중지함 ===")
            except Exception as e:
                self._log(f"!! 실패: {e}")
            finally:
                self._state["running"] = False
        return self._spawn(worker)

    def sample(self, cfg: dict, index):
        if self._state["running"]:
            return {"err": "이미 실행 중입니다."}
        self._cfg = dict(cfg)
        c = normalize_cfg(cfg)
        err = self._resolve_out(c)
        if err:
            return {"err": err}
        try:
            c["sample_index"] = max(1, int(index or 1))
        except (ValueError, TypeError):
            c["sample_index"] = 1
        c["out"] = str(Path(c["out"]) / "_sample")
        self.save(self._cfg)

        def worker():
            try:
                core.run_job(c, self._log, lambda: self._state["cancel"])
                outp = Path(c["out"])
                finals = sorted(outp.glob("*_final.png"))
                if finals:
                    self._log("=== 샘플 완료 — 결과 파일을 엽니다 ===")
                    try:
                        os.startfile(finals[0])
                    except (OSError, AttributeError):
                        self._log(f"결과: {finals[0]}")
                else:
                    self._log("샘플 결과 파일이 없습니다 — 로그를 확인하세요.")
            except core.Cancelled:
                self._log("=== 사용자가 중지함 ===")
            except Exception as e:
                self._log(f"!! 실패: {e}")
            finally:
                self._state["running"] = False
        return self._spawn(worker)

    def stop(self):
        self._state["cancel"] = True
        return True

    # ---------- 검수 ----------
    def _ensure_server(self, c: dict):
        """검수 서버 1회 기동 — Tkinter 앱 _ensure_server와 동일 로직."""
        if self._state.get("server_url"):
            return self._state["server_url"]
        out = Path(c["out"])
        try:
            retype.write_review_html(out)
        except Exception as e:
            self._log(f"!! 검수 페이지 재생성 실패: {e}")
        if not (out / "review.html").exists():
            return None
        if c.get("api_key"):
            os.environ["ANTHROPIC_API_KEY"] = str(c["api_key"]).strip()
        if c.get("gemini_api_key"):
            os.environ["GEMINI_API_KEY"] = str(c["gemini_api_key"]).strip()
        if c.get("deepseek_api_key"):
            os.environ["DEEPSEEK_API_KEY"] = \
                str(c["deepseek_api_key"]).strip()
        if c.get("kimi_api_key"):
            os.environ["MOONSHOT_API_KEY"] = str(c["kimi_api_key"]).strip()
        self._state["server_cfg"] = c
        import functools
        from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
        api = self

        class Handler(SimpleHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def end_headers(self):
                self.send_header("Cache-Control", "no-store")
                super().end_headers()

            def do_GET(self):
                p = self.path.split("?")[0]
                if p.startswith("/_srcimg/"):
                    from urllib.parse import unquote
                    stem = Path(unquote(p[len("/_srcimg/"):])).stem
                    cfgS = api._state["server_cfg"]
                    srcd = Path(cfgS.get("src") or "")
                    mime = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg",
                            ".webp": "webp", ".bmp": "bmp"}
                    if srcd.is_dir():
                        for fp3 in sorted(srcd.glob(stem + ".*")):
                            ext = fp3.suffix.lower()
                            if ext in mime:
                                data = fp3.read_bytes()
                                self.send_response(200)
                                self.send_header("Content-Type",
                                                 "image/" + mime[ext])
                                self.send_header("Content-Length",
                                                 str(len(data)))
                                self.end_headers()
                                self.wfile.write(data)
                                return
                    self.send_error(404)
                    return
                super().do_GET()

            def do_POST(self):
                if self.path.split("?")[0] != "/api/rework":
                    self.send_error(404)
                    return
                if api._state["running"]:
                    self.send_error(409, "busy")
                    return
                api._state["running"] = True
                try:
                    n = int(self.headers.get("Content-Length", 0))
                    acts = json.loads(self.rfile.read(n).decode("utf-8"))
                    cfgS = api._state["server_cfg"]
                    args = core.make_args(cfgS)
                    outp = Path(cfgS["out"])
                    up = outp / "_upscaled"
                    pages_dir = up if up.exists() else Path(cfgS["src"])
                    api._log(f"검수 서버: {len(acts)}건 적용 중…")
                    done = retype.apply_rework(outp, acts, args, pages_dir,
                                               log=api._log)
                    api._log(f"검수 서버: 적용 완료 — {done}페이지")
                    us = retype.usage_summary()
                    if us:
                        api._log(us)
                    body = json.dumps({"done": done}).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except Exception as e:
                    api._log(f"!! 검수 서버 오류: {e}")
                    try:
                        self.send_error(500, "rework failed")
                    except Exception:
                        pass
                finally:
                    api._state["running"] = False

        port = 49152 + zlib.crc32(str(out).encode("utf-8")) % 16000
        try:
            srv = ThreadingHTTPServer(
                ("127.0.0.1", port),
                functools.partial(Handler, directory=str(out)))
        except OSError:
            try:
                srv = ThreadingHTTPServer(
                    ("127.0.0.1", 0),
                    functools.partial(Handler, directory=str(out)))
            except Exception as e:
                self._log(f"!! 검수 서버 시작 실패({e})")
                return None
        except Exception as e:
            self._log(f"!! 검수 서버 시작 실패({e})")
            return None
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{srv.server_address[1]}"
        self._state["server_url"] = url
        self._log(f"검수 서버 시작: {url}")
        return url

    def open_review(self, cfg: dict):
        self._cfg = dict(cfg)
        c = normalize_cfg(cfg)
        err = self._resolve_out(c)
        if err:
            return {"err": err}
        url = self._ensure_server(c)
        if url:
            webbrowser.open(url + "/review.html")
            return {"ok": True}
        p = Path(c["out"]) / "review.html"
        if p.exists():
            webbrowser.open(p.resolve().as_uri())
            return {"ok": True}
        return {"err": "표시할 페이지가 아직 없습니다 — 먼저 [전체 시작]으로 "
                       "처리하세요."}

    def rework(self, cfg: dict):
        if self._state["running"]:
            return {"err": "이미 실행 중입니다."}
        self._cfg = dict(cfg)
        c = normalize_cfg(cfg)
        err = self._resolve_out(c)
        if err:
            return {"err": err}
        fp = self._dialog("open", ["JSON (*.json)"])
        if not fp:
            return {"ok": False}
        self.save(self._cfg)

        def worker():
            try:
                if c.get("api_key"):
                    os.environ["ANTHROPIC_API_KEY"] = str(c["api_key"]).strip()
                args = core.make_args(c)
                outp = Path(c["out"])
                up = outp / "_upscaled"
                pages_dir = up if up.exists() else Path(c["src"])
                self._log("검수 반영 시작")
                n = retype.apply_rework(outp, Path(fp), args, pages_dir,
                                        log=self._log)
                self._log(f"=== 검수 반영 완료 — {n}페이지 재조판 ===")
            except Exception as e:
                self._log(f"!! 실패: {e}")
            finally:
                self._state["running"] = False
        return self._spawn(worker)

    # ---------- 완성 ----------
    def export_zip(self, cfg: dict, preset_label: str):
        if self._state["running"]:
            return {"err": "이미 실행 중입니다."}
        self._cfg = dict(cfg)
        self._cfg["zip_preset"] = preset_label
        c = normalize_cfg(self._cfg)
        err = self._resolve_out(c)
        if err:
            return {"err": err}
        preset = next((ps for lb, ps in retype.ZIP_PRESETS
                       if lb == preset_label), None)
        self.save(self._cfg)

        def worker():
            try:
                zp, n, nl = retype.export_final_zip(
                    Path(c["out"]), preset=preset, log=self._log,
                    is_cancelled=lambda: self._state["cancel"])
                mb = zp.stat().st_size / (1 << 20)
                self._log(f"최종본 아카이브 저장: {zp} ({n}페이지, "
                          f"잠금 {nl} — {mb:,.1f} MB)")
                try:
                    os.startfile(zp.parent)
                except (OSError, AttributeError):
                    pass
            except Exception as e:
                self._log(f"!! 최종본 ZIP 실패: {e}")
            finally:
                self._state["running"] = False
        return self._spawn(worker)

    def cleanup_scan(self, cfg: dict):
        c = normalize_cfg(cfg)
        err = self._resolve_out(c)
        if err:
            return {"err": err}
        out = Path(c["out"])
        if not out.exists():
            return {"err": "출력 폴더가 없습니다."}
        items = retype.scan_cleanup(out)
        has_zip = bool(list(out.glob("*.zip")) + list(out.glob("*.cbz")))
        return {"items": items, "out": str(out), "has_zip": has_zip}

    def cleanup_run(self, cfg: dict, keys: list):
        if self._state["running"]:
            return {"err": "작업 실행 중에는 정리할 수 없습니다."}
        c = normalize_cfg(cfg)
        self._resolve_out(c)
        n, freed = retype.cleanup_workdir(Path(c["out"]), keys)
        self._log(f"작업 폴더 정리: {n}개 파일 삭제, "
                  f"{freed / (1 << 20):,.1f} MB 확보")
        return {"n": n, "mb": round(freed / (1 << 20), 1)}


WEB_HTML = r"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><title>만화 한글 복원</title>
<style>
 :root{
  --bg:#f4f5f7; --panel:#e9ebef; --panel2:#e1e4e9; --field:#ffffff;
  --line:#d5d9e0; --line2:#c2c8d3;
  --tx:#21242b; --tx2:#5b6270; --tx3:#8a919e;
  --pri:#2f6fed; --pri-h:#245cd0; --btn2:#e3e6ec; --btn2-h:#d2d7e0;
  --acc:#0ea5c4; --ok:#1f9d43; --warn:#e07f00; --lock:#b8860b;
  --danger:#c04545; --rs:5px; --r:8px;
 }
 *{box-sizing:border-box}
 body{font-family:'Malgun Gothic','Segoe UI',sans-serif;margin:0;
      background:var(--bg);color:var(--tx);font-size:13px;padding:14px}
 .row{display:flex;align-items:center;gap:8px;margin:7px 0}
 .row label{flex:0 0 128px;color:var(--tx)}
 input[type=text],input[type=password],input[type=number],select{
   background:var(--field);color:var(--tx);border:1px solid var(--line2);
   border-radius:var(--rs);padding:6px 8px;font-size:13px;min-width:0}
 input[type=text],input[type=password],select{flex:1}
 input:focus-visible,select:focus-visible{outline:1.5px solid var(--acc)}
 input.w60{flex:0 0 64px} input.w90{flex:0 0 96px}
 button{background:var(--btn2);color:var(--tx);border:1px solid var(--line2);
   border-radius:var(--rs);padding:6px 13px;cursor:pointer;font-size:13px;
   transition:background .12s}
 button:hover{background:var(--btn2-h)}
 button:disabled{opacity:.45;cursor:default}
 button.pri{background:var(--pri);color:#fff;border-color:var(--pri);font-weight:700;
   padding:8px 18px}
 button.pri:hover{background:var(--pri-h)}
 .chkrow{display:flex;align-items:center;gap:7px;margin:6px 0;
   color:var(--tx)}
 .chkrow input{accent-color:var(--pri);width:15px;height:15px}
 .hint{color:var(--tx3);font-size:12px}
 .engblk{border:1px solid var(--line);border-radius:var(--r);
   padding:6px 10px;margin:6px 0}
 .engblk.inuse{border-color:var(--pri);background:
   color-mix(in srgb, var(--pri) 5%, var(--field))}
 .engblk .bhead{display:flex;align-items:center;gap:8px;margin:2px 0 4px}
 .engblk .bname{font-weight:600;color:var(--tx1)}
 .badge{font-size:11px;border-radius:99px;padding:1px 8px;
   background:var(--pri);color:#fff}
 .badge.off{background:var(--line2,#c2c8d3);color:var(--tx2)}
 .dimwrap.isdim{opacity:.45;pointer-events:none}
 .tabs{display:flex;gap:4px;margin-top:12px}
 .tab{padding:10px 22px;background:var(--panel);color:var(--tx2);
   border:1px solid var(--line);border-bottom:0;
   border-radius:9px 9px 0 0;cursor:pointer;user-select:none;
   font-size:14px;font-weight:600;letter-spacing:.2px;
   border-top:3px solid transparent;
   transition:background .12s, color .12s}
 .tab:hover{background:var(--panel2);color:var(--tx)}
 .tab.on{background:var(--field,#fff);color:var(--pri);
   border-top:3px solid var(--pri);border-bottom:1px solid var(--field,#fff);
   margin-bottom:-1px;position:relative;z-index:1;
   box-shadow:0 -2px 6px rgba(30,40,60,.06)}
 .tabbody{border:1px solid var(--line);border-radius:0 10px 10px 10px;
   padding:14px;display:none;background:var(--field,#fff)}
 .tabbody.on{display:block}
 fieldset{border:1px solid var(--line);border-radius:var(--r);
   margin:0 0 10px;padding:8px 12px}
 legend{color:var(--tx2);padding:0 6px;font-size:12px}
 .groups{display:flex;gap:10px;margin-top:12px;align-items:stretch}
 .groups fieldset{margin:0;display:flex;align-items:center;gap:6px;
   flex-wrap:wrap}
 .statusbar{display:flex;align-items:center;gap:10px;margin-top:8px}
 .prog{flex:1;height:14px;background:var(--panel2);
   border:1px solid var(--line);border-radius:4px;overflow:hidden}
 .prog i{display:block;height:100%;width:0;background:var(--acc);
   transition:width .3s}
 .prog.busy i{width:100%;opacity:.35;
   animation:pulse 1.2s ease-in-out infinite}
 @keyframes pulse{50%{opacity:.75}}
 #log{height:200px;overflow-y:auto;background:var(--field);
   border:1px solid var(--line);border-radius:var(--rs);margin-top:8px;
   padding:8px 10px;color:var(--tx2);font-size:12px;line-height:1.65;
   white-space:pre-wrap}
 #summary{color:var(--tx3);margin-top:10px;font-size:12px}
 dialog{background:var(--panel);color:var(--tx);
   border:1px solid var(--line2);border-radius:12px;padding:16px;
   min-width:380px;box-shadow:0 14px 44px rgba(30,40,60,.22)}
 dialog::backdrop{background:rgba(30,36,48,.35)}
 dialog h3{margin:0 0 10px;font-size:14px}
 .zopt,.copt{display:block;margin:7px 0}
 .copt .hint{margin-left:22px;display:block}
 #resline{color:var(--tx2);font-size:12px;white-space:nowrap}
 #statetxt{color:var(--tx2);font-size:12px;min-width:80px;text-align:right}
</style></head><body>

<div class="row"><label>작품 프리셋</label>
 <select id="preset_sel"></select>
 <input type="text" id="preset_name" class="w90" placeholder="이름">
 <button onclick="presetSave()">저장</button>
 <button onclick="presetDel()">삭제</button></div>

<div class="tabs">
 <div class="tab on" data-t="run">실행</div>
 <div class="tab" data-t="opt">보정 옵션</div>
 <div class="tab" data-t="font">폰트</div>
 <div class="tab" data-t="env">환경 설정</div>
</div>

<div class="tabbody on" id="tab-run">
 <div class="row"><label>원본 폴더</label><input type="text" id="c_src">
  <button onclick="pickDir('c_src')">찾아보기</button></div>
 <div class="row"><label>출력 폴더</label>
  <input type="text" id="c_out" placeholder="비우면 원본폴더\복원출력 자동">
  <button onclick="pickDir('c_out')">찾아보기</button></div>
 <div class="row"><label>페이지 범위</label>
  <input type="text" id="c_page_range" class="w90">
  <span class="hint">예: 5-20 · 5- · -20 · 7 — 비우면 전체</span>
  <label style="flex:0 0 auto">테스트 장수</label>
  <input type="text" id="c_limit" class="w60">
  <span class="hint">(0=전체)</span></div>
 <div id="summary"></div>
</div>

<div class="tabbody" id="tab-opt">
 <fieldset><legend>실행 방식</legend>
  <label class="chkrow"><input type="checkbox" id="c_resume">
   이어하기 (완료 페이지 건너뛰기)</label>
  <label class="chkrow"><input type="checkbox" id="c_use_batch">
   Batch API 전사 (50% 할인)</label>
  <label class="chkrow"><input type="checkbox" id="c_skip_retype">
   전사·재조판 건너뛰기 (감지만)</label>
 </fieldset>
 <fieldset><legend>화질·배경</legend>
  <label class="chkrow"><input type="checkbox" id="c_preserve_bg">
   원본 화질 100% 보존</label>
  <label class="chkrow"><input type="checkbox" id="c_text_backing">
   글자 뒤 말풍선 채움</label>
  <label class="chkrow"><input type="checkbox" id="c_erase_fill">
   글자 영역 흰 채움</label>
  <div class="row"><label>글자 굵기 보강</label>
   <input type="text" id="c_ink_boost" class="w60">
   <span class="hint">px (0=끔 · 0.5~2 권장)</span></div>
 </fieldset>
 <fieldset><legend>업스케일</legend>
  <div class="row"><label>모델</label><select id="c_upscayl_model"></select>
   <label style="flex:0 0 auto">최종 배율</label>
   <select id="c_out_scale" style="flex:0 0 64px">
    <option>1</option><option>2</option><option>3</option><option>4</option>
   </select>
   <label class="chkrow" style="margin-left:10px">
    <input type="checkbox" id="c_skip_upscale"> 업스케일 건너뛰기</label>
  </div>
 </fieldset>
</div>

<div class="tabbody" id="tab-font">
 <div class="row"><label>본문 프리셋</label><select id="c_font_preset"></select>
 </div>
 <div class="row"><label>본문 폰트 경로</label><input type="text" id="c_font">
  <button onclick="pickFont('c_font')">찾아보기</button></div>
 <div class="row"><label>손글씨 프리셋</label>
  <select id="c_hand_preset"></select>
  <label class="chkrow" style="flex:0 0 auto">
   <input type="checkbox" id="c_retype_hand"> 손글씨도 재조판</label></div>
 <div class="row"><label>손글씨 폰트</label><input type="text" id="c_hand_font">
  <button onclick="pickFont('c_hand_font')">찾아보기</button></div>
 <div class="row"><label>캡션 프리셋</label>
  <select id="c_caption_preset"></select></div>
 <div class="row"><label>캡션 폰트</label>
  <input type="text" id="c_caption_font">
  <button onclick="pickFont('c_caption_font')">찾아보기</button></div>
 <div class="row"><label>외침 프리셋</label>
  <select id="c_shout_preset"></select></div>
 <div class="row"><label>외침 폰트</label><input type="text" id="c_shout_font">
  <button onclick="pickFont('c_shout_font')">찾아보기</button></div>
 <div class="row"><label>효과음 프리셋</label>
  <select id="c_sfx_preset"></select>
  <label class="chkrow" style="flex:0 0 auto">
   <input type="checkbox" id="c_retype_sfx"> 효과음도 재조판</label></div>
 <div class="row"><label>효과음 폰트</label><input type="text" id="c_sfx_font">
  <button onclick="pickFont('c_sfx_font')">찾아보기</button></div>
</div>

<div class="tabbody" id="tab-env">
 <div class="row"><label>upscayl-bin.exe</label>
  <input type="text" id="c_upscayl_exe">
  <button onclick="pickFile('c_upscayl_exe','exe','*.exe')">찾아보기</button>
 </div>
 <div class="row"><label>models 폴더</label>
  <input type="text" id="c_upscayl_models">
  <button onclick="pickDir('c_upscayl_models')">찾아보기</button></div>
 <fieldset><legend>전사 (이미지 → 원문 읽기)</legend>
  <div class="row"><label>전사 엔진</label><select id="c_ocr_engine"></select>
  </div>
  <div class="hint" id="hint_ocr"></div>
 </fieldset>
 <fieldset><legend>번역 (원서 → 한글)</legend>
  <div class="row"><label>원서 언어</label><select id="c_source_lang"></select>
  </div>
  <div class="dimwrap" id="xlat_rows">
   <div class="row"><label>번역 방식</label>
    <select id="c_translate_mode"></select>
    <label class="chkrow" style="flex:0 0 auto">
     <input type="checkbox" id="c_translate_consensus"> 합의(2-pass)</label>
   </div>
   <div class="row"><label>번역 엔진</label>
    <select id="c_translate_backend"></select></div>
   <div class="row"><label>용어집(선택)</label>
    <input type="text" id="c_glossary">
    <button onclick="pickFile('c_glossary','텍스트','*.txt')">찾아보기</button>
   </div>
   <div class="hint" id="hint_xlat"></div>
  </div>
 </fieldset>
 <fieldset><legend>엔진별 모델·API 키 — 위 선택에 따라 쓰이는 것만 표시</legend>
  <div class="engblk" id="blk_claude">
   <div class="bhead"><span class="bname">Claude</span>
    <span class="badge" id="bdg_claude"></span></div>
   <div class="row"><label>모델</label>
    <select id="c_claude_model" style="flex:0 0 220px">
     <option>claude-sonnet-4-5</option><option>claude-haiku-4-5</option>
    </select>
    <label style="flex:0 0 auto">ANTHROPIC 키</label>
    <input type="password" id="c_api_key">
    <label class="chkrow" style="flex:0 0 auto">
     <input type="checkbox" id="c_save_key"> 키 저장</label></div>
  </div>
  <div class="engblk" id="blk_gemini">
   <div class="bhead"><span class="bname">Gemini</span>
    <span class="badge" id="bdg_gemini"></span></div>
   <div class="row"><label>모델</label>
    <select id="c_gemini_model" style="flex:0 0 220px"></select>
    <label style="flex:0 0 auto">GEMINI 키</label>
    <input type="password" id="c_gemini_api_key"></div>
  </div>
  <div class="engblk" id="blk_deepseek">
   <div class="bhead"><span class="bname">DeepSeek</span>
    <span class="badge" id="bdg_deepseek"></span></div>
   <div class="row"><label>모델</label>
    <select id="c_deepseek_model" style="flex:0 0 220px"
           title="1회 전사(합의 없음) — 초저가. 번역 모드에선 원문 전사만.
만화 전사는 DeepInfra URL + deepseek-ai/DeepSeek-OCR 조합 권장"></select>
    <label style="flex:0 0 auto">URL</label>
    <select id="c_deepseek_url"
           title="공식 api.deepseek.com은 이미지 입력 미지원 —
만화 전사는 https://api.deepinfra.com/v1/openai + DeepInfra 키를 쓰세요">
    </select></div>
   <div class="row"><label>DEEPSEEK 키</label>
    <input type="password" id="c_deepseek_api_key"></div>
  </div>
  <div class="engblk" id="blk_kimi">
   <div class="bhead"><span class="bname">Kimi (Moonshot)</span>
    <span class="badge" id="bdg_kimi"></span></div>
   <div class="row"><label>모델</label>
    <select id="c_kimi_model" style="flex:0 0 220px"
           title="번역 전용 — Claude보다 싸고 번역 품질 좋음.
k2.5=가성비, k2.6=최신·약간 비쌈"></select>
    <label style="flex:0 0 auto">MOONSHOT 키</label>
    <input type="password" id="c_kimi_api_key"></div>
  </div>
  <div class="engblk" id="blk_ollama">
   <div class="bhead"><span class="bname">Ollama (로컬)</span>
    <span class="badge" id="bdg_ollama"></span></div>
   <div class="row"><label>모델</label>
    <select id="c_ollama_model" style="flex:0 0 220px"></select>
    <span class="hint">API 비용 0 — Ollama가 실행 중이어야 합니다</span></div>
  </div>
 </fieldset>
</div>

<div class="groups">
 <fieldset style="flex:2.2"><legend>① 처리</legend>
  <button class="pri" id="b_start" onclick="doStart()">▶ 전체 시작</button>
  <button id="b_sample" onclick="doSample()">샘플</button>
  <span class="hint">번호</span>
  <input type="text" id="c_sample_index" class="w60" value="3">
 </fieldset>
 <fieldset style="flex:2"><legend>② 검수</legend>
  <button onclick="doReview()">검수 페이지</button>
  <button id="b_rework" onclick="doRework()">검수 반영</button>
 </fieldset>
 <fieldset style="flex:1.8"><legend>③ 완성</legend>
  <button id="b_zip" onclick="zipDlg.showModal()">최종본 ZIP</button>
  <button id="b_clean" onclick="openCleanup()">🧹 정리</button>
 </fieldset>
</div>

<div class="statusbar">
 <div class="prog" id="prog"><i id="progbar"></i></div>
 <span id="resline"></span>
 <span id="statetxt">대기 중</span>
 <button id="b_stop" onclick="api().stop()" disabled>■ 중지</button>
</div>
<div id="log"></div>

<dialog id="zipDlg"><h3>최종본 ZIP 내보내기</h3>
 <div id="zipopts"></div>
 <div style="margin-top:12px">
  <button class="pri" style="font-weight:400" onclick="doZip()">ZIP 생성
  </button> <button onclick="zipDlg.close()">취소</button></div>
</dialog>

<dialog id="cleanDlg"><h3>작업 폴더 정리</h3>
 <div class="hint">최종 결과·검수 데이터·브러시 원본·용어집·ZIP은 항상
  보존됩니다.</div>
 <div id="cleanopts" style="margin:8px 0"></div>
 <div id="cleantotal" style="font-weight:700;margin:8px 0"></div>
 <button class="pri" style="font-weight:400" onclick="doCleanup()">선택 항목
  삭제</button> <button onclick="cleanDlg.close()">닫기</button>
</dialog>

<script>
const $ = id => document.getElementById(id);
const api = () => window.pywebview.api;
let INIT = null;

// ── cfg <-> 폼 바인딩 ──
const FIELDS = ["src","out","page_range","limit","resume","use_batch",
 "skip_retype","preserve_bg","text_backing","erase_fill","ink_boost",
 "upscayl_model","out_scale","skip_upscale","font_preset","font",
 "hand_preset","hand_font","retype_hand","caption_preset","caption_font",
 "shout_preset","shout_font","sfx_preset","sfx_font","retype_sfx",
 "upscayl_exe","upscayl_models","api_key","save_key","ocr_engine",
 "claude_model","source_lang","translate_mode","translate_consensus",
 "translate_backend","ollama_model","gemini_model","gemini_api_key",
 "deepseek_model","deepseek_url","deepseek_api_key",
 "kimi_model","kimi_api_key",
 "glossary","sample_index"];

function collectCfg(){
  const c = Object.assign({}, INIT.cfg);
  for (const k of FIELDS){
    const el = $("c_" + k);
    if (!el) continue;
    c[k] = el.type === "checkbox" ? el.checked : el.value;
  }
  c.preset_name = $("preset_sel").value || "";
  return c;
}
function fillForm(c){
  for (const k of FIELDS){
    const el = $("c_" + k);
    if (!el || c[k] === undefined) continue;
    if (el.type === "checkbox") el.checked = !!c[k];
    else el.value = c[k];
  }
  updSummary();
}
// 프리셋 + '직접 입력…' 셀렉트 — 저장된 값이 목록에 없으면 옵션으로 추가,
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
    ev.stopPropagation();               // '__custom__' 자동 저장 방지
    sel.value = sel.dataset.prev || list[0] || "";
    const inp = document.createElement("input");
    inp.type = "text";
    inp.id = sel.id;   // 자동 저장 바인딩·재선택을 위해 id 승계
    inp.style.cssText = sel.style.cssText;
    inp.value = "";
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
      if (e.key === "Enter") inp.blur();
      if (e.key === "Escape") { inp.value = ""; inp.blur(); }
    });
  });
}

// ── 엔진 조합에 따라 실제로 쓰이는 모델·키 블록만 표시 ──
function updateEngineUI(){
  const eng = $("c_ocr_engine").value;
  const sl = $("c_source_lang").value;
  const be = $("c_translate_backend").value;
  const xlat = sl !== "ko";
  const mode = $("c_translate_mode").value;
  // 번역 하위 행: 한국어 복원 모드면 비활성 표시
  $("xlat_rows").classList.toggle("isdim", !xlat);
  // 각 엔진의 역할 계산
  const roles = {
    claude: [eng === "claude" && "전사",
             xlat && be === "claude" && "번역"],
    gemini: [eng === "gemini" && "전사",
             xlat && be === "gemini" && "번역"],
    deepseek: [eng === "deepseek" && "전사"],
    kimi: [xlat && be === "kimi" && "번역"],
    ollama: [xlat && be === "ollama" && "번역"],
  };
  for (const [k, rs] of Object.entries(roles)){
    const use = rs.filter(Boolean);
    const blk = $("blk_" + k), bdg = $("bdg_" + k);
    blk.style.display = use.length ? "" : "none";
    blk.classList.toggle("inuse", !!use.length);
    bdg.textContent = use.length ? use.join("·") + "에 사용" : "";
  }
  // 요약 힌트
  const engName = {claude: "Claude", gemini: "Gemini", deepseek: "DeepSeek",
                   windows: "Windows OCR", tesseract: "Tesseract",
                   easyocr: "EasyOCR"}[eng] || eng;
  const local = !["claude", "gemini", "deepseek"].includes(eng);
  $("hint_ocr").textContent = local
    ? "로컬 엔진 — 모델·API 키 불필요 (kind 분류 없음, 검수 페이지 병용 권장)"
    : "→ 아래 " + engName + " 블록의 모델·키를 사용합니다";
  const beName = {claude: "Claude", gemini: "Gemini",
                  kimi: "Kimi", ollama: "Ollama"}[be] || be;
  let hx = "";
  if (xlat){
    if (eng === "gemini" && be === "gemini")
      hx = "Gemini가 전사+번역을 한 요청으로 처리합니다 (요청 수 최소)";
    else if (["gemini", "deepseek"].includes(eng))
      hx = engName + "가 원문만 전사하고, 번역은 " + beName
           + "가 별도로 수행합니다 (분리 조합)";
    else if (eng === "claude" && mode === "vision")
      hx = "Claude가 이미지에서 전사+번역을 한 요청으로 처리합니다";
    else
      hx = "전사된 원문을 " + beName + "가 텍스트로 번역합니다";
  }
  $("hint_xlat").textContent = hx;
}

function opt(sel, values, cur){
  const el = $(sel);
  el.innerHTML = "";
  for (const v of values){
    const label = Array.isArray(v) ? v[0] : v;
    const key = Array.isArray(v) ? v[1] : v;
    const o = document.createElement("option");
    o.textContent = label; o.value = key;
    el.appendChild(o);
  }
  if (cur !== undefined) el.value = cur;
}

// ── 탭 ──
document.querySelectorAll(".tab").forEach(t => t.onclick = () => {
  document.querySelectorAll(".tab").forEach(x => x.classList.remove("on"));
  document.querySelectorAll(".tabbody").forEach(x => x.classList.remove("on"));
  t.classList.add("on");
  $("tab-" + t.dataset.t).classList.add("on");
  updSummary();
});

function updSummary(){
  try{
    const c = collectCfg();
    const parts = [];
    parts.push(c.skip_upscale ? "업스케일 건너뜀"
      : "업스케일 " + c.upscayl_model + " → " + c.out_scale + "x");
    if (c.skip_retype) parts.push("전사 없음(감지만)");
    else {
      const eng = ($("c_ocr_engine").selectedOptions[0] || {}).textContent
        || ""; parts.push("전사 " + eng.split(" ")[0]
                          + (c.use_batch ? "·Batch" : ""));
    }
    if (c.source_lang !== "ko") parts.push("번역 모드");
    parts.push("본문 " + (c.font_preset || "").split(" (")[0]);
    parts.push("손글씨 " + (c.retype_hand ? "재조판" : "보존"));
    parts.push("효과음 " + (c.retype_sfx ? "재조판" : "보존"));
    if (+c.ink_boost) parts.push("굵기 보강 " + c.ink_boost + "px");
    if (!c.resume) parts.push("이어하기 꺼짐");
    $("summary").textContent = "이번 실행 설정:  " + parts.join(" · ");
  }catch(e){}
}

// ── 폴더/파일 선택 ──
async function pickDir(id){
  const p = await api().browse_dir();
  if (p) { $(id).value = p; updSummary(); }
}
async function pickFile(id, desc, pat){
  const p = await api().browse_file(desc, pat);
  if (p) $(id).value = p;
}
function pickFont(id){ pickFile(id, "font", "*.ttf;*.otf;*.ttc"); }

// ── 폰트 프리셋 연동 ──
function bindFontPreset(selId, pathId, list, autoVal, sameLabel){
  $(selId).onchange = () => {
    const v = $(selId).value;
    const hit = list.find(f => f.label === v);
    if (hit) $(pathId).value = hit.path;
    else if (v === "자동 감지") $(pathId).value = autoVal;
    else if (v === "본문과 동일") $(pathId).value = "";
    updSummary();
  };
}

// ── 작품 프리셋 ──
async function presetSave(){
  const name = $("preset_name").value || $("preset_sel").value;
  const r = await api().preset_save(name, collectCfg());
  if (r.err) { alert(r.err); return; }
  opt("preset_sel", [""].concat(r.presets), name.trim());
  logLine("작품 프리셋 저장: " + name);
}
async function presetDel(){
  const name = $("preset_sel").value;
  if (!name || !confirm("'" + name + "' 프리셋을 삭제할까요?")) return;
  const r = await api().preset_delete(name, collectCfg());
  opt("preset_sel", [""].concat(r.presets), "");
}

// ── 실행 ──
function logLine(s){
  const el = $("log");
  el.textContent += s + "\n";
  el.scrollTop = el.scrollHeight;
}
async function doStart(){
  const r = await api().start(collectCfg());
  if (r && r.err) alert(r.err);
}
async function doSample(){
  const r = await api().sample(collectCfg(), $("c_sample_index").value);
  if (r && r.err) alert(r.err);
}
async function doReview(){
  const r = await api().open_review(collectCfg());
  if (r && r.err) alert(r.err);
}
async function doRework(){
  const r = await api().rework(collectCfg());
  if (r && r.err) alert(r.err);
}
async function doZip(){
  const sel = document.querySelector('input[name=zp]:checked');
  zipDlg.close();
  const r = await api().export_zip(collectCfg(), sel ? sel.value : "");
  if (r && r.err) alert(r.err);
}
async function openCleanup(){
  const r = await api().cleanup_scan(collectCfg());
  if (r.err) { alert(r.err); return; }
  const box = $("cleanopts");
  box.innerHTML = r.has_zip ? "" :
    '<div class="hint" style="color:var(--lock)">⚠ 최종본 ZIP이 아직 ' +
    '없습니다 — 정리 전에 [최종본 ZIP] 권장</div>';
  if (!r.items.length){ alert("정리할 중간 데이터가 없습니다."); return; }
  for (const it of r.items){
    const l = document.createElement("label");
    l.className = "copt chkrow";
    const mb = (it.bytes / 1048576).toFixed(1);
    l.innerHTML = '<input type="checkbox" value="' + it.key + '"' +
      (it["default"] ? " checked" : "") + "> " + it.label +
      " — " + it.count + "개, " + mb + " MB" +
      '<span class="hint">' + it.note + "</span>";
    l.querySelector("input").onchange = updClean;
    box.appendChild(l);
  }
  window._cleanItems = r.items;
  updClean();
  cleanDlg.showModal();
}
function updClean(){
  let n = 0, b = 0;
  document.querySelectorAll("#cleanopts input:checked").forEach(i => {
    const it = window._cleanItems.find(x => x.key === i.value);
    n += it.count; b += it.bytes;
  });
  $("cleantotal").textContent = "선택: " + n + "개 파일, "
    + (b / 1048576).toFixed(1) + " MB 확보";
}
async function doCleanup(){
  const keys = [...document.querySelectorAll("#cleanopts input:checked")]
    .map(i => i.value);
  if (!keys.length) return;
  if (!confirm("선택 항목을 휴지통을 거치지 않고 완전히 삭제합니다. "
               + "계속할까요?")) return;
  cleanDlg.close();
  const r = await api().cleanup_run(collectCfg(), keys);
  if (r.err) alert(r.err);
}

// ── 상태 폴링 ──
let running = false;
async function tick(){
  try{
    const st = await api().poll();
    for (const l of st.lines) logLine(l);
    if (st.running !== running){
      running = st.running;
      for (const id of ["b_start","b_sample","b_rework","b_zip","b_clean"])
        $(id).disabled = running;
      $("b_stop").disabled = !running;
      $("statetxt").textContent = running ? "작업 실행 중…" : "대기 중";
      $("prog").classList.toggle("busy", running);
      if (!running) $("progbar").style.width = "0";
    }
    if (st.progress){
      $("prog").classList.remove("busy");
      $("progbar").style.width =
        (st.progress[0] / st.progress[1] * 100) + "%";
      $("statetxt").textContent =
        st.progress[0] + "/" + st.progress[1] + " 페이지";
    }
    $("resline").textContent = st.res || "";
  }catch(e){}
  setTimeout(tick, 500);
}

// ── 초기화 ──
async function boot(){
  INIT = await api().get_init();
  document.title = "만화 한글 복원 v" + INIT.version + " — 웹앱";
  opt("preset_sel", [""].concat(INIT.presets), INIT.cfg.preset_name || "");
  opt("c_ocr_engine", INIT.ocr_engines, INIT.cfg.ocr_engine);
  opt("c_source_lang", INIT.src_langs, INIT.cfg.source_lang);
  opt("c_translate_mode", INIT.xlat_modes, INIT.cfg.translate_mode);
  opt("c_translate_backend", INIT.xlat_backends, INIT.cfg.translate_backend);
  opt("c_upscayl_model", INIT.models.length ? INIT.models
      : [INIT.cfg.upscayl_model], INIT.cfg.upscayl_model);
  optCustom("c_ollama_model", ["qwen3:14b", "qwen3:8b", "gemma3:12b",
                               "exaone3.5:7.8b"], INIT.cfg.ollama_model);
  optCustom("c_gemini_model", ["gemini-3.1-flash-lite",
                               "gemini-2.5-flash-lite", "gemini-3.5-flash"],
            INIT.cfg.gemini_model);
  optCustom("c_deepseek_model", ["deepseek-ai/DeepSeek-OCR",
                                 "deepseek-ai/DeepSeek-OCR-2",
                                 "deepseek-v4-flash", "deepseek-v4-pro"],
            INIT.cfg.deepseek_model);
  optCustom("c_deepseek_url", ["https://api.deepinfra.com/v1/openai",
                               "https://api.deepseek.com"],
            INIT.cfg.deepseek_url);
  optCustom("c_kimi_model", ["kimi-k2.5", "kimi-k2.6"],
            INIT.cfg.kimi_model);
  const fl = INIT.fonts.map(f => f.label);
  const hl = INIT.hands.map(f => f.label);
  opt("c_font_preset", ["자동 매칭 (원본과 유사한 폰트)", "자동 감지"]
      .concat(fl, ["직접 지정"]), INIT.cfg.font_preset);
  opt("c_hand_preset", ["자동 감지"].concat(hl, ["직접 지정"]),
      INIT.cfg.hand_preset);
  opt("c_caption_preset", ["본문과 동일"].concat(fl, ["직접 지정"]),
      INIT.cfg.caption_preset);
  opt("c_shout_preset", ["본문과 동일"].concat(fl, ["직접 지정"]),
      INIT.cfg.shout_preset);
  opt("c_sfx_preset", ["자동 감지"].concat(fl, ["직접 지정"]),
      INIT.cfg.sfx_preset);
  bindFontPreset("c_font_preset", "c_font", INIT.fonts,
                 INIT.defaults.font);
  bindFontPreset("c_hand_preset", "c_hand_font", INIT.hands,
                 INIT.defaults.hand);
  bindFontPreset("c_caption_preset", "c_caption_font", INIT.fonts, "");
  bindFontPreset("c_shout_preset", "c_shout_font", INIT.fonts, "");
  bindFontPreset("c_sfx_preset", "c_sfx_font", INIT.fonts,
                 INIT.defaults.sfx);
  const zb = $("zipopts");
  INIT.zip_presets.forEach((lb, i) => {
    const l = document.createElement("label");
    l.className = "zopt chkrow";
    l.innerHTML = '<input type="radio" name="zp" value="' + lb + '"' +
      ((INIT.cfg.zip_preset || INIT.zip_presets[0]) === lb ?
        " checked" : "") + "> " + lb;
    zb.appendChild(l);
  });
  $("preset_sel").onchange = async () => {
    const d = await api().preset_load($("preset_sel").value);
    if (d && Object.keys(d).length) { Object.assign(INIT.cfg, d);
      fillForm(INIT.cfg); updateEngineUI(); }
  };
  fillForm(INIT.cfg);
  updateEngineUI();                  // 프리셋 로드·폼 채움 후 블록 갱신
  document.body.addEventListener("change", () => {
    updSummary();
    updateEngineUI();
    api().save(collectCfg());        // 변경 즉시 저장 (Tk앱과 달리 자동)
  });
  tick();
}
window.addEventListener("pywebviewready", boot);
</script></body></html>"""


def _set_win_icon(title: str, ico: Path) -> None:
    """Windows 한정 — 아이콘 + 제목줄 색을 앱 테마와 통일 (실패해도 무시).

    pywebview는 Windows(EdgeChromium)에서 icon 인자를 지원하지 않아
    WM_SETICON 메시지로 직접 지정한다. 제목줄은 Windows 11의
    DWMWA_CAPTION_COLOR로 본문 배경(#f4f5f7)과 같은 색으로 — Windows 10
    이하는 미지원이라 호출이 조용히 무시된다.
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
            try:   # 제목줄 색 통일 (Windows 11 22000+)
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
    title = f"만화 한글 복원 v{retype.__version__} — 웹앱"
    win = webview.create_window(
        title, html=WEB_HTML, js_api=api,
        width=1000, height=880, background_color="#f4f5f7")
    api._window = win
    ico = Path(__file__).parent / "webapp_icon.ico"
    threading.Thread(target=_set_win_icon, args=(title, ico),
                     daemon=True).start()
    webview.start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
