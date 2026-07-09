"""
Comic bubble restoration pipeline.

Detects speech bubbles in upscaled (upscayl) manga pages and clears the
bubble interior to white so they can be re-lettered in Photoshop. Two
modes are supported:

  - Clear-only (recommended for degraded scans, pass --no-ocr):
      Every detected bubble is cleared; the Text layer is left empty
      so you can type Korean text manually in Photoshop.

  - Clear + OCR (pass a --tesseract path and don't use --no-ocr):
      Tesseract runs on each bubble. Bubbles with readable Korean get
      their text re-rendered in Nanum Myeongjo Bold on the Text layer.
      Bubbles where Tesseract can't read the text fall through to the
      clear-only path — they are still cleared, just not retyped.

Output is a layered PSD (Background / BubbleClear / Text) plus a
flattened PNG preview.

Usage
-----
    # Recommended: clear-only workflow
    python comic_bubble_pipeline.py \
        --src  "D:/i/comics/도박묵시록 카이지/도박묵시록 카이지 01/upscayl_png_digital-art-4x_2x" \
        --out  "D:/i/comics/도박묵시록 카이지/도박묵시록 카이지 01/restored" \
        --font "C:/Users/kiuk1/AppData/Local/Microsoft/Windows/Fonts/NanumMyeongjoBold.ttf" \
        --no-ocr \
        --limit 3            # optional: process only first N pages while tuning
        --debug              # optional: also save debug overlays

    # Optional: also try OCR (Tesseract + Korean language pack must be installed)
    python comic_bubble_pipeline.py \
        --src  "..." --out "..." --font "..." \
        --tesseract "C:/Program Files/Tesseract-OCR/tesseract.exe" \
        --debug

Dependencies (install on local machine):
    pip install opencv-python pillow numpy psd-tools pytesseract
    Tesseract OCR engine with Korean language pack:
        Windows: https://github.com/UB-Mannheim/tesseract/wiki
        During install, tick "Additional language data -> Korean"
    Nanum Myeongjo Bold font installed on the system.

Pipeline stages (per page):
    1. detect_bubble_candidates  — loose OpenCV detection of bright closed regions
    2. ocr_filter                — run Tesseract kor on each candidate, keep those
                                   with real Korean text
    3. clear_and_render          — paint bubble white, render OCR'd text with
                                   Nanum Myeongjo Bold, auto-fit inside bubble
    4. save_psd                  — background + bubble-mask + text layers

Skipped pages (no bubbles detected / OCR failed entirely) are logged to
skipped.log in the output directory with the reason.
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont


# ---------------------------------------------------------------------------
# IO helpers (handle non-ASCII Windows paths via numpy)
# ---------------------------------------------------------------------------
def imread_unicode(path: Path) -> Optional[np.ndarray]:
    data = np.fromfile(str(path), dtype=np.uint8)
    if data.size == 0:
        return None
    return cv2.imdecode(data, cv2.IMREAD_COLOR)


def imwrite_unicode(path: Path, img: np.ndarray) -> None:
    ext = path.suffix or ".png"
    ok, buf = cv2.imencode(ext, img)
    if not ok:
        raise IOError(f"cv2.imencode failed for {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(buf.tobytes())


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------
@dataclass
class Bubble:
    bbox: tuple        # (x, y, w, h)
    area: int
    solidity: float
    text_ratio: float
    mask: np.ndarray = field(repr=False)   # full-frame uint8 mask
    text: str = ""                         # filled by OCR
    ocr_conf: float = 0.0                  # average word confidence


def _method_erosion(white: np.ndarray, white_closed: np.ndarray,
                    H: int, W: int,
                    min_area: int, max_area: int) -> list[np.ndarray]:
    """Method A — erode the white mask to disconnect bubbles from gutters,
    find components, dilate back. Good for bubbles sitting in wide background."""
    erode_k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (25, 25))
    eroded = cv2.erode(white, erode_k, iterations=1)
    n, lab, stats, _ = cv2.connectedComponentsWithStats(eroded, connectivity=8)
    out: list[np.ndarray] = []
    for i in range(1, n):
        a = stats[i, cv2.CC_STAT_AREA]
        if a < min_area * 0.5 or a > max_area:
            continue
        comp = (lab == i).astype(np.uint8) * 255
        dilate_k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (30, 30))
        bubble_mask = cv2.dilate(comp, dilate_k, iterations=1)
        bubble_mask = cv2.bitwise_and(bubble_mask, white_closed)
        out.append(bubble_mask)
    return out


def _method_holes(gray: np.ndarray, H: int, W: int,
                  min_area: int, max_area: int) -> list[np.ndarray]:
    """Method B — find holes (enclosed regions) inside the dark border network
    using cv2.RETR_CCOMP hierarchy. Good for bubbles whose dark outline is
    a fully-closed loop, even when connected to background through gutters."""
    _, dark = cv2.threshold(gray, 100, 255, cv2.THRESH_BINARY_INV)
    dark = cv2.morphologyEx(
        dark, cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    )
    cnts, hier = cv2.findContours(dark, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    out: list[np.ndarray] = []
    if hier is None:
        return out
    hier = hier[0]
    for idx, cnt in enumerate(cnts):
        if hier[idx][3] == -1:
            continue  # outer contour, not a hole
        a = cv2.contourArea(cnt)
        if a < min_area or a > max_area:
            continue
        m = np.zeros((H, W), np.uint8)
        cv2.drawContours(m, [cnt], -1, 255, thickness=cv2.FILLED)
        out.append(m)
    return out


def _score_candidate(bubble_mask: np.ndarray, gray: np.ndarray,
                     white_closed: np.ndarray,
                     H: int, W: int, min_area: int) -> Optional[Bubble]:
    """Apply shape + text filters. Return Bubble or None."""
    if bubble_mask.sum() == 0:
        return None
    xs, ys, ws, hs = cv2.boundingRect(bubble_mask)
    if ws == 0 or hs == 0:
        return None
    # Must not touch page border (that's background)
    if xs <= 3 or ys <= 3 or xs + ws >= W - 3 or ys + hs >= H - 3:
        return None
    aspect = ws / float(hs)
    if aspect < 0.25 or aspect > 5.0:
        return None
    b_area = int(bubble_mask.sum() / 255)
    if b_area < min_area:
        return None

    bbox_fill = b_area / float(ws * hs)
    if bbox_fill < 0.45:
        return None

    # Interior must be mostly white (rules out shadows, clothing, etc.)
    white_in = int(((bubble_mask > 0) & (white_closed > 0)).sum())
    white_ratio = white_in / float(b_area)
    if white_ratio < 0.55:
        return None

    # Must contain dark text
    text_pixels = int(((bubble_mask > 0) & (gray < 90)).sum())
    text_ratio = text_pixels / float(b_area)
    if text_ratio < 0.008 or text_ratio > 0.35:
        return None

    # Solidity — bubbles are convex-ish
    contours, _ = cv2.findContours(bubble_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    cnt = max(contours, key=cv2.contourArea)
    hull_area = cv2.contourArea(cv2.convexHull(cnt))
    if hull_area <= 0:
        return None
    solidity = cv2.contourArea(cnt) / hull_area
    if solidity < 0.78:
        return None

    # Text-row signature: real speech bubbles contain rows of horizontally
    # packed text, so the peak row of dark pixels should span a significant
    # fraction of the bubble width. Scattered features (card suits, face
    # features) do not.
    crop_gray = gray[ys:ys + hs, xs:xs + ws]
    crop_mask = bubble_mask[ys:ys + hs, xs:xs + ws]
    dark_interior = ((crop_gray < 110) & (crop_mask > 0)).astype(np.uint8)
    if dark_interior.size == 0:
        return None
    row_sums = dark_interior.sum(axis=1)
    peak_row = int(row_sums.max()) if row_sums.size else 0
    peak_row_density = peak_row / float(ws)
    if peak_row_density < 0.12:
        return None

    return Bubble(
        bbox=(int(xs), int(ys), int(ws), int(hs)),
        area=b_area,
        solidity=float(solidity),
        text_ratio=float(text_ratio),
        mask=bubble_mask,
    )


def detect_bubble_candidates(img_bgr: np.ndarray) -> list[Bubble]:
    """Hybrid bubble detection. Combines two independent methods and applies
    shared shape/text filters. Over-generates candidates; OCR filters later."""
    H, W = img_bgr.shape[:2]
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)

    # Near-white mask + closing to fill interior text.
    _, white = cv2.threshold(gray, 230, 255, cv2.THRESH_BINARY)
    close_k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (45, 45))
    white_closed = cv2.morphologyEx(white, cv2.MORPH_CLOSE, close_k)

    page_area = H * W
    min_area = int(page_area * 0.0010)
    max_area = int(page_area * 0.06)

    raw_masks: list[np.ndarray] = []
    raw_masks += _method_erosion(white, white_closed, H, W, min_area, max_area)
    raw_masks += _method_holes(gray, H, W, min_area, max_area)

    candidates: list[Bubble] = []
    for m in raw_masks:
        res = _score_candidate(m, gray, white_closed, H, W, min_area)
        if res is not None:
            candidates.append(res)

    # Deduplicate overlapping candidates (keep larger first).
    candidates.sort(key=lambda b: -b.area)
    kept: list[Bubble] = []
    used = np.zeros((H, W), np.uint8)
    for b in candidates:
        inter = int(((used > 0) & (b.mask > 0)).sum())
        if inter > 0.25 * b.area:
            continue
        used |= b.mask
        kept.append(b)
    return kept


# ---------------------------------------------------------------------------
# OCR filter
# ---------------------------------------------------------------------------
def _preprocess_for_ocr(crop_bgr: np.ndarray) -> np.ndarray:
    """Prepare a bubble crop for Tesseract on a degraded scanned manga page.

    Steps:
      1. Upscale to a minimum width (OCR likes large glyphs).
      2. Convert to gray, apply CLAHE to boost faded contrast.
      3. Bilateral filter to reduce scan noise while keeping strokes crisp.
      4. Otsu binarization (robust to per-bubble lighting drift).
      5. Invert if background ended up black (Tesseract expects dark text on
         light bg).
    """
    ch, cw = crop_bgr.shape[:2]
    target_w = 600
    if cw < target_w:
        scale = target_w / cw
        crop_bgr = cv2.resize(crop_bgr,
                              (int(cw * scale), int(ch * scale)),
                              interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY)

    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    gray = clahe.apply(gray)

    gray = cv2.bilateralFilter(gray, d=5, sigmaColor=60, sigmaSpace=60)

    _, bin_ = cv2.threshold(gray, 0, 255,
                            cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    # Keep the background as white: if more than half the pixels are black,
    # invert so Tesseract sees the expected dark-on-light.
    if int((bin_ == 0).sum()) > int((bin_ == 255).sum()):
        bin_ = cv2.bitwise_not(bin_)
    return bin_


def _score_ocr_text(text: str, words: list, confs: list) -> dict:
    """Summarise OCR output for filter decisions."""
    kor_chars = sum(1 for ch in text if "\uac00" <= ch <= "\ud7a3")
    total_non_space = sum(1 for ch in text if not ch.isspace())
    kor_ratio = (kor_chars / total_non_space) if total_non_space else 0.0
    avg_conf = (sum(confs) / len(confs)) if confs else 0.0
    return {
        "text": text,
        "kor_chars": kor_chars,
        "kor_ratio": kor_ratio,
        "avg_conf": avg_conf,
        "words": len(words),
    }


def ocr_filter(img_bgr: np.ndarray, bubbles: list[Bubble],
               tesseract_cmd: Optional[str] = None,
               log_path: Optional[Path] = None) -> list[Bubble]:
    """Three-tier OCR gate for scanned / upscayl-upscaled manga.

    For each shape-validated bubble candidate we run Tesseract Korean and
    classify the result into one of three buckets:

      ACCEPT (with text)
          Tesseract found plausible Korean text. The bubble will be
          cleared and the OCR text re-rendered with Nanum Myeongjo.

      CLEAR-ONLY
          OCR returned nothing usable, but the shape is still strong.
          The bubble is kept: its interior will be cleared (painted
          white), but the text layer is left empty so the user can
          manually re-type in Photoshop. For scanned/upscayled sources
          this is the common case because Tesseract often can't handle
          degraded glyphs reliably.

      REJECT
          OCR returned *confident* non-Korean content (numbers, English,
          card pips, etc). These are almost certainly false positives
          that slipped through shape filters — drop them.

    Pass ``log_path`` to dump every OCR decision to a text file so you
    can see why a given bubble was kept or dropped.
    """
    try:
        import pytesseract
    except ImportError:
        print("WARNING: pytesseract not installed — skipping OCR filter.",
              file=sys.stderr)
        return bubbles
    if tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = tesseract_cmd

    log_lines: list[str] = []
    out: list[Bubble] = []
    for b in bubbles:
        x, y, w, h = b.bbox
        pad = 10
        x0, y0 = max(0, x - pad), max(0, y - pad)
        x1 = min(img_bgr.shape[1], x + w + pad)
        y1 = min(img_bgr.shape[0], y + h + pad)
        crop = img_bgr[y0:y1, x0:x1]
        bin_ = _preprocess_for_ocr(crop)

        try:
            data = pytesseract.image_to_data(
                bin_, lang="kor",
                config="--psm 6",
                output_type=pytesseract.Output.DICT,
            )
        except pytesseract.TesseractError as e:
            print(f"  OCR error for bubble {b.bbox}: {e}", file=sys.stderr)
            # Still clear-only: shape already passed.
            b.text = ""
            b.ocr_conf = 0.0
            out.append(b)
            log_lines.append(f"  {b.bbox} TESS-ERROR -> CLEAR-ONLY")
            continue

        words, confs = [], []
        for txt, conf in zip(data["text"], data["conf"]):
            try:
                conf = float(conf)
            except (TypeError, ValueError):
                continue
            if conf < 0 or not txt or not txt.strip():
                continue
            if conf < 30:  # was 40 — allow more of degraded scan's text
                continue
            words.append(txt.strip())
            confs.append(conf)

        text = " ".join(words).strip()
        score = _score_ocr_text(text, words, confs)
        kc = score["kor_chars"]
        kr = score["kor_ratio"]
        av = score["avg_conf"]

        # Bucket decision
        # ------------------------------------------------------------
        # REJECT: only when Tesseract is *very* confident the content
        # is non-Korean (likely an English logo or card numerals that
        # slipped through shape filters). Everything else falls through
        # to CLEAR-ONLY so the user can fix it manually in Photoshop.
        if av >= 70 and kc == 0 and len(words) >= 2:
            decision = "REJECT"
        # ACCEPT + text: Tesseract found something that looks Korean.
        # Thresholds are much looser than the previous strict gate
        # because degraded scans give low confidences even on real text.
        elif kc >= 2 and kr >= 0.40 and av >= 35:
            b.text = text
            b.ocr_conf = av
            out.append(b)
            decision = f"ACCEPT text='{text[:30]}' kc={kc} kr={kr:.2f} avg={av:.0f}"
        else:
            # CLEAR-ONLY fallback: no reliable text but shape was good,
            # so clear the bubble and leave the text layer blank.
            b.text = ""
            b.ocr_conf = 0.0
            out.append(b)
            decision = (f"CLEAR-ONLY kc={kc} kr={kr:.2f} avg={av:.0f} "
                        f"raw='{text[:30]}'")

        log_lines.append(f"  {b.bbox} -> {decision}")

    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("\n".join(log_lines) or "(no bubbles)",
                            encoding="utf-8")

    return out


# ---------------------------------------------------------------------------
# Clearing + rendering
# ---------------------------------------------------------------------------
def clear_and_render(img_bgr: np.ndarray, bubbles: list[Bubble],
                     font_path: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Returns (background_rgba, bubble_clear_rgba, text_rgba) layer stack.

    All layers have the same size as the page. Composite top-down:
        background  -> bubble_clear  -> text
    """
    H, W = img_bgr.shape[:2]
    rgba = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2BGRA)
    background = rgba.copy()

    # bubble_clear: white fill only inside bubble masks (transparent elsewhere)
    bubble_clear = np.zeros((H, W, 4), np.uint8)
    for b in bubbles:
        # Slight erosion so we don't paint over the bubble's own black border.
        inner = cv2.erode(b.mask,
                          cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)))
        bubble_clear[inner > 0] = (255, 255, 255, 255)

    # text layer: transparent everywhere, text rendered inside bubble bbox.
    text_pil = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(text_pil)
    for b in bubbles:
        if not b.text:
            continue
        render_text_in_bubble(draw, b, font_path)
    text_rgba = cv2.cvtColor(np.array(text_pil), cv2.COLOR_RGBA2BGRA)

    return background, bubble_clear, text_rgba


def render_text_in_bubble(draw: ImageDraw.ImageDraw, bubble: Bubble,
                          font_path: str) -> None:
    """Render bubble.text inside bubble.bbox with auto-fitting font size."""
    x, y, w, h = bubble.bbox
    # Inner rectangle (leave padding so text doesn't touch bubble border)
    pad_w = max(8, int(w * 0.08))
    pad_h = max(6, int(h * 0.08))
    inner_w = max(10, w - 2 * pad_w)
    inner_h = max(10, h - 2 * pad_h)

    # Binary-search font size that fits a wrapped version of the text.
    txt = bubble.text
    lo, hi, best = 8, 80, 12
    best_lines: list[str] = [txt]
    while lo <= hi:
        mid = (lo + hi) // 2
        try:
            font = ImageFont.truetype(font_path, mid)
        except OSError:
            # Fallback to default bitmap font
            font = ImageFont.load_default()
            best_lines = wrap_text(txt, font, inner_w)
            best = mid
            break
        lines = wrap_text(txt, font, inner_w)
        line_h = font_line_height(font)
        total_h = line_h * len(lines)
        max_w = max(line_width(font, ln) for ln in lines) if lines else 0
        if total_h <= inner_h and max_w <= inner_w:
            best, best_lines = mid, lines
            lo = mid + 1
        else:
            hi = mid - 1

    try:
        font = ImageFont.truetype(font_path, best)
    except OSError:
        font = ImageFont.load_default()

    # Center the block vertically and each line horizontally.
    line_h = font_line_height(font)
    block_h = line_h * len(best_lines)
    start_y = y + pad_h + max(0, (inner_h - block_h) // 2)
    for i, ln in enumerate(best_lines):
        lw = line_width(font, ln)
        lx = x + pad_w + max(0, (inner_w - lw) // 2)
        ly = start_y + i * line_h
        draw.text((lx, ly), ln, font=font, fill=(0, 0, 0, 255))


def wrap_text(text: str, font: ImageFont.ImageFont, max_width: int) -> list[str]:
    """Greedy character-based wrap (works for Korean where spaces are optional)."""
    if not text:
        return []
    # Try space-wrap first.
    tokens = text.split()
    lines: list[str] = []
    cur = ""
    for tok in tokens:
        trial = tok if not cur else f"{cur} {tok}"
        if line_width(font, trial) <= max_width:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            # Token itself too long — break by character
            if line_width(font, tok) > max_width:
                buf = ""
                for ch in tok:
                    if line_width(font, buf + ch) <= max_width:
                        buf += ch
                    else:
                        if buf:
                            lines.append(buf)
                        buf = ch
                cur = buf
            else:
                cur = tok
    if cur:
        lines.append(cur)
    return lines


def line_width(font: ImageFont.ImageFont, s: str) -> int:
    try:
        left, top, right, bottom = font.getbbox(s)
        return right - left
    except Exception:
        return font.getsize(s)[0]


def font_line_height(font: ImageFont.ImageFont) -> int:
    try:
        asc, desc = font.getmetrics()
        return int((asc + desc) * 1.15)
    except Exception:
        return int(font.size * 1.2)


# ---------------------------------------------------------------------------
# PSD output
# ---------------------------------------------------------------------------
def save_psd(path: Path, background: np.ndarray,
             bubble_clear: np.ndarray, text_rgba: np.ndarray) -> None:
    """Save a three-layer PSD: Background / BubbleClear / Text."""
    try:
        from psd_tools import PSDImage
        from psd_tools.api.layers import PixelLayer
    except ImportError:
        print("WARNING: psd-tools not installed — saving as PNG instead.",
              file=sys.stderr)
        fallback = compose(background, bubble_clear, text_rgba)
        imwrite_unicode(path.with_suffix(".png"), fallback)
        return

    def to_pil(arr_bgra: np.ndarray) -> Image.Image:
        return Image.fromarray(cv2.cvtColor(arr_bgra, cv2.COLOR_BGRA2RGBA))

    H, W = background.shape[:2]
    psd = PSDImage.new("RGBA", (W, H))
    for name, arr in [("Background", background),
                      ("BubbleClear", bubble_clear),
                      ("Text", text_rgba)]:
        layer = PixelLayer.frompil(to_pil(arr), psd, name, 0, 0, 0)
        psd.append(layer)
    path.parent.mkdir(parents=True, exist_ok=True)
    psd.save(str(path))


def compose(background: np.ndarray, bubble_clear: np.ndarray,
            text: np.ndarray) -> np.ndarray:
    """Alpha-composite background -> bubble_clear -> text, return BGRA."""
    def over(dst: np.ndarray, src: np.ndarray) -> np.ndarray:
        sa = src[..., 3:4] / 255.0
        da = dst[..., 3:4] / 255.0
        out_a = sa + da * (1 - sa)
        out_rgb = (src[..., :3] * sa + dst[..., :3] * da * (1 - sa))
        out = np.zeros_like(dst)
        # Avoid div-by-zero
        mask = out_a[..., 0] > 0
        out[..., :3][mask] = (out_rgb[mask] / out_a[mask]).clip(0, 255)
        out[..., 3] = (out_a[..., 0] * 255).clip(0, 255)
        return out.astype(np.uint8)
    out = background.copy()
    out = over(out, bubble_clear)
    out = over(out, text)
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def process_page(page_path: Path, out_dir: Path, font_path: str,
                 debug: bool = False, tesseract_cmd: Optional[str] = None,
                 no_ocr: bool = False) -> dict:
    """Process a single page; return a log dict.

    When ``no_ocr`` is True, skip Tesseract entirely — every
    shape-validated bubble is kept and simply cleared (interior
    painted white). This is the preferred mode when the user plans
    to hand-type Korean text later in Photoshop.
    """
    img = imread_unicode(page_path)
    if img is None:
        return {"file": page_path.name, "status": "read_error"}

    candidates = detect_bubble_candidates(img)
    stem = page_path.stem
    if no_ocr:
        # Clear-only mode: every detected bubble is kept with empty text.
        bubbles = candidates
        for b in bubbles:
            b.text = ""
            b.ocr_conf = 0.0
    else:
        ocr_log = out_dir / "_ocr_log" / f"{stem}.txt" if debug else None
        bubbles = ocr_filter(img, candidates,
                             tesseract_cmd=tesseract_cmd,
                             log_path=ocr_log)

    if not bubbles:
        return {"file": page_path.name, "status": "skipped",
                "reason": "no_bubbles_detected",
                "candidates": len(candidates)}

    bg, clear_, text = clear_and_render(img, bubbles, font_path)

    with_text = sum(1 for b in bubbles if b.text)
    clear_only = len(bubbles) - with_text

    psd_path = out_dir / f"{stem}.psd"
    save_psd(psd_path, bg, clear_, text)
    preview = compose(bg, clear_, text)
    imwrite_unicode(out_dir / f"{stem}_preview.png",
                    cv2.cvtColor(preview, cv2.COLOR_BGRA2BGR))

    if debug:
        dbg = img.copy()
        overlay = dbg.copy()
        for b in bubbles:
            overlay[b.mask > 0] = (0, 255, 255)
        dbg = cv2.addWeighted(overlay, 0.3, dbg, 0.7, 0)
        for b in bubbles:
            x, y, w, h = b.bbox
            # Red box = text was re-rendered, blue = clear-only
            colour = (0, 0, 255) if b.text else (255, 128, 0)
            cv2.rectangle(dbg, (x, y), (x + w, y + h), colour, 3)
        imwrite_unicode(out_dir / "_debug" / f"{stem}_debug.png", dbg)

    return {
        "file": page_path.name,
        "status": "ok",
        "candidates": len(candidates),
        "bubbles": len(bubbles),
        "with_text": with_text,
        "clear_only": clear_only,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Comic bubble restoration pipeline")
    ap.add_argument("--src", required=True, help="Source folder with upscayl PNGs")
    ap.add_argument("--out", required=True, help="Output folder for restored PSD/PNG")
    ap.add_argument("--font", required=True, help="Path to NanumMyeongjo Bold .ttf/.otf")
    ap.add_argument("--limit", type=int, default=0, help="Process only N pages (tuning)")
    ap.add_argument("--debug", action="store_true", help="Save detection debug overlays")
    ap.add_argument("--tesseract", default=None,
                    help="Path to tesseract.exe (optional; e.g. on Windows)")
    ap.add_argument("--no-ocr", action="store_true",
                    help="Skip Tesseract entirely — clear every detected "
                         "bubble and leave the text layer empty. Use this "
                         "when you plan to retype Korean text manually in "
                         "Photoshop (the recommended workflow for heavily "
                         "degraded scans).")
    ap.add_argument("--ext", default=".png,.jpg,.jpeg",
                    help="Comma-separated image extensions to include")
    args = ap.parse_args()

    src = Path(args.src)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    exts = {e.strip().lower() for e in args.ext.split(",") if e.strip()}
    files = sorted(p for p in src.iterdir() if p.suffix.lower() in exts)
    if args.limit:
        files = files[:args.limit]
    if not files:
        print(f"No images found in {src}", file=sys.stderr)
        return 1

    log_path = out / "run.log.json"
    skipped_path = out / "skipped.log"
    results = []
    skipped_lines = []
    for i, f in enumerate(files, 1):
        print(f"[{i}/{len(files)}] {f.name}")
        try:
            r = process_page(f, out, args.font,
                             debug=args.debug,
                             tesseract_cmd=args.tesseract,
                             no_ocr=args.no_ocr)
        except Exception as e:
            r = {"file": f.name, "status": "error",
                 "error": str(e),
                 "trace": traceback.format_exc()}
        results.append(r)
        print(f"    -> {r.get('status')}", r.get('reason', ''),
              f"bubbles={r.get('bubbles', 0)}",
              f"text={r.get('with_text', 0)}",
              f"clearOnly={r.get('clear_only', 0)}")
        if r.get("status") in ("skipped", "error", "read_error"):
            skipped_lines.append(f"{f.name}\t{r.get('status')}\t"
                                 f"{r.get('reason', r.get('error', ''))}")

    log_path.write_text(json.dumps(results, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    if skipped_lines:
        skipped_path.write_text("\n".join(skipped_lines), encoding="utf-8")
    print(f"\nDone. Log: {log_path}")
    if skipped_lines:
        print(f"Skipped/failed pages: {skipped_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
