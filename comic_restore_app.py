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
import re
import subprocess
import sys
import threading
import time
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
    ("Claude AI (권장 — 정확, 합의 검증, API 크레딧 사용)", "claude"),
    ("Gemini 비전 (저렴 — Google API 키, 1회 전사·합의 없음)", "gemini"),
    ("DeepSeek 비전 (초저가 — OpenAI 호환·URL 교체 가능)", "deepseek"),
    ("Windows OCR (무료 — winocr 설치·한국어 언어팩 필요)", "windows"),
    ("Tesseract (무료 — 본체+kor 데이터 설치 필요)", "tesseract"),
    ("EasyOCR (무료 — easyocr 설치, 최초 실행 시 모델 다운로드)", "easyocr"),
]

# 원서 언어 — (표시명, 내부 키). ko 외 선택 시 한글 번역 모드
SRC_LANGS = [
    ("한국어 복원 (기본 — 열화된 한글 재조판)", "ko"),
    ("독일어 → 한글 번역", "de"),
    ("영어 → 한글 번역", "en"),
    ("일본어 → 한글 번역", "ja"),
]

# 번역 방식 — (표시명, 내부 키)
XLAT_MODES = [
    ("Claude 비전 — 이미지에서 원문 전사+번역 한 번에 (정확·권장)",
     "vision"),
    ("로컬 OCR + 텍스트 번역 — 원문은 로컬 추출, 번역만 API (저렴)",
     "local-ocr"),
]

# 텍스트 번역 엔진 — (표시명, 내부 키). local-ocr 방식에서만 쓰임
XLAT_BACKENDS = [
    ("Claude API (권장 — 자연스러움·용어집 준수 좋음)", "claude"),
    ("Gemini API (초저가 — Google API 키 필요)", "gemini"),
    ("Kimi API (저가·번역 품질 좋음 — Moonshot API 키 필요)", "kimi"),
    ("Ollama 로컬 (무료·오프라인 — API 키 불필요, 품질은 검수 전제)",
     "ollama"),
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


def find_default_sfx_font() -> str:
    """효과음용 기본 폰트 — 검은고딕 > 에스코어 8 Heavy > 도현 > 을지로."""
    pats = ["BlackHanSans*.ttf", "검은고딕*.ttf", "에스코어 드림 8*.ttf",
            "S-CoreDream*8*.ttf", "BMDOHYEON*.ttf", "BMEULJIRO*.ttf"]
    for d in retype._font_dirs():
        for pat in pats:
            hits = sorted(d.glob(pat)) if d.exists() else []
            if hits:
                return str(hits[0])
    return ""


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
# 시스템 리소스 측정 — 실행 중 CPU/GPU 사용량 표시용 (Ollama 로컬 번역 등)
# ---------------------------------------------------------------------------
def _cpu_sampler():
    """CPU 사용률(%) 샘플러 — 직전 호출 이후 구간 평균. 외부 패키지 불필요.

    Windows는 ctypes GetSystemTimes, 그 외는 /proc/stat. 반환 함수는
    실패 시 None."""
    if os.name == "nt":
        import ctypes

        class _FT(ctypes.Structure):
            _fields_ = [("lo", ctypes.c_uint32), ("hi", ctypes.c_uint32)]

        k32 = ctypes.windll.kernel32

        def _read():
            idle, kern, user = _FT(), _FT(), _FT()
            if not k32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern),
                                      ctypes.byref(user)):
                return None
            ft = lambda t: (t.hi << 32) | t.lo
            # kernel 시간에는 idle이 포함됨 → 전체 = kernel + user
            return ft(idle), ft(kern) + ft(user)
    else:
        def _read():
            try:
                with open("/proc/stat") as f:
                    v = [int(x) for x in f.readline().split()[1:9]]
                return v[3] + v[4], sum(v)
            except Exception:
                return None

    prev = _read()

    def sample():
        nonlocal prev
        cur = _read()
        if not cur or not prev:
            prev = cur
            return None
        d_idle, d_total = cur[0] - prev[0], cur[1] - prev[1]
        prev = cur
        if d_total <= 0:
            return None
        return max(0.0, min(100.0, (1 - d_idle / d_total) * 100))
    return sample


_NVSMI: str | None = None


def _gpu_sample():
    """NVIDIA GPU — (사용률%, VRAM 사용 MB, VRAM 전체 MB) 또는 None."""
    global _NVSMI
    if _NVSMI is None:
        import shutil
        _NVSMI = shutil.which("nvidia-smi") or ""
    if not _NVSMI:
        return None
    try:
        r = subprocess.run(
            [_NVSMI, "--query-gpu=utilization.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        lines = (r.stdout or "").strip().splitlines()
        if r.returncode != 0 or not lines:
            return None
        u, mu, mt = [float(x) for x in lines[0].split(",")[:3]]
        return u, mu, mt
    except Exception:
        return None


def _ollama_gpu_load(url: str):
    """Ollama /api/ps — 적재된 모델의 GPU 오프로드 비율(%). 실패 시 None."""
    import urllib.request
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/ps",
                                    timeout=1.5) as res:
            data = json.loads(res.read().decode("utf-8"))
        models = data.get("models") or []
        if not models or not models[0].get("size"):
            return None
        m = models[0]
        return round((m.get("size_vram") or 0) / m["size"] * 100)
    except Exception:
        return None


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
        caption_font=cfg.get("caption_font") or None,
        caption_font_index=int(cfg.get("caption_font_index", 0) or 0),
        shout_font=cfg.get("shout_font") or None,
        shout_font_index=int(cfg.get("shout_font_index", 0) or 0),
        retype_sfx=bool(cfg.get("retype_sfx", False)),
        sfx_font=cfg.get("sfx_font") or None,
        sfx_font_index=int(cfg.get("sfx_font_index", 0) or 0),
        text_backing=8.0 if cfg.get("text_backing", True) else 0.0,
        erase_fill=6.0 if cfg.get("erase_fill") else 0.0,
        model=cfg.get("claude_model", "claude-sonnet-4-5"),
        strict=True, export_crops=False, no_psd=bool(cfg.get("no_psd", False)),
        debug=bool(cfg.get("debug", True)),
        text_black=int(cfg.get("text_black", 80)),
        text_white=int(cfg.get("text_white", 210)),
        thicken=float(cfg.get("thicken", 0.5)),
        ink_boost=float(cfg.get("ink_boost", 0) or 0),
        paper=int(cfg.get("paper", 215)),
        no_denoise=bool(cfg.get("no_denoise", False)),
        preserve_bg=bool(cfg.get("preserve_bg", True)),
        ocr_engine=cfg.get("ocr_engine", "claude"),
        source_lang=cfg.get("source_lang", "ko"),
        translate_mode=cfg.get("translate_mode", "vision"),
        translate_consensus=bool(cfg.get("translate_consensus", False)),
        glossary=cfg.get("glossary") or None,
        translate_backend=cfg.get("translate_backend", "claude"),
        ollama_model=cfg.get("ollama_model") or None,
        ollama_url=cfg.get("ollama_url") or None,
        gemini_model=cfg.get("gemini_model") or None,
        gemini_key=cfg.get("gemini_api_key") or None,
        deepseek_model=cfg.get("deepseek_model") or None,
        deepseek_key=cfg.get("deepseek_api_key") or None,
        deepseek_url=cfg.get("deepseek_url") or None,
        kimi_model=cfg.get("kimi_model") or None,
        kimi_key=cfg.get("kimi_api_key") or None,
        async_psd=True,   # 앱은 상주하므로 PSD 저장 백그라운드 (응답 단축)
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
    if (cfg.get("page_range") or "").strip():
        total = len(files)
        try:
            files = retype.apply_page_range(files, cfg["page_range"])
        except ValueError as e:
            raise RuntimeError(str(e))
        if not files:
            raise RuntimeError(
                f"페이지 범위 {cfg['page_range']} 에 해당하는 이미지가 "
                f"없습니다 (전체 {total}장)")
        log(f"페이지 범위 {cfg['page_range'].strip()} — 전체 {total}장 중 "
            f"{len(files)}장 처리 (샘플 번호도 범위 안에서 셉니다)")
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
    # Claude 전사·Claude 번역이 있을 때만 API 키 필요 (파이프라인과 공용
    # 판정 — 로컬 OCR + Ollama 번역 조합은 키 없이 완전 로컬 동작)
    if cfg.get("gemini_api_key"):
        os.environ["GEMINI_API_KEY"] = cfg["gemini_api_key"].strip()
    if cfg.get("deepseek_api_key"):
        os.environ["DEEPSEEK_API_KEY"] = cfg["deepseek_api_key"].strip()
    if cfg.get("kimi_api_key"):
        os.environ["MOONSHOT_API_KEY"] = cfg["kimi_api_key"].strip()
    key_ns = Namespace(
        ocr_engine=cfg.get("ocr_engine", "claude"),
        source_lang=cfg.get("source_lang", "ko"),
        translate_mode=cfg.get("translate_mode", "vision"),
        translate_backend=cfg.get("translate_backend", "claude"))
    need_key = retype.needs_api_key(key_ns)
    if not cfg.get("transcript") and not cfg.get("skip_retype") \
            and need_key \
            and not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY 가 없습니다. 앱에 입력하거나 "
                           "전사 엔진을 로컬 OCR로 바꾸세요 (번역 모드는 "
                           "번역 엔진을 Gemini/Ollama로 바꾸면 Anthropic "
                           "키 없이 동작).")
    if not cfg.get("transcript") and not cfg.get("skip_retype") \
            and retype.needs_gemini_key(key_ns) \
            and not retype._gemini_key():
        raise RuntimeError("Gemini API 키가 없습니다 — 앱의 'Gemini API "
                           "키'에 입력하거나 GEMINI_API_KEY 환경변수를 "
                           "설정하세요 (https://aistudio.google.com/apikey "
                           "무료 발급).")
    if not cfg.get("transcript") and not cfg.get("skip_retype") \
            and retype.needs_deepseek_key(key_ns) \
            and not retype._deepseek_key():
        raise RuntimeError("DeepSeek API 키가 없습니다 — 앱의 'DeepSeek "
                           "API 키'에 입력하거나 DEEPSEEK_API_KEY 환경변수를 "
                           "설정하세요 (https://platform.deepseek.com 발급).")
    if not cfg.get("transcript") and not cfg.get("skip_retype") \
            and retype.needs_kimi_key(key_ns) \
            and not retype._kimi_key():
        raise RuntimeError("Kimi API 키가 없습니다 — 앱의 'Kimi API 키'에 "
                           "입력하거나 MOONSHOT_API_KEY 환경변수를 "
                           "설정하세요 (https://platform.moonshot.ai).")

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
        caption_font=cfg.get("caption_font") or None,
        caption_font_index=int(cfg.get("caption_font_index", 0) or 0),
        shout_font=cfg.get("shout_font") or None,
        shout_font_index=int(cfg.get("shout_font_index", 0) or 0),
        retype_sfx=bool(cfg.get("retype_sfx", False)),
        sfx_font=cfg.get("sfx_font") or None,
        sfx_font_index=int(cfg.get("sfx_font_index", 0) or 0),
        text_backing=8.0 if cfg.get("text_backing", True) else 0.0,
        erase_fill=6.0 if cfg.get("erase_fill") else 0.0,
        model=cfg.get("claude_model", "claude-sonnet-4-5"),
        strict=True, export_crops=False, no_psd=bool(cfg.get("no_psd", False)),
        debug=bool(cfg.get("debug", True)),
        text_black=int(cfg.get("text_black", 80)),
        text_white=int(cfg.get("text_white", 210)),
        thicken=float(cfg.get("thicken", 0.5)),
        ink_boost=float(cfg.get("ink_boost", 0) or 0),
        paper=int(cfg.get("paper", 215)),
        no_denoise=bool(cfg.get("no_denoise", False)),
        preserve_bg=bool(cfg.get("preserve_bg", True)),
        ocr_engine=cfg.get("ocr_engine", "claude"),
        source_lang=cfg.get("source_lang", "ko"),
        translate_mode=cfg.get("translate_mode", "vision"),
        translate_consensus=bool(cfg.get("translate_consensus", False)),
        glossary=cfg.get("glossary") or None,
        translate_backend=cfg.get("translate_backend", "claude"),
        ollama_model=cfg.get("ollama_model") or None,
        ollama_url=cfg.get("ollama_url") or None,
        gemini_model=cfg.get("gemini_model") or None,
        gemini_key=cfg.get("gemini_api_key") or None,
        deepseek_model=cfg.get("deepseek_model") or None,
        deepseek_key=cfg.get("deepseek_api_key") or None,
        deepseek_url=cfg.get("deepseek_url") or None,
        kimi_model=cfg.get("kimi_model") or None,
        kimi_key=cfg.get("kimi_api_key") or None,
        async_psd=True,   # 앱은 상주하므로 PSD 저장 백그라운드 (응답 단축)
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

    # 전사·재조판 스킵: 빈 transcript 주입 → 감지·결과 생성만 수행.
    # 이후 검수 페이지에서 필요한 말풍선만 마킹해 선별 전사 (API 비용 0)
    if cfg.get("skip_retype") and transcript is None:
        transcript = {}
        log("전사·재조판 건너뜀 — 감지만 수행 "
            "(검수 페이지에서 말풍선을 마킹해 선별 전사하세요)")

    xl = retype.xlat_cfg(args, out)
    if xl:
        be = xl.get("backend") or "claude"
        be_name = {"claude": "Claude", "gemini": "Gemini",
                   "ollama": "Ollama"}.get(be, be)
        eng = getattr(args, "ocr_engine", "claude")
        if eng == "gemini":
            # Gemini 전사는 translate_mode와 무관하게 항상 비전 경로
            mode = ("Gemini 비전 (전사+번역 한 번에)" if be == "gemini"
                    else f"Gemini 비전 전사 + {be_name} 번역 (분리)")
        elif eng == "deepseek":
            mode = f"DeepSeek 비전 전사 + {be_name} 번역 (분리)"
            if "api.deepseek.com" in (getattr(args, "deepseek_url", "")
                                      or retype.DEEPSEEK_URL):
                log("  !! 주의: 공식 api.deepseek.com은 아직 이미지 입력을 "
                    "지원하지 않습니다 — 거부되면 환경 설정에서 URL을 "
                    "https://api.deepinfra.com/v1/openai (모델 "
                    "deepseek-ai/DeepSeek-OCR, DeepInfra 키)로 바꾸세요.")
        elif getattr(args, "translate_mode", "vision") == "vision":
            mode = "Claude 비전 (전사+번역 한 번에)"
        else:
            mode = f"로컬 OCR + {be_name} 텍스트 번역"
        log(f"번역 모드: {xl['name']} → 한글 — {mode}")
        if xl.get("backend") == "ollama":
            if getattr(args, "translate_mode", "vision") == "local-ocr":
                log(f"  번역 엔진: Ollama 로컬 ({xl['ollama_model']}) — "
                    "API 비용 0, Ollama 실행 중이어야 합니다")
            else:
                log("  참고: Ollama 번역 엔진은 '로컬 OCR + 텍스트 번역' "
                    "방식에서만 쓰입니다 — Claude 비전 방식은 전사+번역이 "
                    "한 요청이라 Claude API 사용")
        if retype._load_glossary(args, out):
            gp = getattr(args, "glossary", None)
            gname = Path(gp).name if gp else "_glossary.txt"
            log(f"  용어집 적용: {gname} (이름 표기·말투 규칙)")
        else:
            log("  용어집 없음 — 용어집 파일을 지정하거나 출력폴더에 "
                "_glossary.txt 를 만들면 인물 이름·말투가 챕터 전체에서 "
                "일관되게 유지됩니다")

    # Batch API 전사 (비용 50% 할인) — 전 페이지 크롭 수집 후 한 번에 제출.
    # 샘플 모드는 즉시 결과가 필요하므로 실시간 API 사용.
    # 번역 local-ocr 모드는 로컬 추출이라 배치 대상 아님.
    if (transcript is None and cfg.get("use_batch", True)
            and cfg.get("ocr_engine", "claude") == "claude"
            and not (xl and args.translate_mode == "local-ocr")
            and not cfg.get("sample_index")):
        todo = [f for f in up_files if f.name not in locked
                and not (resume and (out / f"{f.stem}_final.png").exists())]
        if todo:
            log("배치 전사 준비 — 말풍선 감지 중…")
            pages = []
            for f in todo:
                if is_cancelled():
                    raise Cancelled()
                crops = retype.prepare_crops(f, args, out)
                pages.append((f.name, crops))
                log(f"  {f.name}: 말풍선 {len(crops)}개")
            log("배치 제출 — 50% 할인 요금 적용, 완료까지 대기합니다 "
                "(보통 수 분, 최대 24시간)")
            try:
                transcript = retype.transcribe_batch(
                    pages, args.model, log=log, is_cancelled=is_cancelled,
                    fast=(bool(cfg.get("fast_transcribe", False))
                          or (xl is not None
                              and not args.translate_consensus)),
                    xlat=xl)
            except retype.BatchCancelled:
                raise Cancelled()
            log("배치 전사 완료")

    log("재조판 시작")
    results = []

    def _flush_review() -> None:
        # 페이지마다 review.json 즉시 병합 저장 — 파이프라인 공용 함수
        # 사용 (원자적 교체 + WinError 5/32 재시도 + 실패해도 실행 계속.
        # 검수 서버·백신이 파일을 잠깐 잡아 전체 실행이 중단됐던 사고
        # 재발 방지 — 경고는 콘솔에 출력됨)
        retype._flush_review_cli(out, results)

    for i, f in enumerate(up_files, 1):
        if is_cancelled():
            raise Cancelled()
        if f.name in locked:
            log(f"  [{i}/{len(up_files)}] {f.name} — 수동 확정 잠금, 건너뜀")
            results.append({"file": f.name, "status": "locked"})
            _flush_review()
            continue
        if resume and (out / f"{f.stem}_final.png").exists():
            log(f"  [{i}/{len(up_files)}] {f.name} — 완료본 있음, 건너뜀")
            results.append({"file": f.name, "status": "skipped"})
            _flush_review()
            continue
        log(f"  [{i}/{len(up_files)}] {f.name}")
        try:
            r = retype.process_page(f, out, args, transcript)
        except Exception as e:
            r = {"file": f.name, "status": "error", "error": str(e),
                 "trace": traceback.format_exc()}
            results.append(r)
            _flush_review()
            if "API 오류 401" in str(e) or "API 오류 403" in str(e):
                log("!! API 키 인증 실패 — 같은 오류가 반복되므로 남은 "
                    "페이지를 중단합니다.")
                log("   위 오류 안내에 나온 서버에서 발급한 키인지 "
                    "확인하세요 (서버가 다르면 키도 다릅니다 — DeepInfra "
                    "URL에는 deepinfra.com 키).")
                break
            if "이미지 입력을 지원하지 않는" in str(e):
                log("!! DeepSeek 서버가 이미지를 거부 — 남은 페이지를 "
                    "중단합니다.")
                log("   해결: 환경 설정 탭에서 'DeepSeek URL'을 "
                    "https://api.deepinfra.com/v1/openai 로,")
                log("   'DeepSeek 모델'을 deepseek-ai/DeepSeek-OCR 로 "
                    "바꾸고 DeepInfra 키(deepinfra.com)를 넣으세요.")
                log("   (공식 api.deepseek.com은 아직 이미지 입력 미지원)")
                break
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
            _flush_review()   # 처리 완료 페이지는 즉시 검수 가능
            msg = (f"    -> 말풍선 {r['bubbles']}개, 재조판 {r['retyped']}개")
            if r.get("pos_warnings"):
                msg += f", 위치 확인 필요 {r['pos_warnings']}건"
            log(msg)

    _flush_review()
    ok = sum(1 for r in results if r.get("status") == "ok")
    warns = sum(r.get("pos_warnings", 0) for r in results)
    log(f"\n완료 — {ok}/{len(results)}장 성공, 위치 확인 필요 {warns}건")
    us = retype.usage_summary()   # 파트별 API 토큰·예상 요금
    if us:
        log(us)
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

    # ---- 툴팁 (표준 라이브러리 idlelib — 미포함 배포에선 조용히 비활성) ----
    try:
        from idlelib.tooltip import Hovertip

        def tip(w, text):
            Hovertip(w, text, hover_delay=400)
    except Exception:
        def tip(w, text):
            pass

    root = tk.Tk()
    root.title(f"만화 한글 복원 — 업스케일 + 재조판 v{retype.__version__}")
    root.geometry("820x760")

    # 앱 아이콘 — 창 제목줄·작업표시줄 (PyInstaller 번들은 _MEIPASS 폴백)
    ico = APP_DIR / "app_icon.ico"
    if not ico.exists() and getattr(sys, "_MEIPASS", None):
        ico = Path(sys._MEIPASS) / "app_icon.ico"
    if ico.exists():
        try:
            root.iconbitmap(default=str(ico))
        except tk.TclError:   # 비Windows 환경 등 — 아이콘 없이 진행
            pass

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
        "skip_retype": tk.BooleanVar(value=cfg0.get("skip_retype", False)),
        "ink_boost": tk.StringVar(value=str(cfg0.get("ink_boost", 0))),
        "ocr_engine": tk.StringVar(value=next(
            (lb for lb, k in OCR_ENGINES
             if k == cfg0.get("ocr_engine", "claude")), OCR_ENGINES[0][0])),
        "source_lang": tk.StringVar(value=next(
            (lb for lb, k in SRC_LANGS
             if k == cfg0.get("source_lang", "ko")), SRC_LANGS[0][0])),
        "translate_mode": tk.StringVar(value=next(
            (lb for lb, k in XLAT_MODES
             if k == cfg0.get("translate_mode", "vision")),
            XLAT_MODES[0][0])),
        "translate_consensus": tk.BooleanVar(
            value=cfg0.get("translate_consensus", False)),
        "translate_backend": tk.StringVar(value=next(
            (lb for lb, k in XLAT_BACKENDS
             if k == cfg0.get("translate_backend", "claude")),
            XLAT_BACKENDS[0][0])),
        "ollama_model": tk.StringVar(
            value=cfg0.get("ollama_model", retype.OLLAMA_MODEL)),
        "gemini_model": tk.StringVar(
            value=cfg0.get("gemini_model", retype.GEMINI_MODEL)),
        "gemini_api_key": tk.StringVar(
            value=cfg0.get("gemini_api_key", "")),
        "deepseek_model": tk.StringVar(
            value=cfg0.get("deepseek_model", retype.DEEPSEEK_MODEL)),
        "deepseek_url": tk.StringVar(
            value=cfg0.get("deepseek_url", retype.DEEPSEEK_URL)),
        "deepseek_api_key": tk.StringVar(
            value=cfg0.get("deepseek_api_key", "")),
        "kimi_model": tk.StringVar(
            value=cfg0.get("kimi_model", retype.KIMI_MODEL)),
        "kimi_api_key": tk.StringVar(
            value=cfg0.get("kimi_api_key", "")),
        "glossary": tk.StringVar(value=cfg0.get("glossary", "")),
        "caption_preset": tk.StringVar(
            value=cfg0.get("caption_preset", "본문과 동일")),
        "caption_font": tk.StringVar(value=cfg0.get("caption_font", "")),
        "caption_font_index": tk.StringVar(
            value=str(cfg0.get("caption_font_index", 0))),
        "shout_preset": tk.StringVar(
            value=cfg0.get("shout_preset", "본문과 동일")),
        "shout_font": tk.StringVar(value=cfg0.get("shout_font", "")),
        "shout_font_index": tk.StringVar(
            value=str(cfg0.get("shout_font_index", 0))),
        "retype_sfx": tk.BooleanVar(value=cfg0.get("retype_sfx", False)),
        "text_backing": tk.BooleanVar(
            value=cfg0.get("text_backing", True)),
        "erase_fill": tk.BooleanVar(value=cfg0.get("erase_fill", False)),
        "sfx_preset": tk.StringVar(
            value=cfg0.get("sfx_preset", "자동 감지")),
        "sfx_font": tk.StringVar(
            value=cfg0.get("sfx_font") or find_default_sfx_font()),
        "sfx_font_index": tk.StringVar(
            value=str(cfg0.get("sfx_font_index", 0))),
        "preset_name": tk.StringVar(value=cfg0.get("preset_name", "")),
        "zip_preset": tk.StringVar(value=cfg0.get("zip_preset", "")),
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
        "page_range": tk.StringVar(value=cfg0.get("page_range", "")),
        "sample_index": tk.StringVar(value=str(cfg0.get("sample_index", 3))),
        "font_index": tk.StringVar(value=str(cfg0.get("font_index", 0))),
        "font_preset": tk.StringVar(value=cfg0.get("font_preset", "자동 감지")),
        "hand_preset": tk.StringVar(value=cfg0.get("hand_preset", "자동 감지")),
    }

    font_presets = resolve_presets(FONT_PRESETS)
    hand_presets = resolve_presets(HAND_PRESETS)

    frm = ttk.Frame(root, padding=10)
    frm.pack(fill="both", expand=True)

    # ---- 상단: 작품 프리셋 (항상 표시) ----
    topf = ttk.Frame(frm)
    topf.pack(fill="x")
    topf.columnconfigure(1, weight=1)

    # ---- 가운데: 설정 탭 (실행 / 보정 옵션 / 폰트 / 환경 설정) ----
    nb = ttk.Notebook(frm)
    nb.pack(fill="x", pady=(8, 0))
    tab_run = ttk.Frame(nb, padding=8)
    tab_opt = ttk.Frame(nb, padding=8)
    tab_font = ttk.Frame(nb, padding=8)
    tab_env = ttk.Frame(nb, padding=8)
    nb.add(tab_run, text=" 실행 ")
    nb.add(tab_opt, text=" 보정 옵션 ")
    nb.add(tab_font, text=" 폰트 ")
    nb.add(tab_env, text=" 환경 설정 ")
    for _t in (tab_run, tab_font, tab_env):
        _t.columnconfigure(1, weight=1)

    # ---- 하단: 실행 버튼·진행 상태·로그 (탭과 무관하게 항상 표시) ----
    bot = ttk.Frame(frm)
    bot.pack(fill="both", expand=True, pady=(8, 0))

    log_box = tk.Text(bot, height=12, state="disabled")
    log_box.pack(side="bottom", fill="both", expand=True, pady=(6, 0))

    _rows: dict = {}

    def nrow(parent) -> int:
        """부모 프레임별 grid 행 카운터."""
        _rows[parent] = _rows.get(parent, -1) + 1
        return _rows[parent]

    def add_path(parent, label, key, is_dir=True, ftypes=None):
        r = nrow(parent)
        ttk.Label(parent, text=label).grid(row=r, column=0, sticky="w", pady=2)
        ent = ttk.Entry(parent, textvariable=v[key])
        ent.grid(row=r, column=1, sticky="ew", padx=4)
        def browse():
            p = (filedialog.askdirectory() if is_dir
                 else filedialog.askopenfilename(filetypes=ftypes or []))
            if p:
                v[key].set(p)
        ttk.Button(parent, text="찾아보기", command=browse).grid(row=r, column=2)
        return ent

    # ---- 작품(코믹스)별 설정 프리셋 ----
    # 현재 창의 모든 설정을 이름 붙여 저장/복원 (API 키 제외).
    # app_config.json 의 "presets" 키에 영구 저장.
    presets_store: dict = dict(cfg0.get("presets") or {})
    _PRESET_SKIP = {"api_key", "save_key", "preset_name"}

    ttk.Label(topf, text="작품 프리셋").grid(row=0, column=0,
                                             sticky="w", pady=2)
    ps_cb = ttk.Combobox(topf, textvariable=v["preset_name"],
                         values=sorted(presets_store))
    ps_cb.grid(row=0, column=1, sticky="ew", padx=4)
    ps_btns = ttk.Frame(topf)
    ps_btns.grid(row=0, column=2, sticky="w")
    tip(ps_cb, "현재 창의 모든 설정을 작품 이름으로 저장/복원합니다 "
               "(API 키 제외).")

    def _snapshot() -> dict:
        return {k: (bool(var.get()) if isinstance(var, tk.BooleanVar)
                    else var.get())
                for k, var in v.items() if k not in _PRESET_SKIP}

    def apply_preset(_=None):
        data = presets_store.get(v["preset_name"].get().strip())
        if not data:
            return
        for k, val in data.items():
            if k in v and k not in _PRESET_SKIP:
                try:
                    v[k].set(val)
                except tk.TclError:
                    pass
    ps_cb.bind("<<ComboboxSelected>>", apply_preset)

    def save_preset():
        name = v["preset_name"].get().strip()
        if not name:
            messagebox.showinfo("작품 프리셋",
                                "프리셋 이름을 입력한 뒤 저장하세요.\n"
                                "(예: Invincible, 카이지)")
            return
        presets_store[name] = _snapshot()
        ps_cb["values"] = sorted(presets_store)
        save_cfg(collect_cfg())   # 즉시 영구 저장
        messagebox.showinfo("작품 프리셋", f"'{name}' 저장 완료 — "
                            "콤보에서 선택하면 전체 설정이 복원됩니다.")

    def del_preset():
        name = v["preset_name"].get().strip()
        if name in presets_store and messagebox.askyesno(
                "작품 프리셋", f"'{name}' 프리셋을 삭제할까요?"):
            del presets_store[name]
            ps_cb["values"] = sorted(presets_store)
            v["preset_name"].set("")
            save_cfg(collect_cfg())

    ttk.Button(ps_btns, text="저장", width=5,
               command=save_preset).pack(side="left")
    ttk.Button(ps_btns, text="삭제", width=5,
               command=del_preset).pack(side="left", padx=2)

    def _fmt_size(b: float) -> str:
        if b >= 1 << 30:
            return f"{b / (1 << 30):,.2f} GB"
        return f"{b / (1 << 20):,.1f} MB"

    def cleanup_dialog():
        """검수 완료 후 재생성 가능한 중간 데이터 선택 삭제."""
        if state["running"]:
            messagebox.showinfo("작업 폴더 정리",
                                "작업 실행 중에는 정리할 수 없습니다.")
            return
        cfg = collect_cfg()
        if not resolve_out(cfg):
            return
        out = Path(cfg["out"])
        if not out.exists():
            messagebox.showinfo("작업 폴더 정리", "출력 폴더가 없습니다.")
            return
        items = retype.scan_cleanup(out)
        if not items:
            messagebox.showinfo("작업 폴더 정리",
                                "정리할 중간 데이터가 없습니다.")
            return

        win = tk.Toplevel(root)
        win.title("작업 폴더 정리")
        win.transient(root)
        win.grab_set()
        fr = ttk.Frame(win, padding=12)
        fr.pack(fill="both", expand=True)
        ttk.Label(fr, text=f"대상: {out}").pack(anchor="w")
        ttk.Label(fr, text="최종 결과(*_final.png)·검수 데이터(review.json)·"
                           "브러시 원본(_paint)·용어집·ZIP은 항상 보존됩니다.",
                  foreground="#666").pack(anchor="w", pady=(2, 6))
        if not (list(out.glob("*.zip")) + list(out.glob("*.cbz"))):
            ttk.Label(fr, text="⚠ 최종본 ZIP이 아직 없습니다 — "
                               "정리 전에 [최종본 ZIP] 실행을 권장합니다.",
                      foreground="#b8860b").pack(anchor="w", pady=(0, 6))

        checks = {}
        total_lbl = ttk.Label(fr, font=("맑은 고딕", 10, "bold"))

        def upd_total(*_):
            sel = [it for it in items if checks[it["key"]].get()]
            total_lbl.configure(
                text=f"선택: {sum(it['count'] for it in sel)}개 파일, "
                     f"{_fmt_size(sum(it['bytes'] for it in sel))} 확보")

        for it in items:
            bv = tk.BooleanVar(value=it["default"])
            bv.trace_add("write", upd_total)
            checks[it["key"]] = bv
            row_f = ttk.Frame(fr)
            row_f.pack(fill="x", pady=(4, 0))
            ttk.Checkbutton(
                row_f, variable=bv,
                text=f"{it['label']}  —  {it['count']}개, "
                     f"{_fmt_size(it['bytes'])}").pack(anchor="w")
            ttk.Label(row_f, text=it["note"], foreground="#888",
                      wraplength=520, justify="left").pack(
                anchor="w", padx=(22, 0))

        total_lbl.pack(anchor="w", pady=(10, 4))
        upd_total()

        def run_clean():
            sel = [it for it in items if checks[it["key"]].get()]
            if not sel:
                return
            names = "\n".join(f"  · {it['label']} ({_fmt_size(it['bytes'])})"
                              for it in sel)
            if not messagebox.askyesno(
                    "작업 폴더 정리",
                    f"다음 항목을 삭제합니다:\n{names}\n\n"
                    "휴지통을 거치지 않고 완전히 삭제됩니다. 계속할까요?",
                    parent=win):
                return
            n, freed = retype.cleanup_workdir(out, [it["key"] for it in sel])
            log(f"작업 폴더 정리: {n}개 파일 삭제, {_fmt_size(freed)} 확보")
            messagebox.showinfo("작업 폴더 정리",
                                f"{n}개 파일 삭제 — {_fmt_size(freed)} 확보",
                                parent=win)
            win.destroy()

        bf = ttk.Frame(fr)
        bf.pack(fill="x", pady=(8, 0))
        ttk.Button(bf, text="선택 항목 삭제", command=run_clean).pack(
            side="left")
        ttk.Button(bf, text="닫기", command=win.destroy).pack(
            side="left", padx=8)

    # ================= [실행] 탭 — 매 작업에 쓰는 것만 =================
    add_path(tab_run, "원본 폴더", "src")
    out_ent = add_path(tab_run, "출력 폴더", "out")
    tip(out_ent, "비우면 원본폴더\\복원출력 폴더가 자동 생성됩니다.")

    r = nrow(tab_run)
    ttk.Label(tab_run, text="페이지 범위").grid(row=r, column=0, sticky="w")
    pr_fr = ttk.Frame(tab_run)
    pr_fr.grid(row=r, column=1, columnspan=2, sticky="w", padx=4)
    pr_ent = ttk.Entry(pr_fr, textvariable=v["page_range"], width=10)
    pr_ent.pack(side="left")
    ttk.Label(pr_fr, text="예: 5-20 · 비우면 전체",
              foreground="#666").pack(side="left", padx=6)
    tip(pr_ent, "5-20 = 5~20장 · 5- = 5장부터 끝 · -20 = 20장까지 · "
                "7 = 한 장만\n비우면 전체 — 파일 정렬 순서 기준.")
    ttk.Label(pr_fr, text="  테스트 장수").pack(side="left", padx=(12, 0))
    lim_ent = ttk.Entry(pr_fr, textvariable=v["limit"], width=6)
    lim_ent.pack(side="left", padx=4)
    ttk.Label(pr_fr, text="(0=전체)", foreground="#666").pack(side="left")
    tip(lim_ent, "앞에서부터 이 장수만 처리 — 설정 테스트용.")

    r = nrow(tab_run)
    sum_lbl = ttk.Label(tab_run, text="", foreground="#666",
                        wraplength=720, justify="left")
    sum_lbl.grid(row=r, column=0, columnspan=3, sticky="w", pady=(10, 0))

    # ================= [보정 옵션] 탭 =================
    lf_mode = ttk.Labelframe(tab_opt, text="실행 방식", padding=6)
    lf_mode.pack(fill="x", pady=(0, 6))
    cb = ttk.Checkbutton(lf_mode, text="이어하기 (완료 페이지 건너뛰기)",
                         variable=v["resume"])
    cb.pack(anchor="w")
    tip(cb, "결과(_final.png)가 이미 있는 페이지는 건너뜁니다 — 크레딧 절약.\n"
            "주의: 글자 굵기 보강 등 합성 설정만 바꿔 다시 처리하려면\n"
            "이 옵션을 끄거나 검수 페이지의 ♻ 재합성을 쓰세요.")
    cb = ttk.Checkbutton(lf_mode, text="Batch API 전사 (50% 할인)",
                         variable=v["use_batch"])
    cb.pack(anchor="w")
    tip(cb, "전 페이지 전사를 한 배치로 제출해 API 비용을 절반으로 줄입니다.\n"
            "배치 완료까지 대기하며, 샘플 미리보기에는 적용되지 않습니다.")
    cb = ttk.Checkbutton(lf_mode, text="전사·재조판 건너뛰기 (감지만)",
                         variable=v["skip_retype"])
    cb.pack(anchor="w")
    tip(cb, "말풍선 감지·보정만 수행 — API 비용 0, 키 불필요.\n"
            "이후 검수 페이지에서 주황 말풍선을 마킹해 선별 전사하는 흐름용.")

    lf_q = ttk.Labelframe(tab_opt, text="화질·배경", padding=6)
    lf_q.pack(fill="x", pady=(0, 6))
    cb = ttk.Checkbutton(lf_q, text="원본 화질 100% 보존",
                         variable=v["preserve_bg"])
    cb.pack(anchor="w")
    tip(cb, "배경을 원본 그대로 두고 재조판 말풍선 부분만 수정합니다.")
    cb = ttk.Checkbutton(lf_q, text="글자 뒤 말풍선 채움",
                         variable=v["text_backing"])
    cb.pack(anchor="w")
    tip(cb, "지움 범위 밖에 남은 원본 글자 잔영을 새 글자 뒤 채움으로 가립니다.")
    cb = ttk.Checkbutton(lf_q, text="글자 영역 흰 채움",
                         variable=v["erase_fill"])
    cb.pack(anchor="w")
    tip(cb, "색 배경 위 잔영까지 제거합니다 — 말풍선 테두리·그림 선은 보호.")
    ib_fr = ttk.Frame(lf_q)
    ib_fr.pack(anchor="w", pady=(4, 0))
    ttk.Label(ib_fr, text="글자 굵기 보강").pack(side="left")
    ib_sp = ttk.Spinbox(ib_fr, textvariable=v["ink_boost"], from_=0, to=4,
                        increment=0.5, width=6)
    ib_sp.pack(side="left", padx=6)
    ttk.Label(ib_fr, text="px (0=끔 · 0.5~2 권장)").pack(side="left")
    tip(ib_sp, "업스케일로 가늘어진 원본 글자 획을 보강합니다.\n"
               "말풍선·캡션 글자에만 적용 — 재조판 말풍선은 영향 없음.\n"
               "이어하기가 켜져 있으면 완료 페이지엔 반영 안 됨(♻ 재합성 사용).")

    lf_up = ttk.Labelframe(tab_opt, text="업스케일", padding=6)
    lf_up.pack(fill="x")
    ttk.Label(lf_up, text="모델").grid(row=0, column=0, sticky="w")
    model_cb = ttk.Combobox(lf_up, textvariable=v["upscayl_model"],
                            values=list_models(v["upscayl_models"].get()),
                            width=22)
    model_cb.grid(row=0, column=1, sticky="w", padx=4)
    ttk.Label(lf_up, text="최종 배율").grid(row=0, column=2, sticky="w",
                                            padx=(16, 0))
    ttk.Combobox(lf_up, textvariable=v["out_scale"],
                 values=["1", "2", "3", "4"], width=4).grid(
        row=0, column=3, sticky="w", padx=4)
    cb = ttk.Checkbutton(lf_up, text="업스케일 건너뛰기",
                         variable=v["skip_upscale"])
    cb.grid(row=0, column=4, sticky="w", padx=(16, 0))
    tip(cb, "이미 업스케일된 이미지 폴더를 원본으로 쓸 때 켜세요.\n"
            "원본을 그대로 재조판 입력으로 사용합니다.")

    # ================= [폰트] 탭 =================
    r = nrow(tab_font)
    ttk.Label(tab_font, text="본문 폰트 프리셋").grid(row=r, column=0,
                                                      sticky="w")
    fp_cb = ttk.Combobox(
        tab_font, textvariable=v["font_preset"], state="readonly",
        values=["자동 매칭 (원본과 유사한 폰트)", "자동 감지"]
               + [p[0] for p in font_presets] + ["직접 지정"])
    fp_cb.grid(row=r, column=1, sticky="ew", padx=4)

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

    add_path(tab_font, "본문 폰트 경로", "font", is_dir=False,
             ftypes=[("font", "*.ttf *.otf *.ttc")])

    # ---- 손글씨 폰트 프리셋 ----
    r = nrow(tab_font)
    ttk.Label(tab_font, text="손글씨 폰트 프리셋").grid(row=r, column=0,
                                                        sticky="w")
    hp_cb = ttk.Combobox(
        tab_font, textvariable=v["hand_preset"], state="readonly",
        values=["자동 감지"] + [p[0] for p in hand_presets] + ["직접 지정"])
    hp_cb.grid(row=r, column=1, sticky="ew", padx=4)
    cb = ttk.Checkbutton(tab_font, text="손글씨도 재조판",
                         variable=v["retype_hand"])
    cb.grid(row=r, column=2, sticky="w")
    tip(cb, "기본 꺼짐 — 끄면 손글씨(hand) 대사는 원본 그대로 보존합니다.")

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

    add_path(tab_font, "손글씨 폰트 경로", "hand_font", is_dir=False,
             ftypes=[("font", "*.ttf *.otf")])

    # ---- 내레이션·캡션 폰트 프리셋 (kind=caption 전용, 비우면 본문 폰트) ----
    r = nrow(tab_font)
    ttk.Label(tab_font, text="캡션 폰트 프리셋").grid(row=r, column=0,
                                                      sticky="w")
    cp_cb = ttk.Combobox(
        tab_font, textvariable=v["caption_preset"], state="readonly",
        values=["본문과 동일"] + [p[0] for p in font_presets] + ["직접 지정"])
    cp_cb.grid(row=r, column=1, sticky="ew", padx=4)
    tip(cp_cb, "내레이션 박스(caption) 전용 폰트 — "
               "'본문과 동일'이면 본문 폰트를 씁니다.")

    def on_caption_preset(_=None):
        sel = v["caption_preset"].get()
        if sel == "본문과 동일":
            v["caption_font"].set("")
            v["caption_font_index"].set("0")
        elif sel != "직접 지정":
            for label, path, idx in font_presets:
                if label == sel:
                    v["caption_font"].set(path)
                    v["caption_font_index"].set(str(idx))
                    break
    cp_cb.bind("<<ComboboxSelected>>", on_caption_preset)

    add_path(tab_font, "캡션 폰트 경로", "caption_font", is_dir=False,
             ftypes=[("font", "*.ttf *.otf *.ttc")])

    # ---- 외침 폰트 (kind=shout — 뾰족 말풍선·굵은 대형 대사) ----
    r = nrow(tab_font)
    ttk.Label(tab_font, text="외침 폰트 프리셋").grid(row=r, column=0,
                                                      sticky="w")
    sh_cb = ttk.Combobox(
        tab_font, textvariable=v["shout_preset"], state="readonly",
        values=["본문과 동일"] + [p[0] for p in font_presets] + ["직접 지정"])
    sh_cb.grid(row=r, column=1, sticky="ew", padx=4)
    tip(sh_cb, "뾰족 말풍선·굵은 대형 대사(shout) 전용 폰트 — "
               "'본문과 동일'이면 본문 폰트를 씁니다.")

    def on_shout_preset(_=None):
        sel = v["shout_preset"].get()
        if sel == "본문과 동일":
            v["shout_font"].set("")
            v["shout_font_index"].set("0")
        elif sel != "직접 지정":
            for label, path, idx in font_presets:
                if label == sel:
                    v["shout_font"].set(path)
                    v["shout_font_index"].set(str(idx))
                    break
    sh_cb.bind("<<ComboboxSelected>>", on_shout_preset)

    add_path(tab_font, "외침 폰트 경로", "shout_font", is_dir=False,
             ftypes=[("font", "*.ttf *.otf *.ttc")])

    # ---- 효과음 (kind=sfx — 기본 보존, 옵트인 재조판) ----
    r = nrow(tab_font)
    ttk.Label(tab_font, text="효과음 폰트 프리셋").grid(row=r, column=0,
                                                        sticky="w")
    sx_cb = ttk.Combobox(
        tab_font, textvariable=v["sfx_preset"], state="readonly",
        values=["자동 감지"] + [p[0] for p in font_presets] + ["직접 지정"])
    sx_cb.grid(row=r, column=1, sticky="ew", padx=4)
    cb = ttk.Checkbutton(tab_font, text="효과음도 재조판",
                         variable=v["retype_sfx"])
    cb.grid(row=r, column=2, sticky="w")
    tip(cb, "기본 보존 — 켜면 효과음(sfx)도 지우고 다시 씁니다.")

    def on_sfx_preset(_=None):
        sel = v["sfx_preset"].get()
        if sel == "자동 감지":
            v["sfx_font"].set(find_default_sfx_font())
            v["sfx_font_index"].set("0")
        elif sel != "직접 지정":
            for label, path, idx in font_presets:
                if label == sel:
                    v["sfx_font"].set(path)
                    v["sfx_font_index"].set(str(idx))
                    break
    sx_cb.bind("<<ComboboxSelected>>", on_sfx_preset)

    add_path(tab_font, "효과음 폰트 경로", "sfx_font", is_dir=False,
             ftypes=[("font", "*.ttf *.otf *.ttc")])

    # ================= [환경 설정] 탭 — 최초 1회 성격 =================
    add_path(tab_env, "upscayl-bin.exe", "upscayl_exe", is_dir=False,
             ftypes=[("exe", "*.exe")])
    add_path(tab_env, "Upscayl models 폴더", "upscayl_models")

    r = nrow(tab_env)
    ttk.Label(tab_env, text="ANTHROPIC_API_KEY").grid(row=r, column=0,
                                                      sticky="w")
    ttk.Entry(tab_env, textvariable=v["api_key"], show="*").grid(
        row=r, column=1, sticky="ew", padx=4)
    cb = ttk.Checkbutton(tab_env, text="키 저장", variable=v["save_key"])
    cb.grid(row=r, column=2)
    tip(cb, "app_config.json에 키를 저장합니다 — 공용 PC에서는 끄세요.")

    BILLING_URL = "https://console.anthropic.com/settings/billing"

    def open_billing(_=None):
        import webbrowser
        webbrowser.open(BILLING_URL)

    r = nrow(tab_env)
    link = ttk.Label(tab_env, text="크레딧 잔액 확인·충전 (Plans & Billing 열기)",
                     foreground="#0b5ed7", cursor="hand2")
    link.grid(row=r, column=1, sticky="w", padx=4)
    link.bind("<Button-1>", open_billing)

    r = nrow(tab_env)
    ttk.Label(tab_env, text="전사 엔진").grid(row=r, column=0, sticky="w")
    ttk.Combobox(tab_env, textvariable=v["ocr_engine"], state="readonly",
                 values=[lb for lb, _ in OCR_ENGINES]).grid(
        row=r, column=1, sticky="ew", padx=4)

    r = nrow(tab_env)
    ttk.Label(tab_env, text="Claude 모델").grid(row=r, column=0, sticky="w")
    ttk.Combobox(tab_env, textvariable=v["claude_model"],
                 values=["claude-sonnet-4-5", "claude-haiku-4-5"],
                 width=22).grid(row=r, column=1, sticky="w", padx=4)

    r = nrow(tab_env)
    ttk.Label(tab_env, text="Gemini 모델").grid(row=r, column=0, sticky="w")
    gm_cb = ttk.Combobox(tab_env, textvariable=v["gemini_model"],
                         values=["gemini-3.1-flash-lite",
                                 "gemini-2.5-flash-lite",
                                 "gemini-3.5-flash"], width=22)
    gm_cb.grid(row=r, column=1, sticky="w", padx=4)
    tip(gm_cb, "전사 엔진 또는 번역 엔진을 Gemini로 선택했을 때 쓰입니다.\n"
               "Gemini 전사는 1회 전사(합의 검증 없음) — Claude보다 "
               "저렴하지만\n오인식 시 검수 페이지에서 잡아야 합니다.\n"
               "번역 모드에서 번역 엔진이 Gemini가 아니면 Gemini는 원문 "
               "전사만 하고\n번역은 선택한 번역 엔진(Claude 등)이 "
               "수행합니다 (분리 조합).")

    r = nrow(tab_env)
    ttk.Label(tab_env, text="GEMINI_API_KEY").grid(row=r, column=0,
                                                   sticky="w")
    gk_ent = ttk.Entry(tab_env, textvariable=v["gemini_api_key"], show="*")
    gk_ent.grid(row=r, column=1, sticky="ew", padx=4)
    tip(gk_ent, "Google AI Studio에서 무료 발급: "
                "https://aistudio.google.com/apikey\n"
                "비워두면 GEMINI_API_KEY 환경변수를 사용합니다.")

    r = nrow(tab_env)
    ttk.Label(tab_env, text="DeepSeek 모델").grid(row=r, column=0, sticky="w")
    dsm_cb = ttk.Combobox(tab_env, textvariable=v["deepseek_model"],
                          values=["deepseek-v4-flash", "deepseek-v4-pro",
                                  "deepseek-ai/DeepSeek-OCR",
                                  "deepseek-ai/DeepSeek-OCR-2"],
                          width=22)
    dsm_cb.grid(row=r, column=1, sticky="w", padx=4)
    tip(dsm_cb, "전사 엔진을 DeepSeek으로 선택했을 때 쓰입니다.\n"
                "1회 전사(합의 검증 없음) — 초저가지만 오인식 시 검수 "
                "페이지에서 잡아야 합니다.\n번역 모드에서는 원문 전사만 "
                "하고 번역은 선택한 번역 엔진이 수행합니다 (분리 조합).")

    r = nrow(tab_env)
    ttk.Label(tab_env, text="DeepSeek URL").grid(row=r, column=0, sticky="w")
    dsu_ent = ttk.Combobox(tab_env, textvariable=v["deepseek_url"],
                           values=[retype.DEEPSEEK_URL,
                                   "https://api.deepinfra.com/v1/openai"])
    dsu_ent.grid(row=r, column=1, sticky="ew", padx=4)
    tip(dsu_ent, "OpenAI 호환 chat/completions 서버 주소.\n"
                 "공식 API(api.deepseek.com)는 아직 이미지 입력 미지원 —\n"
                 "만화 전사에는 https://api.deepinfra.com/v1/openai 선택 후\n"
                 "모델 deepseek-ai/DeepSeek-OCR + DeepInfra 키"
                 "(deepinfra.com 발급)를 쓰세요.")

    r = nrow(tab_env)
    ttk.Label(tab_env, text="DEEPSEEK_API_KEY").grid(row=r, column=0,
                                                     sticky="w")
    dsk_ent = ttk.Entry(tab_env, textvariable=v["deepseek_api_key"],
                        show="*")
    dsk_ent.grid(row=r, column=1, sticky="ew", padx=4)
    tip(dsk_ent, "https://platform.deepseek.com 에서 발급.\n"
                 "비워두면 DEEPSEEK_API_KEY 환경변수를 사용합니다.")

    r = nrow(tab_env)
    ttk.Label(tab_env, text="Kimi 모델").grid(row=r, column=0, sticky="w")
    km_cb = ttk.Combobox(tab_env, textvariable=v["kimi_model"],
                         values=["kimi-k2.5", "kimi-k2.6"], width=22)
    km_cb.grid(row=r, column=1, sticky="w", padx=4)
    tip(km_cb, "번역 엔진을 Kimi로 선택했을 때 쓰입니다 (번역 전용).\n"
               "k2.5=가성비 멀티모달, k2.6=최신·약간 비쌈.")

    r = nrow(tab_env)
    ttk.Label(tab_env, text="MOONSHOT_API_KEY").grid(row=r, column=0,
                                                     sticky="w")
    kk_ent = ttk.Entry(tab_env, textvariable=v["kimi_api_key"], show="*")
    kk_ent.grid(row=r, column=1, sticky="ew", padx=4)
    tip(kk_ent, "Moonshot 플랫폼에서 발급: https://platform.moonshot.ai\n"
                "비워두면 MOONSHOT_API_KEY 환경변수를 사용합니다.")

    r = nrow(tab_env)
    lf_xl = ttk.Labelframe(tab_env, text="번역 (원서 → 한글)", padding=6)
    lf_xl.grid(row=r, column=0, columnspan=3, sticky="ew", pady=(8, 0))
    lf_xl.columnconfigure(1, weight=1)

    rx = nrow(lf_xl)
    ttk.Label(lf_xl, text="원서 언어").grid(row=rx, column=0, sticky="w")
    ttk.Combobox(lf_xl, textvariable=v["source_lang"], state="readonly",
                 values=[lb for lb, _ in SRC_LANGS]).grid(
        row=rx, column=1, sticky="ew", padx=4)

    rx = nrow(lf_xl)
    ttk.Label(lf_xl, text="번역 방식").grid(row=rx, column=0, sticky="w")
    xm_cb = ttk.Combobox(lf_xl, textvariable=v["translate_mode"],
                         state="readonly",
                         values=[lb for lb, _ in XLAT_MODES])
    xm_cb.grid(row=rx, column=1, sticky="ew", padx=4)
    tip(xm_cb, "원서 언어가 한국어가 아닐 때만 쓰입니다.\n"
               "로컬 OCR 방식이어도 번역 자체에는 API 키가 필요합니다\n"
               "(아래 번역 엔진을 Ollama로 바꾸면 키 없이 완전 로컬).")
    cb = ttk.Checkbutton(lf_xl, text="번역 합의(2-pass)",
                         variable=v["translate_consensus"])
    cb.grid(row=rx, column=2, sticky="w")
    tip(cb, "이중 번역 후 대조 — 열화가 심한 페이지용, 비용 2~3배.")

    rx = nrow(lf_xl)
    ttk.Label(lf_xl, text="번역 엔진").grid(row=rx, column=0, sticky="w")
    xb_cb = ttk.Combobox(lf_xl, textvariable=v["translate_backend"],
                         state="readonly",
                         values=[lb for lb, _ in XLAT_BACKENDS])
    xb_cb.grid(row=rx, column=1, sticky="ew", padx=4)
    tip(xb_cb, "번역문을 만드는 엔진 — 로컬 OCR 방식과 Gemini 전사에서 "
               "쓰입니다.\n· Gemini 전사 + Claude 번역: 저가 비전 전사와 "
               "고품질 번역의 분리 조합\n· Gemini 전사 + Gemini 번역: "
               "전사+번역을 한 요청으로 (가장 저렴)\n· Ollama: API 키·크레딧 "
               "없이 완전 로컬 — 단 용어집·말투 준수가 약함\n(Claude 비전 "
               "방식은 전사+번역이 한 요청이라 항상 Claude 사용)")

    rx = nrow(lf_xl)
    ttk.Label(lf_xl, text="Ollama 모델").grid(row=rx, column=0, sticky="w")
    om_ent = ttk.Entry(lf_xl, textvariable=v["ollama_model"], width=24)
    om_ent.grid(row=rx, column=1, sticky="w", padx=4)
    tip(om_ent, "Ollama에 받아둔 모델 이름 (미리 ollama pull 필요).\n"
                "예: qwen3:14b, gemma3:12b, exaone3.5:7.8b — "
                "RTX 5080(16GB)이면 14B급까지 여유.")

    gl_ent = add_path(lf_xl, "번역 용어집(선택)", "glossary", is_dir=False,
                      ftypes=[("텍스트 파일", "*.txt"), ("모든 파일", "*.*")])
    tip(gl_ent, "인명·지명 등 고정 번역 목록 (원문=번역 한 줄씩).\n"
                "비우면 출력폴더\\_glossary.txt를 자동 인식합니다.")

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
            cap_font = (job_cfg or {}).get("caption_font") or ""
            shout_font = (job_cfg or {}).get("shout_font") or ""
            sfx_font = (job_cfg or {}).get("sfx_font") or ""
            dummy = np.zeros((1, 1), np.uint8)
            for e in cache["bubbles"]:
                b = retype.Bubble(
                    bbox=tuple(e["bbox"]), mask=dummy, text=e["text"],
                    kind=e.get("kind", "dialogue"),
                    font_cap=e.get("font_cap", 0),
                    line_boxes=[tuple(x) for x in e.get("line_boxes", [])])
                if b.kind == "hand" and hand_font:
                    fp, fi, stroke = hand_font, 0, 1
                elif b.kind == "caption" and cap_font:
                    fp, fi, stroke = cap_font, 0, 0
                elif b.kind == "shout" and shout_font:
                    fp, fi, stroke = shout_font, 0, 0
                elif b.kind == "sfx" and sfx_font:
                    fp, fi, stroke = sfx_font, 0, 0
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

    _PROG_RE = re.compile(r"\[(\d+)/(\d+)\]")

    def _track_progress(msg: str) -> None:
        """로그의 [i/N] 패턴으로 진행바 갱신 — 배치 대기 등은 인디터미닛."""
        mm = _PROG_RE.search(msg)
        if not mm or not state.get("running"):
            return
        i, n = int(mm.group(1)), int(mm.group(2))
        if n <= 0:
            return
        if not state.get("_prog_det"):      # 인디터미닛 → 페이지 카운트 전환
            state["_prog_det"] = True
            prog.stop()
            prog.configure(mode="determinate")
        prog.configure(maximum=n, value=i)
        status_lbl.configure(text=f"{i}/{n} 페이지")

    def poll():
        try:
            while True:
                m = q.get_nowait()
                if isinstance(m, tuple) and m and m[0] == "PREVIEW":
                    open_preview(*m[1:])
                    continue
                if isinstance(m, tuple) and m and m[0] == "MSG":
                    messagebox.showinfo(m[1], m[2])   # 워커 스레드발 알림
                    continue
                if isinstance(m, tuple) and m and m[0] == "RES":
                    res_lbl.configure(text=m[1])   # CPU/GPU 사용량 갱신
                    continue
                log_box.configure(state="normal")
                log_box.insert("end", str(m) + "\n")
                log_box.see("end")
                log_box.configure(state="disabled")
                _track_progress(str(m))
        except queue.Empty:
            pass
        _sync_ui()
        root.after(150, poll)

    def collect_cfg() -> dict:
        cfg = {k: (var.get() if not isinstance(var, tk.BooleanVar)
                   else bool(var.get())) for k, var in v.items()}
        cfg["limit"] = int(cfg.get("limit") or 0)
        cfg["page_range"] = (cfg.get("page_range") or "").strip()
        cfg["glossary"] = (cfg.get("glossary") or "").strip()
        cfg["caption_font_index"] = int(cfg.get("caption_font_index") or 0)
        cfg["shout_font_index"] = int(cfg.get("shout_font_index") or 0)
        cfg["sfx_font_index"] = int(cfg.get("sfx_font_index") or 0)
        cfg["out_scale"] = float(cfg.get("out_scale") or 2)
        try:
            cfg["ink_boost"] = float(cfg.get("ink_boost") or 0)
        except ValueError:
            cfg["ink_boost"] = 0.0
        cfg["font_index"] = int(cfg.get("font_index") or 0)
        cfg["font_auto_match"] = str(
            cfg.get("font_preset", "")).startswith("자동 매칭")
        cfg["ocr_engine"] = next((k for lb, k in OCR_ENGINES
                                  if lb == cfg.get("ocr_engine")), "claude")
        cfg["source_lang"] = next((k for lb, k in SRC_LANGS
                                   if lb == cfg.get("source_lang")), "ko")
        cfg["translate_mode"] = next(
            (k for lb, k in XLAT_MODES
             if lb == cfg.get("translate_mode")), "vision")
        cfg["translate_backend"] = next(
            (k for lb, k in XLAT_BACKENDS
             if lb == cfg.get("translate_backend")), "claude")
        cfg["ollama_model"] = (cfg.get("ollama_model") or "").strip()
        cfg["sample_index"] = 0   # 전체 실행 기본값 — 샘플 버튼에서만 설정
        return cfg

    def save_cfg(cfg: dict) -> None:
        c = dict(cfg)
        if not cfg.get("save_key"):
            c["api_key"] = ""
        c["presets"] = presets_store   # 작품별 프리셋 함께 보존
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
            # 중지·오류로 끝나도 그때까지 쓴 API 요금은 표시 (정상 종료
            # 시엔 run_job이 이미 출력·초기화해 빈 문자열)
            try:
                us = retype.usage_summary()
                if us:
                    log(us)
            except Exception:
                pass
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
                "확인", "표시할 페이지가 아직 없습니다.\n"
                "실행 중이라면 첫 페이지가 완료된 뒤 다시 눌러보세요.\n"
                "(처음이라면 [전체 시작]으로 작업을 실행하세요.)")
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

            def do_GET(self):
                # /_srcimg/<페이지명> — 업스케일 전 소스 원본 이미지 서빙
                # (출력 폴더 밖의 원본 폴더에서 같은 이름(stem)으로 탐색)
                p = self.path.split("?")[0]
                if p.startswith("/_srcimg/"):
                    from urllib.parse import unquote
                    stem = Path(unquote(p[len("/_srcimg/"):])).stem
                    cfgS = app_state["server_cfg"]
                    src = Path(cfgS.get("src") or "")
                    mime = {".png": "png", ".jpg": "jpeg",
                            ".jpeg": "jpeg", ".webp": "webp",
                            ".bmp": "bmp"}
                    if src.is_dir():
                        for fp3 in sorted(src.glob(stem + ".*")):
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
                    us = retype.usage_summary()
                    if us:
                        log(us)
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

        # 출력 폴더별 고정 포트 (49152~65151) — 앱을 껐다 켜도 같은 origin이
        # 되어 검수 페이지 localStorage(줌·마지막 본 페이지 기억)가 유지됨.
        import zlib
        port = 49152 + zlib.crc32(str(out).encode("utf-8")) % 16000
        try:
            srv = ThreadingHTTPServer(
                ("127.0.0.1", port),
                functools.partial(Handler, directory=str(out)))
        except OSError:   # 포트 충돌 등 — 임의 포트 폴백
            try:
                srv = ThreadingHTTPServer(
                    ("127.0.0.1", 0),
                    functools.partial(Handler, directory=str(out)))
            except Exception as e:
                log(f"!! 검수 서버 시작 실패({e}) — 파일로 엽니다")
                return None
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
            us = retype.usage_summary()
            if us:
                log(us)
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

    def _export_zip_worker(cfg: dict, preset) -> None:
        try:
            zp, n, nl = retype.export_final_zip(
                Path(cfg["out"]), preset=preset, log=log,
                is_cancelled=lambda: state["cancel"])
            log(f"최종본 아카이브 저장: {zp} "
                f"({n}페이지, 수동확정 잠금 {nl}페이지)")
            try:   # 원본 페이지 수 대비 미처리 경고
                n_src = sum(1 for p in Path(cfg["src"]).iterdir()
                            if p.suffix.lower() in IMG_EXTS)
                if n_src and n < n_src:
                    log(f"  !! 원본 {n_src}페이지 중 {n}페이지만 결과 있음 — "
                        "미처리 페이지를 확인하세요")
            except OSError:
                pass
            try:
                mb = zp.stat().st_size / (1 << 20)
                q.put(("MSG", "최종본 ZIP",
                       f"{n}페이지 아카이브 완료 ({mb:,.1f} MB):\n{zp}"))
            except OSError:
                q.put(("MSG", "최종본 ZIP", f"{n}페이지 아카이브 완료:\n{zp}"))
            try:
                os.startfile(zp.parent)   # 탐색기에서 위치 열기 (Windows)
            except (OSError, AttributeError):
                pass
        except Exception as e:
            log(f"!! 최종본 ZIP 실패: {e}")
            q.put(("MSG", "최종본 ZIP", f"실패: {e}"))
        finally:
            state["running"] = False

    def export_zip():
        """최종본 ZIP — 프리셋(원본 보관용/모바일 리사이즈·압축) 선택 후 생성."""
        if state["running"]:
            return
        cfg = collect_cfg()
        if not resolve_out(cfg):
            return

        win = tk.Toplevel(root)
        win.title("최종본 ZIP 내보내기")
        win.transient(root)
        win.grab_set()
        fr = ttk.Frame(win, padding=12)
        fr.pack(fill="both", expand=True)
        ttk.Label(fr, text="이미지 프리셋 — 모바일용은 장변 축소 + JPEG "
                           "재압축으로 용량을 크게 줄입니다.").pack(anchor="w")
        labels = [lb for lb, _ in retype.ZIP_PRESETS]
        sel = tk.StringVar(value=v["zip_preset"].get()
                           if v["zip_preset"].get() in labels else labels[0])
        for lb, _ps in retype.ZIP_PRESETS:
            ttk.Radiobutton(fr, text=lb, value=lb, variable=sel).pack(
                anchor="w", pady=2)
        ttk.Label(fr, text="※ 모바일 프리셋은 페이지 수에 따라 수십 초~수 분 "
                           "걸립니다 (진행바 표시 · [■ 중지]로 취소 가능). "
                           "프리셋별로 파일명이 달라 원본 ZIP과 공존합니다.",
                  foreground="#666", wraplength=440, justify="left").pack(
            anchor="w", pady=(8, 0))

        def go():
            preset = next((ps for lb, ps in retype.ZIP_PRESETS
                           if lb == sel.get()), None)
            v["zip_preset"].set(sel.get())
            save_cfg(collect_cfg())
            win.destroy()
            state["running"], state["cancel"] = True, False
            log(f"최종본 ZIP 생성 시작 — {sel.get()}")
            threading.Thread(target=_export_zip_worker, args=(cfg, preset),
                             daemon=True).start()

        bf = ttk.Frame(fr)
        bf.pack(fill="x", pady=(10, 0))
        ttk.Button(bf, text="ZIP 생성", command=go).pack(side="left")
        ttk.Button(bf, text="취소", command=win.destroy).pack(
            side="left", padx=8)

    # ---- 하단 버튼 — 워크플로 3단계 그룹 + 실행 상태 표시 ----
    style = ttk.Style()
    style.configure("Accent.TButton",
                    font=("맑은 고딕", 10, "bold"), padding=(12, 4))

    btns = ttk.Frame(bot)
    btns.pack(side="top", fill="x")

    grow = ttk.Frame(btns)
    grow.pack(fill="x")
    g1 = ttk.Labelframe(grow, text="① 처리", padding=(6, 0))
    g1.pack(side="left", padx=(0, 6))
    btn_start = ttk.Button(g1, text="▶ 전체 시작", style="Accent.TButton",
                           command=start)
    btn_start.pack(side="left", padx=4, pady=2)
    btn_sample = ttk.Button(g1, text="샘플 미리보기", command=sample)
    btn_sample.pack(side="left", padx=(10, 2), pady=2)
    tip(btn_sample, "지정한 번호 한 장만 처리해 미리보기 창으로 확인합니다.\n"
                    "결과가 좋으면 [▶ 전체 시작].")
    ttk.Label(g1, text="번호").pack(side="left")
    sp_ent = ttk.Entry(g1, textvariable=v["sample_index"], width=4)
    sp_ent.pack(side="left", padx=(2, 4))
    tip(sp_ent, "샘플로 처리할 페이지 번호 (파일 정렬 순서 기준)")

    g2 = ttk.Labelframe(grow, text="② 검수", padding=(6, 0))
    g2.pack(side="left", padx=6)
    btn_review = ttk.Button(g2, text="검수 페이지", command=open_review)
    btn_review.pack(side="left", padx=4, pady=2)
    tip(btn_review, "브라우저 검수 페이지를 엽니다 — 말풍선 수정·즉시 적용.")
    btn_rework = ttk.Button(g2, text="검수 반영", command=rework)
    btn_rework.pack(side="left", padx=4, pady=2)
    tip(btn_rework, "다운로드한 rework.json을 선택해 일괄 반영합니다.\n"
                    "(검수 페이지의 [✔ 이 페이지 적용]을 쓰면 필요 없음)")

    g3 = ttk.Labelframe(grow, text="③ 완성", padding=(6, 0))
    g3.pack(side="left", padx=6)
    btn_zip = ttk.Button(g3, text="최종본 ZIP", command=export_zip)
    btn_zip.pack(side="left", padx=4, pady=2)
    tip(btn_zip, "검수 완료 후 결과(*_final.png)만 모아 ZIP으로 묶습니다.")
    btn_clean = ttk.Button(g3, text="🧹 정리", command=cleanup_dialog)
    btn_clean.pack(side="left", padx=4, pady=2)
    tip(btn_clean, "검수 완료 후 재생성 가능한 중간 데이터(업스케일·보정 "
                   "캐시, 샘플, PSD 등)를 골라 삭제해 용량을 확보합니다.\n"
                   "최종 결과·검수 데이터·브러시 원본은 항상 보존됩니다.")

    statfr = ttk.Frame(btns)
    statfr.pack(fill="x", pady=(4, 0))
    prog = ttk.Progressbar(statfr, mode="determinate", maximum=100, value=0)
    prog.pack(side="left", fill="x", expand=True)
    res_lbl = ttk.Label(statfr, text="", foreground="#666")
    res_lbl.pack(side="left", padx=(8, 0))
    tip(res_lbl, "실행 중 시스템 부하 — CPU/GPU 사용률·VRAM, "
                 "Ollama 로컬 번역 시 모델 GPU 적재율")
    status_lbl = ttk.Label(statfr, text="대기 중", width=18, anchor="e")
    status_lbl.pack(side="left", padx=6)
    btn_stop = ttk.Button(statfr, text="■ 중지", command=stop,
                          state="disabled")
    btn_stop.pack(side="left")
    tip(btn_stop, "처리 중인 페이지까지 마치고 중지합니다 (실행 중에만 활성).")

    _run_btns = (btn_start, btn_sample, btn_rework, btn_zip, btn_clean)

    def _res_monitor(use_ollama: bool) -> None:
        """실행 중 1.5초마다 CPU/GPU 사용량을 상태줄에 표시 (워커 스레드).

        use_ollama면 6초마다 Ollama /api/ps로 모델 GPU 적재율도 조회."""
        cpu = _cpu_sampler()
        cpu()   # 기준점 수립
        n = 0
        try:
            while state["running"]:
                time.sleep(1.5)
                parts = []
                c = cpu()
                if c is not None:
                    parts.append(f"CPU {c:.0f}%")
                g = _gpu_sample()
                if g:
                    parts.append(f"GPU {g[0]:.0f}%")
                    parts.append(f"VRAM {g[1] / 1024:.1f}/{g[2] / 1024:.1f}GB")
                if use_ollama:
                    if n % 4 == 0:   # 6초 간격 (요청 부하 최소화)
                        state["_ol_load"] = _ollama_gpu_load(
                            getattr(retype, "OLLAMA_URL",
                                    "http://localhost:11434"))
                    ol = state.get("_ol_load")
                    if ol is not None:
                        parts.append(f"Ollama GPU적재 {ol}%")
                n += 1
                q.put(("RES", "  ·  ".join(parts)))
        finally:
            state["_resmon"] = False
            state.pop("_ol_load", None)
            q.put(("RES", ""))   # 종료 시 표시 지움

    def _sync_ui():
        """실행 상태 → 버튼 활성/진행바 동기화 (poll에서 매 주기 호출)."""
        running = state["running"]
        if running == state.get("_ui_running"):
            return
        state["_ui_running"] = running
        for b in _run_btns:
            b.configure(state="disabled" if running else "normal")
        btn_stop.configure(state="normal" if running else "disabled")
        if running:
            state["_prog_det"] = False
            prog.configure(mode="indeterminate")
            prog.start(80)
            status_lbl.configure(text="작업 실행 중…")
            if not state.get("_resmon"):   # CPU/GPU 사용량 모니터 시작
                state["_resmon"] = True
                try:   # 로컬(Ollama) 번역 조합일 때만 적재율 조회
                    use_ol = (v["translate_backend"].get().startswith("Ollama")
                              and v["translate_mode"].get().startswith("로컬")
                              and not v["source_lang"].get()
                                       .startswith("한국어"))
                except Exception:
                    use_ol = False
                threading.Thread(target=_res_monitor, args=(use_ol,),
                                 daemon=True).start()
        else:
            prog.stop()
            prog.configure(mode="determinate", value=0, maximum=100)
            status_lbl.configure(text="대기 중")

    def _update_summary(_=None):
        """다른 탭의 주요 설정을 [실행] 탭 한 줄 요약으로 표시."""
        try:
            parts = []
            if v["skip_upscale"].get():
                parts.append("업스케일 건너뜀")
            else:
                parts.append(f"업스케일 {v['upscayl_model'].get()} → "
                             f"{v['out_scale'].get()}x")
            if v["skip_retype"].get():
                parts.append("전사 없음(감지만)")
            else:
                t = "전사 " + v["ocr_engine"].get().split(" ")[0]
                if v["use_batch"].get():
                    t += "·Batch"
                parts.append(t)
            sl = v["source_lang"].get()
            if not sl.startswith("한국어"):
                x = sl.split(" ")[0] + "→한글 번역"
                # 번역 백엔드가 실제로 쓰이는 조합(로컬 OCR / Gemini 전사)
                # 이면 어떤 엔진이 번역하는지 표시
                if v["translate_mode"].get().startswith("로컬") \
                        or v["ocr_engine"].get().startswith("Gemini"):
                    x += f"({v['translate_backend'].get().split(' ')[0]})"
                parts.append(x)
            parts.append("본문 " + v["font_preset"].get().split(" (")[0])
            parts.append("손글씨 "
                         + ("재조판" if v["retype_hand"].get() else "보존"))
            parts.append("효과음 "
                         + ("재조판" if v["retype_sfx"].get() else "보존"))
            try:
                ib = float(v["ink_boost"].get() or 0)
            except ValueError:
                ib = 0.0
            if ib:
                parts.append(f"굵기 보강 {ib:g}px")
            if not v["resume"].get():
                parts.append("이어하기 꺼짐")
            sum_lbl.configure(text="이번 실행 설정:  " + " · ".join(parts))
        except Exception:
            pass
    nb.bind("<<NotebookTabChanged>>", _update_summary)
    _update_summary()

    poll()
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
