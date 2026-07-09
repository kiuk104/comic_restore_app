r"""
comic_restore_app.py — 만화 복원 통합 앱 (GUI)

원본 폴더 지정 → Upscayl(digital-art 모델) 업스케일 → 한글 재조판(v3)
→ 출력 폴더로 PSD/PNG 저장까지 한 번에 처리하는 데스크톱 앱.

실행:
    python comic_restore_app.py

필요 패키지:
    pip install opencv-python numpy pillow psd-tools anthropic

준비물:
  1. Upscayl 데스크톱 앱 설치 (자동 감지됨)
     https://upscayl.org  — 기본 경로:
     %LOCALAPPDATA%\Programs\Upscayl\resources\bin\upscayl-bin.exe
  2. ANTHROPIC_API_KEY (앱에 입력하거나 환경변수로 설정)
  3. 나눔명조 Bold 폰트

설정은 이 파일 옆의 app_config.json 에 자동 저장됩니다 (API 키는
'키 저장' 체크 시에만 저장 — 공용 PC에서는 끄세요).
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import traceback
from argparse import Namespace
from pathlib import Path

import cv2
import numpy as np

# 같은 폴더의 재조판 파이프라인 재사용
sys.path.insert(0, str(Path(__file__).parent))
import comic_retype_pipeline as retype

# PyInstaller 실행파일에서는 exe 옆에 설정 저장
if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).parent
else:
    APP_DIR = Path(__file__).parent
CONFIG_PATH = APP_DIR / "app_config.json"
IMG_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}

# 전사 엔진 — (표시명, 내부 키)
OCR_ENGINES = [
    ("Claude AI (권장 — 정확, API 크레딧 사용)", "claude"),
    ("Windows OCR (무료 — winocr 설치·한국어 언어팩 필요)", "windows"),
    ("Tesseract (무료 — 본체+kor 데이터 설치 필요)", "tesseract"),
    ("EasyOCR (무료 — easyocr 설치, 최초 실행 시 모델 다운로드)", "easyocr"),
]


# ---------------------------------------------------------------------------
# Upscayl 자동 감지
# ---------------------------------------------------------------------------
def find_upscayl() -> tuple[str, str]:
    """(upscayl-bin.exe 경로, models 폴더 경로) — 못 찾으면 빈 문자열."""
    candidates = []
    la = os.environ.get("LOCALAPPDATA", "")
    if la:
        candidates.append(Path(la) / "Programs" / "Upscayl" / "resources")
    candidates.append(Path("C:/Program Files/Upscayl/resources"))
    for res in candidates:
        exe = res / "bin" / "upscayl-bin.exe"
        models = res / "models"
        if exe.exists() and models.exists():
            return str(exe), str(models)
    return "", ""


def find_default_font() -> tuple[str, int]:
    """(폰트 경로, ttc 인덱스). 나눔명조 Bold → 나눔명조 → 바탕 순으로 탐색."""
    font_dirs = []
    la = os.environ.get("LOCALAPPDATA", "")
    if la:
        font_dirs.append(Path(la) / "Microsoft" / "Windows" / "Fonts")
    win = os.environ.get("WINDIR", "C:/Windows")
    font_dirs.append(Path(win) / "Fonts")
    patterns = ["NanumMyeongjo*Bold*.ttf", "NanumMyeongjoB*.ttf",
                "NanumMyeongjo*.ttf"]
    for d in font_dirs:
        for pat in patterns:
            hits = sorted(d.glob(pat)) if d.exists() else []
            if hits:
                return str(hits[0]), 0
    batang = Path(win) / "Fonts" / "batang.ttc"   # 한국어 Windows 기본
    if batang.exists():
        return str(batang), 0
    return "", 0


# 폰트 프리셋·해석기 — 파이프라인과 공유 (검수 페이지 폰트 목록과 단일 소스)
from comic_retype_pipeline import (FONT_PRESETS, HAND_PRESETS,
                                   resolve_presets)


def find_default_hand_font() -> str:
    """나눔손글씨(붓/펜) 자동 탐색 — 없으면 빈 문자열."""
    font_dirs = []
    la = os.environ.get("LOCALAPPDATA", "")
    if la:
        font_dirs.append(Path(la) / "Microsoft" / "Windows" / "Fonts")
    font_dirs.append(Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts")
    for d in font_dirs:
        for pat in ["NanumBrush*.ttf", "NanumPen*.ttf", "*손글씨*.ttf"]:
            hits = sorted(d.glob(pat)) if d.exists() else []
            if hits:
                return str(hits[0])
    return ""


# ---------------------------------------------------------------------------
# 원본 유사 폰트 자동 매칭 — 획 굵기/대비 특성 비교
# ---------------------------------------------------------------------------
def _stroke_features(mask: np.ndarray) -> tuple[float, float] | None:
    """글자 마스크의 (상대 획 굵기, 굵기 대비). 명조=대비 큼, 고딕=균일."""
    m8 = (mask > 0).astype(np.uint8)
    n, _, st, _ = cv2.connectedComponentsWithStats(m8, connectivity=8)
    hs = [st[i, cv2.CC_STAT_HEIGHT] for i in range(1, n)
          if st[i, cv2.CC_STAT_AREA] >= 12]
    if not hs:
        return None
    med_h = float(np.median(hs))
    dist = cv2.distanceTransform(m8, cv2.DIST_L2, 3)
    vals = dist[m8 > 0]
    if vals.size < 80 or med_h < 6:
        return None
    t = float(vals.mean())
    return t / med_h, float(vals.std() / max(t, 1e-6))


def _font_features(path: str, index: int) -> tuple[float, float] | None:
    from PIL import Image as PImage, ImageDraw, ImageFont
    try:
        f = ImageFont.truetype(path, 48, index=index)
    except OSError:
        return None
    im = PImage.new("L", (760, 90), 255)
    ImageDraw.Draw(im).text((6, 12), "한글의만화믿음별훈민정음국봉", font=f, fill=0)
    return _stroke_features((np.array(im) < 128).astype(np.uint8) * 255)


def auto_match_font(img_path: Path, log) -> tuple[str, int, str]:
    """페이지의 글자 특성과 가장 비슷한 설치 폰트를 (경로, 인덱스, 이름)로."""
    img = retype.imread_unicode(Path(img_path))
    # 분석용 복원은 획 보강(thicken) 없이 — 원본 굵기를 그대로 측정
    restored, _ = retype.restore_page(img, thicken=0.0)
    rg = cv2.cvtColor(restored, cv2.COLOR_BGR2GRAY)
    # 흰 포켓 안 글자 픽셀 추출 (파이프라인 감지와 동일 기준의 축약판)
    dark = (rg < 100).astype(np.uint8)
    white_closed = cv2.morphologyEx(
        (rg > 215).astype(np.uint8) * 255, cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (35, 35)))
    n, lab, st, _ = cv2.connectedComponentsWithStats(dark, connectivity=8)
    letters = np.zeros_like(dark)
    for i in range(1, n):
        a, w, h = (st[i, cv2.CC_STAT_AREA], st[i, cv2.CC_STAT_WIDTH],
                   st[i, cv2.CC_STAT_HEIGHT])
        if 12 <= a <= 2500 and w <= 70 and h <= 70 \
                and a / float(max(w * h, 1)) >= 0.15:
            comp = (lab == i)
            if white_closed[comp].mean() >= 0.85 * 255:
                letters[comp] = 1
    feats = _stroke_features(letters * 255)
    if feats is None:
        raise RuntimeError("분석할 글자를 찾지 못함")
    rel0, con0 = feats

    ranked = []
    for label, path, idx in resolve_presets(FONT_PRESETS):
        ff = _font_features(path, idx)
        if ff is None:
            continue
        d = abs(ff[0] - rel0) * 3.0 + abs(ff[1] - con0)
        ranked.append((d, label, path, idx))
    if not ranked:
        raise RuntimeError("비교할 설치 폰트가 없음")
    ranked.sort()
    log(f"원본 글자 특성: 굵기 {rel0:.3f}, 대비 {con0:.3f}")
    for d, label, _, _ in ranked[:3]:
        log(f"  후보: {label} (차이 {d:.3f})")
    _, label, path, idx = ranked[0]
    return path, idx, label


def list_models(models_dir: str) -> list[str]:
    try:
        names = {p.stem.replace(".param", "")
                 for p in Path(models_dir).glob("*.param")}
        return sorted(names) or ["digital-art-4x"]
    except OSError:
        return ["digital-art-4x"]


# ---------------------------------------------------------------------------
# 작업 실행 (GUI와 분리된 코어 — 취소 가능)
# ---------------------------------------------------------------------------
class Cancelled(Exception):
    pass


def make_args(cfg: dict) -> Namespace:
    """cfg → 파이프라인 인자. run_job의 Namespace와 같은 규칙 (검수 반영용).

    폰트 자동 매칭(font_auto_match)은 전체 실행 전용이라 여기선 미적용."""
    font_path = (cfg.get("font") or "").strip()
    font_index = int(cfg.get("font_index", 0))
    if not font_path or not Path(font_path).exists():
        auto, auto_idx = find_default_font()
        if auto:
            font_path, font_index = auto, auto_idx
    return Namespace(
        font=font_path, font_index=font_index,
        retype_hand=bool(cfg.get("retype_hand", False)),
        hand_font=cfg.get("hand_font") or None,
        hand_font_index=int(cfg.get("hand_font_index", 0)),
        model=cfg.get("claude_model", "claude-sonnet-4-5"),
        strict=True, export_crops=False, no_psd=bool(cfg.get("no_psd", False)),
        debug=bool(cfg.get("debug", True)),
        text_black=int(cfg.get("text_black", 80)),
        text_white=int(cfg.get("text_white", 210)),
        thicken=float(cfg.get("thicken", 0.5)),
        paper=int(cfg.get("paper", 215)),
        no_denoise=bool(cfg.get("no_denoise", False)),
        preserve_bg=bool(cfg.get("preserve_bg", True)),
        ocr_engine=cfg.get("ocr_engine", "claude"),
        render_cache=bool(cfg.get("render_cache", False)),
    )


def run_job(cfg: dict, log, is_cancelled) -> None:
    """cfg 설정으로 전체 파이프라인 실행. log(str) 콜백, is_cancelled() 확인."""
    src = Path(cfg["src"])
    out = Path(cfg["out"])
    out.mkdir(parents=True, exist_ok=True)

    files = sorted(p for p in src.iterdir() if p.suffix.lower() in IMG_EXTS)
    if not files:
        raise RuntimeError(f"원본 폴더에 이미지가 없습니다: {src}")
    if cfg.get("sample_index"):           # 샘플 미리보기: 해당 1장만
        n = max(1, min(int(cfg["sample_index"]), len(files)))
        files = files[n - 1: n]
        log(f"샘플 처리: {files[0].name} ({n}번째)")
    elif cfg.get("limit"):
        files = files[: int(cfg["limit"])]
        log(f"이미지 {len(files)}장 처리 (테스트 제한)")
    else:
        log(f"이미지 {len(files)}장 발견")

    # ---- 1단계: Upscayl 업스케일 ----
    if cfg.get("skip_upscale"):
        log("업스케일 건너뜀 (원본을 그대로 재조판 입력으로 사용)")
        up_dir = src
        up_files = files
    else:
        exe, models_dir = cfg["upscayl_exe"], cfg["upscayl_models"]
        if not Path(exe).exists():
            raise RuntimeError(f"upscayl-bin.exe 를 찾을 수 없습니다: {exe}")
        model = cfg.get("upscayl_model", "digital-art-4x")
        out_scale = float(cfg.get("out_scale", 2))
        up_dir = out / "_upscaled"
        up_dir.mkdir(exist_ok=True)
        log(f"업스케일 시작 — 모델 {model}, 최종 {out_scale:g}x")
        up_files = []
        for i, f in enumerate(files, 1):
            if is_cancelled():
                raise Cancelled()
            dst = up_dir / f"{f.stem}.png"
            up_files.append(dst)
            if dst.exists():
                log(f"  [{i}/{len(files)}] {f.name} — 이미 있음, 건너뜀")
                continue
            log(f"  [{i}/{len(files)}] {f.name} 업스케일 중…")
            tmp = up_dir / f"__tmp_{f.stem}.png"
            cmd = [exe, "-i", str(f), "-o", str(tmp),
                   "-n", model, "-m", models_dir, "-f", "png"]
            # upscayl-bin 출력은 UTF-8 — 한국어 Windows 기본 cp949로 읽으면
            # UnicodeDecodeError (reader thread 크래시)
            r = subprocess.run(cmd, capture_output=True, text=True,
                               encoding="utf-8", errors="replace",
                               creationflags=getattr(subprocess,
                                                     "CREATE_NO_WINDOW", 0))
            if r.returncode != 0 or not tmp.exists():
                raise RuntimeError(
                    f"Upscayl 실패: {f.name}\n{r.stderr[-400:]}")
            # 모델 배율(보통 4x) 결과를 최종 배율로 리사이즈
            src_img = retype.imread_unicode(f)
            up_img = retype.imread_unicode(tmp)
            tw = int(src_img.shape[1] * out_scale)
            th = int(src_img.shape[0] * out_scale)
            if (up_img.shape[1], up_img.shape[0]) != (tw, th):
                interp = (cv2.INTER_AREA
                          if up_img.shape[1] > tw else cv2.INTER_CUBIC)
                up_img = cv2.resize(up_img, (tw, th), interpolation=interp)
            retype.imwrite_unicode(dst, up_img)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        log("업스케일 완료")

    # ---- 2단계: 한글 재조판 (v3) ----
    if cfg.get("api_key"):
        os.environ["ANTHROPIC_API_KEY"] = cfg["api_key"].strip()
    if not cfg.get("transcript") \
            and cfg.get("ocr_engine", "claude") == "claude" \
            and not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY 가 없습니다. 앱에 입력하거나 "
                           "전사 엔진을 로컬 OCR로 바꾸세요.")

    # 폰트 결정: ① 원본 유사 자동 매칭 → ② 지정 경로 → ③ 자동 감지
    font_path = (cfg.get("font") or "").strip()
    font_index = int(cfg.get("font_index", 0))
    if cfg.get("font_auto_match"):
        try:
            probe = up_files[min(2, len(up_files) - 1)]  # 표지 회피
            font_path, font_index, label = auto_match_font(probe, log)
            log(f"원본 유사 폰트 자동 매칭: {label}")
        except Exception as e:
            log(f"폰트 자동 매칭 실패({e}) — 기본 자동 감지로 진행")
            font_path = ""
    if not font_path or not Path(font_path).exists():
        auto, auto_idx = find_default_font()
        if auto:
            font_path, font_index = auto, auto_idx
            log(f"폰트 자동 선택: {Path(auto).name}")
    args = Namespace(
        font=font_path, font_index=font_index,
        retype_hand=bool(cfg.get("retype_hand", False)),
        hand_font=cfg.get("hand_font") or None,
        hand_font_index=int(cfg.get("hand_font_index", 0)),
        model=cfg.get("claude_model", "claude-sonnet-4-5"),
        strict=True, export_crops=False, no_psd=bool(cfg.get("no_psd", False)),
        debug=bool(cfg.get("debug", True)),
        text_black=int(cfg.get("text_black", 80)),
        text_white=int(cfg.get("text_white", 210)),
        thicken=float(cfg.get("thicken", 0.5)),
        paper=int(cfg.get("paper", 215)),
        no_denoise=bool(cfg.get("no_denoise", False)),
        preserve_bg=bool(cfg.get("preserve_bg", True)),
        ocr_engine=cfg.get("ocr_engine", "claude"),
        render_cache=bool(cfg.get("render_cache", False)),
    )
    if not args.font or not Path(args.font).exists():
        raise RuntimeError(
            "본문 폰트를 찾을 수 없습니다. 나눔명조 Bold를 설치하거나 "
            "폰트 경로를 직접 지정하세요.")

    # 테스트/오프라인용: transcript 파일이 지정되면 API 대신 사용
    transcript = None
    if cfg.get("transcript"):
        entries = json.loads(Path(cfg["transcript"]).read_text(encoding="utf-8"))
        transcript = {}
        for e in entries:
            transcript.setdefault(e["page"], []).append(e)

    # 이어하기: 완료본(_final.png)이 있는 페이지는 건너뜀 — 크레딧 절약.
    # 샘플 모드는 설정을 바꿔가며 재실행하는 용도라 항상 다시 처리.
    resume = bool(cfg.get("resume", True)) and not cfg.get("sample_index")
    locked = retype.load_locked(out)   # 수동 확정 페이지 — 절대 재처리 안 함

    # Batch API 전사 (비용 50% 할인) — 전 페이지 크롭 수집 후 한 번에 제출.
    # 샘플 모드는 즉시 결과가 필요하므로 실시간 API 사용.
    if (transcript is None and cfg.get("use_batch", True)
            and cfg.get("ocr_engine", "claude") == "claude"
            and not cfg.get("sample_index")):
        todo = [f for f in up_files if f.name not in locked
                and not (resume and (out / f"{f.stem}_final.png").exists())]
        if todo:
            log("배치 전사 준비 — 말풍선 감지 중…")
            pages = []
            for f in todo:
                if is_cancelled():
                    raise Cancelled()
                crops = retype.prepare_crops(f, args)
                pages.append((f.name, crops))
                log(f"  {f.name}: 말풍선 {len(crops)}개")
            log("배치 제출 — 50% 할인 요금 적용, 완료까지 대기합니다 "
                "(보통 수 분, 최대 24시간)")
            try:
                transcript = retype.transcribe_batch(
                    pages, args.model, log=log, is_cancelled=is_cancelled,
                    fast=bool(cfg.get("fast_transcribe", False)))
            except retype.BatchCancelled:
                raise Cancelled()
            log("배치 전사 완료")

    log("재조판 시작")
    results = []
    for i, f in enumerate(up_files, 1):
        if is_cancelled():
            raise Cancelled()
        if f.name in locked:
            log(f"  [{i}/{len(up_files)}] {f.name} — 수동 확정 잠금, 건너뜀")
            results.append({"file": f.name, "status": "locked"})
            continue
        if resume and (out / f"{f.stem}_final.png").exists():
            log(f"  [{i}/{len(up_files)}] {f.name} — 완료본 있음, 건너뜀")
            results.append({"file": f.name, "status": "skipped"})
            continue
        log(f"  [{i}/{len(up_files)}] {f.name}")
        try:
            r = retype.process_page(f, out, args, transcript)
        except Exception as e:
            r = {"file": f.name, "status": "error", "error": str(e),
                 "trace": traceback.format_exc()}
            results.append(r)
            if "credit balance" in str(e).lower():
                log("!! API 크레딧 소진 — 남은 페이지를 중단합니다.")
                log("   충전: https://console.anthropic.com/settings/billing")
                log("   (API 키 입력칸 아래 파란 링크로도 열 수 있습니다)")
                log("   충전 후 다시 [전체 시작]하면 완료된 페이지는 "
                    "건너뛰고 이어서 처리합니다.")
                break
            log(f"    !! 오류: {e}")
            continue
        results.append(r)
        if r.get("status") == "ok":
            msg = (f"    -> 말풍선 {r['bubbles']}개, 재조판 {r['retyped']}개")
            if r.get("pos_warnings"):
                msg += f", 위치 확인 필요 {r['pos_warnings']}건"
            log(msg)

    (out / "review.json").write_text(
        json.dumps(retype.merge_review(out, results), ensure_ascii=False,
                   indent=2), encoding="utf-8")
    ok = sum(1 for r in results if r.get("status") == "ok")
    warns = sum(r.get("pos_warnings", 0) for r in results)
    log(f"\n완료 — {ok}/{len(results)}장 성공, 위치 확인 필요 {warns}건")
    log(f"출력 폴더: {out}")
    hp = retype.write_review_html(out)
    if hp:
        log("검수: [검수 페이지] 버튼으로 열어 재작업할 말풍선을 클릭 마킹 → "
            "rework.json 저장 → [검수 반영]")


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
def main() -> int:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root = tk.Tk()
    root.title(f"만화 한글 복원 — 업스케일 + 재조판 v{retype.__version__}")
    root.geometry("760x680")

    cfg0 = {}
    if CONFIG_PATH.exists():
        try:
            cfg0 = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            cfg0 = {}
    auto_exe, auto_models = find_upscayl()

    v = {
        "src": tk.StringVar(value=cfg0.get("src", "")),
        "out": tk.StringVar(value=cfg0.get("out", "")),
        "upscayl_exe": tk.StringVar(value=cfg0.get("upscayl_exe", auto_exe)),
        "upscayl_models": tk.StringVar(
            value=cfg0.get("upscayl_models", auto_models)),
        "upscayl_model": tk.StringVar(
            value=cfg0.get("upscayl_model", "digital-art-4x")),
        "out_scale": tk.StringVar(value=str(cfg0.get("out_scale", 2))),
        "skip_upscale": tk.BooleanVar(value=cfg0.get("skip_upscale", False)),
        "resume": tk.BooleanVar(value=cfg0.get("resume", True)),
        "use_batch": tk.BooleanVar(value=cfg0.get("use_batch", True)),
        "preserve_bg": tk.BooleanVar(value=cfg0.get("preserve_bg", True)),
        "ocr_engine": tk.StringVar(value=next(
            (lb for lb, k in OCR_ENGINES
             if k == cfg0.get("ocr_engine", "claude")), OCR_ENGINES[0][0])),
        "font": tk.StringVar(
            value=cfg0.get("font") or find_default_font()[0]),
        "retype_hand": tk.BooleanVar(value=cfg0.get("retype_hand", False)),
        "hand_font": tk.StringVar(
            value=cfg0.get("hand_font") or find_default_hand_font()),
        "api_key": tk.StringVar(value=cfg0.get("api_key", "")),
        "save_key": tk.BooleanVar(value=bool(cfg0.get("api_key"))),
        "claude_model": tk.StringVar(
            value=cfg0.get("claude_model", "claude-sonnet-4-5")),
        "limit": tk.StringVar(value=str(cfg0.get("limit", 0))),
        "sample_index": tk.StringVar(value=str(cfg0.get("sample_index", 3))),
        "font_index": tk.StringVar(value=str(cfg0.get("font_index", 0))),
        "font_preset": tk.StringVar(value=cfg0.get("font_preset", "자동 감지")),
        "hand_preset": tk.StringVar(value=cfg0.get("hand_preset", "자동 감지")),
    }

    font_presets = resolve_presets(FONT_PRESETS)
    hand_presets = resolve_presets(HAND_PRESETS)

    frm = ttk.Frame(root, padding=10)
    frm.pack(fill="both", expand=True)
    frm.columnconfigure(1, weight=1)
    row = 0

    def add_path(label, key, is_dir=True, ftypes=None):
        nonlocal row
        ttk.Label(frm, text=label).grid(row=row, column=0, sticky="w", pady=2)
        ttk.Entry(frm, textvariable=v[key]).grid(
            row=row, column=1, sticky="ew", padx=4)
        def browse():
            p = (filedialog.askdirectory() if is_dir
                 else filedialog.askopenfilename(filetypes=ftypes or []))
            if p:
                v[key].set(p)
        ttk.Button(frm, text="찾아보기", command=browse).grid(row=row, column=2)
        row += 1

    add_path("원본 폴더", "src")
    add_path("출력 폴더", "out")

    ttk.Separator(frm).grid(row=row, columnspan=3, sticky="ew", pady=6); row += 1
    add_path("upscayl-bin.exe", "upscayl_exe", is_dir=False,
             ftypes=[("exe", "*.exe")])
    add_path("Upscayl models 폴더", "upscayl_models")

    ttk.Label(frm, text="업스케일 모델").grid(row=row, column=0, sticky="w")
    model_cb = ttk.Combobox(frm, textvariable=v["upscayl_model"],
                            values=list_models(v["upscayl_models"].get()))
    model_cb.grid(row=row, column=1, sticky="w", padx=4); row += 1
    ttk.Label(frm, text="최종 배율").grid(row=row, column=0, sticky="w")
    ttk.Combobox(frm, textvariable=v["out_scale"], values=["1", "2", "3", "4"],
                 width=6).grid(row=row, column=1, sticky="w", padx=4)
    ttk.Checkbutton(frm, text="업스케일 건너뛰기 (이미 업스케일된 폴더)",
                    variable=v["skip_upscale"]).grid(
        row=row, column=1, sticky="e"); row += 1
    ttk.Checkbutton(frm, text="완료된 페이지 건너뛰기 (이어하기 — 크레딧 절약)",
                    variable=v["resume"]).grid(
        row=row, column=1, sticky="w"); row += 1
    ttk.Checkbutton(frm, text="Batch API 전사 (50% 할인 — 완료까지 대기, "
                              "샘플엔 미적용)",
                    variable=v["use_batch"]).grid(
        row=row, column=1, sticky="w"); row += 1
    ttk.Checkbutton(frm, text="원본 화질 100% 보존 (재조판 말풍선만 수정)",
                    variable=v["preserve_bg"]).grid(
        row=row, column=1, sticky="w"); row += 1

    ttk.Separator(frm).grid(row=row, columnspan=3, sticky="ew", pady=6); row += 1

    # ---- 본문 폰트 프리셋 ----
    ttk.Label(frm, text="본문 폰트 프리셋").grid(row=row, column=0, sticky="w")
    fp_cb = ttk.Combobox(
        frm, textvariable=v["font_preset"], state="readonly",
        values=["자동 매칭 (원본과 유사한 폰트)", "자동 감지"]
               + [p[0] for p in font_presets] + ["직접 지정"])
    fp_cb.grid(row=row, column=1, sticky="ew", padx=4); row += 1

    def on_font_preset(_=None):
        sel = v["font_preset"].get()
        if sel == "자동 감지":
            p, i = find_default_font()
            v["font"].set(p)
            v["font_index"].set(str(i))
        else:
            for label, path, idx in font_presets:
                if label == sel:
                    v["font"].set(path)
                    v["font_index"].set(str(idx))
                    break
    fp_cb.bind("<<ComboboxSelected>>", on_font_preset)

    add_path("본문 폰트 경로", "font", is_dir=False,
             ftypes=[("font", "*.ttf *.otf *.ttc")])

    # ---- 손글씨 폰트 프리셋 ----
    ttk.Checkbutton(frm, text="손글씨 대사도 재조판 (기본 꺼짐)",
                    variable=v["retype_hand"]).grid(
        row=row, column=1, sticky="w"); row += 1
    ttk.Label(frm, text="손글씨 폰트 프리셋").grid(row=row, column=0, sticky="w")
    hp_cb = ttk.Combobox(
        frm, textvariable=v["hand_preset"], state="readonly",
        values=["자동 감지"] + [p[0] for p in hand_presets] + ["직접 지정"])
    hp_cb.grid(row=row, column=1, sticky="ew", padx=4); row += 1

    def on_hand_preset(_=None):
        sel = v["hand_preset"].get()
        if sel == "자동 감지":
            v["hand_font"].set(find_default_hand_font())
        else:
            for label, path, idx in hand_presets:
                if label == sel:
                    v["hand_font"].set(path)
                    break
    hp_cb.bind("<<ComboboxSelected>>", on_hand_preset)

    add_path("손글씨 폰트 경로", "hand_font", is_dir=False,
             ftypes=[("font", "*.ttf *.otf")])

    ttk.Separator(frm).grid(row=row, columnspan=3, sticky="ew", pady=6); row += 1
    ttk.Label(frm, text="ANTHROPIC_API_KEY").grid(row=row, column=0, sticky="w")
    ttk.Entry(frm, textvariable=v["api_key"], show="*").grid(
        row=row, column=1, sticky="ew", padx=4)
    ttk.Checkbutton(frm, text="키 저장", variable=v["save_key"]).grid(
        row=row, column=2); row += 1

    BILLING_URL = "https://console.anthropic.com/settings/billing"

    def open_billing(_=None):
        import webbrowser
        webbrowser.open(BILLING_URL)

    link = ttk.Label(frm, text="크레딧 잔액 확인·충전 (Plans & Billing 열기)",
                     foreground="#0b5ed7", cursor="hand2")
    link.grid(row=row, column=1, sticky="w", padx=4); row += 1
    link.bind("<Button-1>", open_billing)

    ttk.Label(frm, text="전사 엔진").grid(row=row, column=0, sticky="w")
    ttk.Combobox(frm, textvariable=v["ocr_engine"], state="readonly",
                 values=[lb for lb, _ in OCR_ENGINES]).grid(
        row=row, column=1, sticky="ew", padx=4)
    row += 1
    ttk.Label(frm, text="Claude 모델").grid(row=row, column=0, sticky="w")
    ttk.Combobox(frm, textvariable=v["claude_model"],
                 values=["claude-sonnet-4-5", "claude-haiku-4-5"]).grid(
        row=row, column=1, sticky="w", padx=4)
    ttk.Label(frm, text="테스트 장수(0=전체)").grid(row=row, column=2, sticky="e")
    row += 1
    ttk.Entry(frm, textvariable=v["limit"], width=8).grid(
        row=row - 1, column=2, sticky="w")

    log_box = tk.Text(frm, height=16, state="disabled")
    log_box.grid(row=row + 1, column=0, columnspan=3, sticky="nsew", pady=6)
    frm.rowconfigure(row + 1, weight=1)

    q: queue.Queue = queue.Queue()
    state = {"running": False, "cancel": False}

    def log(msg: str) -> None:
        q.put(msg)

    def open_preview(final_png: str, before_png: str,
                     cache_json: str = "", job_cfg: dict = None) -> None:
        """샘플 미리보기 — 원본/결과 토글 + 실시간 폰트 교체."""
        from PIL import Image as PImage, ImageTk

        win = tk.Toplevel(root)
        win.title("샘플 미리보기 — 클릭/스페이스: 원본↔결과 전환")
        max_w, max_h = 980, 700

        def to_photo(im: "PImage.Image"):
            s = min(max_w / im.width, max_h / im.height, 1.0)
            return ImageTk.PhotoImage(
                im.resize((int(im.width * s), int(im.height * s)),
                          PImage.LANCZOS))

        imgs = {}
        for key, p in [("결과", final_png), ("원본", before_png)]:
            if p and Path(p).exists():
                imgs[key] = to_photo(PImage.open(p))

        top = ttk.Frame(win)
        top.pack(fill="x", pady=2)
        lbl_top = ttk.Label(top, text="보정 결과 (클릭하면 원본과 비교)")
        lbl_top.pack(side="left", padx=8)

        # --- 실시간 폰트 교체 (렌더 캐시가 있을 때만) ---
        cache = None
        base_bgr = None
        if cache_json and Path(cache_json).exists():
            cache = json.loads(Path(cache_json).read_text(encoding="utf-8"))
            base_p = Path(cache_json).with_name(
                Path(cache_json).stem + "_base.png")
            if base_p.exists():
                base_bgr = retype.imread_unicode(base_p)

        def rerender(font_path: str, font_index: int) -> None:
            from PIL import ImageDraw
            H, W = base_bgr.shape[:2]
            tp = PImage.new("RGBA", (W, H), (0, 0, 0, 0))
            d = ImageDraw.Draw(tp)
            hand_font = (job_cfg or {}).get("hand_font") or ""
            dummy = np.zeros((1, 1), np.uint8)
            for e in cache["bubbles"]:
                b = retype.Bubble(
                    bbox=tuple(e["bbox"]), mask=dummy, text=e["text"],
                    kind=e.get("kind", "dialogue"),
                    font_cap=e.get("font_cap", 0),
                    line_boxes=[tuple(x) for x in e.get("line_boxes", [])])
                if b.kind == "hand" and hand_font:
                    fp, fi, stroke = hand_font, 0, 1
                else:
                    fp, fi, stroke = font_path, font_index, 0
                if not retype.render_line_matched(d, b, fp, fi, off=(0, 0),
                                                  stroke=stroke):
                    b.text = (b.text or "").replace("\n", " ")
                    retype.render_text(d, b, fp, fi, off=(0, 0))
            arr = np.array(tp)
            a = arr[..., 3:4].astype(np.float32) / 255
            rgb = arr[..., :3][:, :, ::-1]
            outv = (base_bgr * (1 - a) + rgb * a).astype(np.uint8)
            imgs["결과"] = to_photo(PImage.fromarray(outv[:, :, ::-1]))
            state_p["cur"] = "결과"
            lbl.configure(image=imgs["결과"])
            lbl_top.configure(text="보정 결과 (클릭하면 원본과 비교)")

        if cache is not None and base_bgr is not None:
            presets = resolve_presets(FONT_PRESETS)
            pv = tk.StringVar(value="(현재 설정)")
            ttk.Label(top, text="   폰트:").pack(side="left")
            pcb = ttk.Combobox(top, textvariable=pv, state="readonly",
                               width=34,
                               values=["(현재 설정)"] + [p[0] for p in presets])
            pcb.pack(side="left", padx=4)

            def on_pick(_=None):
                sel = pv.get()
                for label, path, idx in presets:
                    if label == sel:
                        win.title("렌더링 중…")
                        win.update_idletasks()
                        try:
                            rerender(path, idx)
                        finally:
                            win.title("샘플 미리보기 — 클릭/스페이스: "
                                      "원본↔결과 전환")
                        break
            pcb.bind("<<ComboboxSelected>>", on_pick)

        state_p = {"cur": "결과"}
        lbl = ttk.Label(win, image=imgs.get("결과"))
        lbl.pack()

        def toggle(_=None):
            state_p["cur"] = "원본" if state_p["cur"] == "결과" else "결과"
            if state_p["cur"] in imgs:
                lbl.configure(image=imgs[state_p["cur"]])
                lbl_top.configure(
                    text=("보정 결과 (클릭하면 원본과 비교)"
                          if state_p["cur"] == "결과" else
                          "원본/업스케일본 (클릭하면 결과 보기)"))

        lbl.bind("<Button-1>", toggle)
        win.bind("<space>", toggle)
        win.imgs = imgs  # GC 방지

    def poll():
        try:
            while True:
                m = q.get_nowait()
                if isinstance(m, tuple) and m and m[0] == "PREVIEW":
                    open_preview(*m[1:])
                    continue
                log_box.configure(state="normal")
                log_box.insert("end", str(m) + "\n")
                log_box.see("end")
                log_box.configure(state="disabled")
        except queue.Empty:
            pass
        root.after(150, poll)

    def collect_cfg() -> dict:
        cfg = {k: (var.get() if not isinstance(var, tk.BooleanVar)
                   else bool(var.get())) for k, var in v.items()}
        cfg["limit"] = int(cfg.get("limit") or 0)
        cfg["out_scale"] = float(cfg.get("out_scale") or 2)
        cfg["font_index"] = int(cfg.get("font_index") or 0)
        cfg["font_auto_match"] = str(
            cfg.get("font_preset", "")).startswith("자동 매칭")
        cfg["ocr_engine"] = next((k for lb, k in OCR_ENGINES
                                  if lb == cfg.get("ocr_engine")), "claude")
        cfg["sample_index"] = 0   # 전체 실행 기본값 — 샘플 버튼에서만 설정
        return cfg

    def save_cfg(cfg: dict) -> None:
        c = dict(cfg)
        if not cfg.get("save_key"):
            c["api_key"] = ""
        CONFIG_PATH.write_text(json.dumps(c, ensure_ascii=False, indent=2),
                               encoding="utf-8")

    def worker(cfg: dict) -> None:
        try:
            run_job(cfg, log, lambda: state["cancel"])
            log("=== 작업 종료 ===")
        except Cancelled:
            log("=== 사용자가 중지함 ===")
        except Exception as e:
            log(f"!! 실패: {e}")
        finally:
            state["running"] = False

    def resolve_out(cfg: dict) -> bool:
        """출력 폴더 미지정 시 원본 폴더 아래 '복원출력' 하위폴더로 자동 설정."""
        if not cfg["src"]:
            messagebox.showwarning("확인", "원본 폴더를 지정하세요.")
            return False
        if not cfg["out"]:
            cfg["out"] = str(Path(cfg["src"]) / "복원출력")
            v["out"].set(cfg["out"])
        return True

    def start():
        if state["running"]:
            return
        cfg = collect_cfg()
        if not resolve_out(cfg):
            return
        save_cfg(cfg)
        state["running"], state["cancel"] = True, False
        threading.Thread(target=worker, args=(cfg,), daemon=True).start()

    def sample_worker(cfg: dict) -> None:
        try:
            run_job(cfg, log, lambda: state["cancel"])
            out = Path(cfg["out"])
            finals = sorted(out.glob("*_final.png"))
            if finals:
                stem = finals[0].name.replace("_final.png", "")
                before = out / "_upscaled" / f"{stem}.png"
                if not before.exists():
                    cand = [p for p in Path(cfg["src"]).iterdir()
                            if p.stem == stem]
                    before = cand[0] if cand else finals[0]
                cache = out / "_cache" / f"{stem}.json"
                q.put(("PREVIEW", str(finals[0]), str(before),
                       str(cache) if cache.exists() else "", dict(cfg)))
                log("=== 샘플 완료 — 미리보기 창을 확인하세요 ===")
                log("결과가 좋으면 [전체 시작], 아니면 설정 조정 후 다시 샘플.")
            else:
                log("샘플 결과 파일이 없습니다 — 로그를 확인하세요.")
        except Cancelled:
            log("=== 사용자가 중지함 ===")
        except Exception as e:
            log(f"!! 실패: {e}")
        finally:
            state["running"] = False

    def sample():
        if state["running"]:
            return
        cfg = collect_cfg()
        if not resolve_out(cfg):
            return
        try:
            idx = max(1, int(v["sample_index"].get() or 1))
        except ValueError:
            idx = 1
        cfg["sample_index"] = idx
        cfg["out"] = str(Path(cfg["out"]) / "_sample")
        cfg["render_cache"] = True   # 미리보기 실시간 폰트 교체용
        save_cfg(collect_cfg())
        state["running"], state["cancel"] = True, False
        threading.Thread(target=sample_worker, args=(cfg,),
                         daemon=True).start()

    def stop():
        state["cancel"] = True

    def _out_dir() -> Path | None:
        outp = v["out"].get().strip()
        if not outp and v["src"].get().strip():
            outp = str(Path(v["src"].get()) / "복원출력")
        return Path(outp) if outp else None

    def _ensure_server() -> str | None:
        """검수 서버 시작(1회) — 검수 페이지에서 [이 페이지 적용] 즉시 반영용."""
        if state.get("server_url"):
            return state["server_url"]
        cfg = collect_cfg()
        if not resolve_out(cfg):
            return None
        out = Path(cfg["out"])
        try:
            # 열 때마다 재생성 — 재조판 없이 최신 템플릿·데이터 반영
            retype.write_review_html(out)
        except Exception as e:
            log(f"!! 검수 페이지 재생성 실패: {e}")
        if not (out / "review.html").exists():
            messagebox.showinfo(
                "확인", "검수 페이지가 없습니다. 먼저 [전체 시작]으로 "
                "작업을 실행하세요.")
            return None
        if cfg.get("api_key"):
            os.environ["ANTHROPIC_API_KEY"] = cfg["api_key"].strip()
        state["server_cfg"] = cfg   # 서버는 시작 시점 설정 사용
        import functools
        from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
        app_state = state

        class Handler(SimpleHTTPRequestHandler):
            def log_message(self, *a):   # 콘솔 소음 제거
                pass

            def end_headers(self):
                # 적용 직후 옛 이미지가 보이는 캐시 문제 방지
                self.send_header("Cache-Control", "no-store")
                super().end_headers()

            def do_POST(self):
                if self.path.split("?")[0] != "/api/rework":
                    self.send_error(404)
                    return
                if app_state["running"]:
                    self.send_error(409, "busy")
                    return
                app_state["running"] = True
                try:
                    n = int(self.headers.get("Content-Length", 0))
                    acts = json.loads(self.rfile.read(n).decode("utf-8"))
                    cfgS = app_state["server_cfg"]
                    args = make_args(cfgS)
                    outp = Path(cfgS["out"])
                    up = outp / "_upscaled"
                    pages_dir = up if up.exists() else Path(cfgS["src"])
                    log(f"검수 서버: {len(acts)}건 적용 중…")
                    done = retype.apply_rework(outp, acts, args, pages_dir,
                                               log=log)
                    log(f"검수 서버: 적용 완료 — {done}페이지")
                    body = json.dumps({"done": done}).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except Exception as e:
                    log(f"!! 검수 서버 오류: {e}")
                    try:
                        self.send_error(500, "rework failed")
                    except Exception:
                        pass
                finally:
                    app_state["running"] = False

        try:
            srv = ThreadingHTTPServer(
                ("127.0.0.1", 0),
                functools.partial(Handler, directory=str(out)))
        except Exception as e:
            log(f"!! 검수 서버 시작 실패({e}) — 파일로 엽니다")
            return None
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{srv.server_address[1]}"
        state["server_url"] = url
        log(f"검수 서버 시작: {url} (설정 변경 시 앱 재시작 후 다시 여세요)")
        return url

    def open_review():
        import webbrowser
        url = _ensure_server()
        if url:
            webbrowser.open(url + "/review.html")
            return
        p = _out_dir()
        p = p / "review.html" if p else None
        if p and p.exists():   # 서버 실패 시 파일로 폴백 (적용 버튼 비활성)
            webbrowser.open(p.resolve().as_uri())

    def rework_worker(cfg: dict, fp: str) -> None:
        try:
            if cfg.get("api_key"):
                os.environ["ANTHROPIC_API_KEY"] = cfg["api_key"].strip()
            args = make_args(cfg)
            if not args.font or not Path(args.font).exists():
                raise RuntimeError("본문 폰트를 찾을 수 없습니다.")
            out = Path(cfg["out"])
            up = out / "_upscaled"
            pages_dir = up if up.exists() else Path(cfg["src"])
            log("검수 반영 시작")
            n = retype.apply_rework(out, Path(fp), args, pages_dir, log=log)
            log(f"=== 검수 반영 완료 — {n}페이지 재조판 ===")
            log("검수 페이지가 갱신됐습니다. [검수 페이지]로 다시 확인하세요.")
        except Exception as e:
            log(f"!! 실패: {e}")
        finally:
            state["running"] = False

    def rework():
        if state["running"]:
            return
        cfg = collect_cfg()
        if not resolve_out(cfg):
            return
        fp = filedialog.askopenfilename(
            title="rework.json 선택 (검수 페이지에서 저장한 파일)",
            filetypes=[("JSON", "*.json")],
            initialdir=str(Path.home() / "Downloads"))
        if not fp:
            return
        save_cfg(cfg)
        state["running"], state["cancel"] = True, False
        threading.Thread(target=rework_worker, args=(cfg, fp),
                         daemon=True).start()

    btns = ttk.Frame(frm)
    btns.grid(row=row, column=0, columnspan=3, pady=4)
    ttk.Button(btns, text="샘플 미리보기", command=sample).pack(
        side="left", padx=6)
    ttk.Label(btns, text="샘플 번호:").pack(side="left")
    ttk.Entry(btns, textvariable=v["sample_index"], width=4).pack(
        side="left", padx=(0, 12))
    ttk.Button(btns, text="전체 시작", command=start).pack(side="left", padx=6)
    ttk.Button(btns, text="검수 페이지", command=open_review).pack(
        side="left", padx=6)
    ttk.Button(btns, text="검수 반영", command=rework).pack(side="left", padx=6)
    ttk.Button(btns, text="중지", command=stop).pack(side="left", padx=6)

    poll()
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
