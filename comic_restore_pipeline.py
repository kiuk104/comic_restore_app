"""
comic_restore_pipeline.py — 스캔 만화 비파괴 화질 복원 파이프라인 (v2, 전면 재설계)

핵심 개념
---------
업스케일(Upscayl) 과정에서 말풍선 안 한글 획이 중간 회색으로 열화되는 문제를
"지우고 다시 쓰기(OCR)" 없이 **원본 글자 자체를 복원**하는 방식으로 해결한다.

  * 대사 소실 불가능 — 어떤 픽셀도 지우지 않고 톤만 재조정
  * OCR 없음 — 오독으로 인한 엉뚱한 텍스트 원천 차단
  * 페이지 전체 보정 — 종이 얼룩/조명 불균일 제거 + 글자 영역 강화

처리 단계 (페이지당)
-------------------
  1. 조명 평탄화   — 밝은 종이 얼룩·조명 불균일만 제거 (어두운 그림은 클램프로 보호)
  2. 노이즈 제거   — fastNlMeansDenoising (업스케일 아티팩트 완화)
  3. 이중 톤 커브  — 전체: 완만한 커브(종이→흰색, 검정 다짐, 중간톤 보존)
                     글자 영역: 강한 S커브(회색 획→검정) + 획 두께 보강
  4. 화이트니스 마스크 — 주변이 대부분 흰색인 영역(말풍선/캡션)에만 강한 커브를
                     페더링 블렌딩. 스크린톤·그라데이션 그림은 완만한 커브 유지
  5. 출력          — PSD(Background 원본 / Restored 보정) + 프리뷰 PNG

사용법
------
    # 먼저 3장 테스트 (비교 이미지 포함)
    python comic_restore_pipeline.py --src "원본폴더" --out ".\test_v2" --limit 3 --debug

    # 전체 처리
    python comic_restore_pipeline.py --src "원본폴더" --out "보정폴더"

    # PNG만 빠르게 (PSD 생략)
    python comic_restore_pipeline.py --src "원본폴더" --out "보정폴더" --no-psd

의존성:  pip install opencv-python numpy pillow psd-tools
(Tesseract, 폰트 불필요 — v1과 달리 OCR을 쓰지 않음)

튜닝 옵션
---------
  --text-black N   글자 커브 검정점 (기본 80).  높이면 더 많은 회색이 검정으로.
                   글자가 여전히 흐리면 90~100으로 올려볼 것.
  --text-white N   글자 커브 흰점 (기본 210). 낮추면 배경이 더 공격적으로 하얘짐.
  --thicken F      획 두께 보강 강도 0.0~1.0 (기본 0.5). 가는 획이 끊겨 보이면 올릴 것.
  --paper N        종이로 간주할 밝기 하한 (기본 215). 종이가 어두운 스캔이면 200으로.
  --no-denoise     노이즈 제거 생략 (약 3배 빠름, 화질 약간 저하)
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path
from typing import Optional

import cv2
import numpy as np


# ---------------------------------------------------------------------------
# 유니코드 경로 IO (Windows 한글 경로 대응)
# ---------------------------------------------------------------------------
def imread_unicode(path: Path, flags: int = cv2.IMREAD_COLOR) -> Optional[np.ndarray]:
    data = np.fromfile(str(path), dtype=np.uint8)
    if data.size == 0:
        return None
    return cv2.imdecode(data, flags)


def imwrite_unicode(path: Path, img: np.ndarray) -> None:
    ok, buf = cv2.imencode(path.suffix or ".png", img)
    if not ok:
        raise IOError(f"cv2.imencode failed: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(buf.tobytes())


# ---------------------------------------------------------------------------
# 복원 코어
# ---------------------------------------------------------------------------
def _smoothstep_curve(img: np.ndarray, lo: float, hi: float) -> np.ndarray:
    """lo 이하 → 0(검정), hi 이상 → 255(흰색), 사이는 smoothstep. float32 반환."""
    t = np.clip((img.astype(np.float32) - lo) / float(hi - lo), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t) * 255.0


def restore_gray(gray: np.ndarray, *,
                 text_black: int = 80, text_white: int = 210,
                 thicken: float = 0.5, paper: int = 215,
                 denoise: bool = True) -> tuple[np.ndarray, np.ndarray]:
    """단일 채널 복원. (보정 이미지, 글자영역 마스크 0~255) 반환."""
    # 1. 조명 평탄화 — 배경 추정을 paper 미만으로 내려가지 않게 클램프하여
    #    어두운 그림/표지가 밝아지는 사고를 방지 (밝은 종이 얼룩만 교정됨).
    bg = cv2.morphologyEx(gray, cv2.MORPH_CLOSE,
                          cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (41, 41)))
    bg = cv2.medianBlur(bg, 21)
    bg = np.maximum(bg, np.uint8(paper))
    flat = cv2.divide(gray, bg, scale=255)

    # 2. 노이즈 제거
    den = (cv2.fastNlMeansDenoising(flat, h=6, templateWindowSize=7,
                                    searchWindowSize=21)
           if denoise else flat)

    # 3. 이중 톤 커브
    mild = _smoothstep_curve(den, 8, 247)                       # 그림용
    strong = _smoothstep_curve(den, text_black, text_white)     # 글자용
    if thicken > 0:
        eroded = cv2.erode(strong, np.ones((3, 3), np.uint8))
        strong = strong * (1.0 - thicken) + eroded * thicken    # 획 보강

    # 4. 화이트니스 마스크 — 61px 창에서 근백색 비율이 높은 곳 = 말풍선/캡션.
    #    오탐해도 '조금 더 또렷해질 뿐' 파괴가 없으므로 안전.
    white = (den > paper).astype(np.float32)
    wr = cv2.boxFilter(white, -1, (61, 61))
    m = np.clip((wr - 0.55) / 0.30, 0.0, 1.0)
    m = m * m * (3.0 - 2.0 * m)
    m = cv2.GaussianBlur(m, (31, 31), 0)

    out = (strong * m + mild * (1.0 - m)).clip(0, 255).astype(np.uint8)
    return out, (m * 255).astype(np.uint8)


def restore_page(img_bgr: np.ndarray, **kw) -> tuple[np.ndarray, np.ndarray]:
    """컬러 페이지면 LAB의 L채널만 보정해 색 보존. (BGR 보정본, 마스크) 반환."""
    b, g, r = cv2.split(img_bgr)
    chroma = float(np.mean(cv2.absdiff(cv2.max(cv2.max(b, g), r),
                                       cv2.min(cv2.min(b, g), r))))
    if chroma < 4.0:  # 사실상 흑백
        gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
        out, mask = restore_gray(gray, **kw)
        return cv2.cvtColor(out, cv2.COLOR_GRAY2BGR), mask
    lab = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2LAB)
    L, A, B = cv2.split(lab)
    L2, mask = restore_gray(L, **kw)
    return cv2.cvtColor(cv2.merge([L2, A, B]), cv2.COLOR_LAB2BGR), mask


# ---------------------------------------------------------------------------
# PSD 출력 (Background 원본 / Restored 보정 — 비파괴)
# ---------------------------------------------------------------------------
def save_psd(path: Path, original_bgr: np.ndarray, restored_bgr: np.ndarray) -> bool:
    try:
        from PIL import Image
        from psd_tools import PSDImage
        from psd_tools.api.layers import PixelLayer
    except ImportError:
        print("WARNING: psd-tools/Pillow 미설치 — PSD 생략", file=sys.stderr)
        return False

    def to_pil(bgr: np.ndarray) -> "Image.Image":
        return Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)).convert("RGBA")

    H, W = original_bgr.shape[:2]
    psd = PSDImage.new("RGBA", (W, H))
    psd.append(PixelLayer.frompil(to_pil(original_bgr), psd, "Background", 0, 0, 0))
    psd.append(PixelLayer.frompil(to_pil(restored_bgr), psd, "Restored", 0, 0, 0))
    path.parent.mkdir(parents=True, exist_ok=True)
    psd.save(str(path))
    return True


# ---------------------------------------------------------------------------
# 메인
# ---------------------------------------------------------------------------
def process_page(page: Path, out_dir: Path, args) -> dict:
    img = imread_unicode(page)
    if img is None:
        return {"file": page.name, "status": "read_error"}

    restored, mask = restore_page(
        img,
        text_black=args.text_black, text_white=args.text_white,
        thicken=args.thicken, paper=args.paper,
        denoise=not args.no_denoise,
    )

    stem = page.stem
    imwrite_unicode(out_dir / f"{stem}_restored.png", restored)

    psd_saved = False
    if not args.no_psd:
        psd_saved = save_psd(out_dir / f"{stem}.psd", img, restored)

    if args.debug:
        h, w = img.shape[:2]
        sep = np.full((h, 8, 3), 128, np.uint8)
        sbs = np.hstack([img, sep, restored])
        imwrite_unicode(out_dir / "_debug" / f"{stem}_compare.png", sbs)
        imwrite_unicode(out_dir / "_debug" / f"{stem}_textmask.png", mask)

    return {"file": page.name, "status": "ok", "psd": psd_saved}


def main() -> int:
    ap = argparse.ArgumentParser(
        description="스캔 만화 비파괴 화질 복원 (말풍선 한글 선명화)")
    ap.add_argument("--src", required=True, help="원본(업스케일) 이미지 폴더")
    ap.add_argument("--out", required=True, help="출력 폴더")
    ap.add_argument("--limit", type=int, default=0, help="앞 N장만 처리 (테스트용)")
    ap.add_argument("--ext", default=".png,.jpg,.jpeg,.webp",
                    help="처리할 확장자 (쉼표 구분)")
    ap.add_argument("--debug", action="store_true",
                    help="_debug/에 원본|보정 비교 이미지 저장")
    ap.add_argument("--no-psd", action="store_true", help="PSD 생략, PNG만 출력")
    ap.add_argument("--no-denoise", action="store_true", help="노이즈 제거 생략 (빠름)")
    ap.add_argument("--text-black", type=int, default=80,
                    help="글자 커브 검정점 (기본 80, 흐리면 90~100)")
    ap.add_argument("--text-white", type=int, default=210,
                    help="글자 커브 흰점 (기본 210)")
    ap.add_argument("--thicken", type=float, default=0.5,
                    help="획 두께 보강 0.0~1.0 (기본 0.5)")
    ap.add_argument("--paper", type=int, default=215,
                    help="종이 밝기 하한 (기본 215)")
    args = ap.parse_args()

    src, out = Path(args.src), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    exts = {e.strip().lower() for e in args.ext.split(",") if e.strip()}
    files = sorted(p for p in src.iterdir() if p.suffix.lower() in exts)
    if args.limit:
        files = files[:args.limit]
    if not files:
        print(f"이미지 없음: {src}", file=sys.stderr)
        return 1

    results, failed = [], []
    for i, f in enumerate(files, 1):
        print(f"[{i}/{len(files)}] {f.name}", flush=True)
        try:
            r = process_page(f, out, args)
        except Exception as e:
            r = {"file": f.name, "status": "error",
                 "error": str(e), "trace": traceback.format_exc()}
        results.append(r)
        if r["status"] != "ok":
            failed.append(f"{f.name}\t{r['status']}\t{r.get('error', '')}")
            print(f"    -> 실패: {r['status']}", file=sys.stderr)

    (out / "run.log.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    if failed:
        (out / "failed.log").write_text("\n".join(failed), encoding="utf-8")
        print(f"\n실패 {len(failed)}장: {out / 'failed.log'}")
    print(f"\n완료. {len(results) - len(failed)}/{len(results)}장 처리, 로그: {out / 'run.log.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
