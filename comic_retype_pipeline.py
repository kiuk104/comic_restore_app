r"""
comic_retype_pipeline.py — 말풍선 한글 재조판 파이프라인 (v3)

개념
----
v2(comic_restore_pipeline)의 비파괴 톤 보정을 베이스로 깔고, 그 위에서
말풍선을 감지 → Claude API 비전으로 열화된 한글을 문맥 기반 전사 →
확신도 높은 말풍선만 지우고 나눔명조로 재조판한다 (하이브리드 정책).

  * 확신도 high 대사만 재조판. medium/low는 v2 보정 상태로 유지 + 리포트 기록
  * 효과음·손글씨 스타일 텍스트는 재조판하지 않음 (그림의 일부로 취급)
  * Tesseract 대신 Claude 비전 — 획이 뭉개진 글자도 한국어 문맥으로 복원

사용법
------
    # 0) 준비
    pip install opencv-python numpy pillow psd-tools anthropic
    set ANTHROPIC_API_KEY=sk-ant-...   (PowerShell: $env:ANTHROPIC_API_KEY="sk-ant-...")

    # 1) 3장 테스트
    python comic_retype_pipeline.py --src "원본폴더" --out ".\test_v3" ^
        --font "C:\Windows\Fonts\NanumMyeongjoBold.ttf" --limit 3 --debug

    # 2) 전체 처리
    python comic_retype_pipeline.py --src "원본폴더" --out "결과폴더" --font "...ttf"

    # API 없이: 크롭만 내보내서 수동/외부 전사 후 재투입
    python comic_retype_pipeline.py --src ... --out ... --font ... --export-crops
    (crops/manifest.json 의 text 칸을 채운 뒤)
    python comic_retype_pipeline.py --src ... --out ... --font ... --transcript "out\crops\manifest.json"

출력 PSD 레이어 (위→아래):
    Text         재조판한 나눔명조 텍스트
    BubbleClear  재조판 말풍선 내부 흰 덮개
    Restored     v2 톤 보정본 (재조판 안 된 말풍선은 이 상태로 보임)
    Background   원본
리포트: review.json — 말풍선별 전사 결과/확신도/처리 여부. medium/low만 골라 수동 확인.
"""

from __future__ import annotations

__version__ = "0.9.0"

import argparse
import base64
import json
import os
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

# v2 보정 모듈 (같은 폴더)
sys.path.insert(0, str(Path(__file__).parent))
from comic_restore_pipeline import imread_unicode, imwrite_unicode, restore_page


# ---------------------------------------------------------------------------
# 말풍선 감지 (v2 보정본 위에서 — 깨끗해서 감지가 안정적)
# ---------------------------------------------------------------------------
@dataclass
class Bubble:
    bbox: tuple                      # x, y, w, h
    mask: np.ndarray = field(repr=False)
    text: Optional[str] = None
    confidence: str = ""             # high / medium / low
    kind: str = ""                   # dialogue / display / sfx / none
    retyped: bool = False
    font_cap: int = 0                # 원본 글자 크기 기반 폰트 상한(px)
    line_boxes: list = field(default_factory=list)  # 원본 글줄별 (x,y,w,h)
    pos_overlap: float = 0.0         # 위치 QC: 글자가 어두운 배경에 겹친 비율
    trans_meta: dict = field(default_factory=dict)  # 전사 합의/검증 기록


def detect_bubbles(restored_gray: np.ndarray) -> list[Bubble]:
    """글자 블록 직접 감지 방식.

    말풍선 '형태'를 추정하지 않고, 흰 배경 포켓 안의 글자 성분 클러스터를
    찾는다. 지우기 마스크도 글자 획 주변만 포함하므로 말풍선 테두리를
    건드리지 않는다. bbox는 글자 블록 영역(재조판 배치 기준)이다.
    """
    rg = restored_gray
    H, W = rg.shape[:2]
    dark = (rg < 100).astype(np.uint8)
    white = (rg > 215).astype(np.uint8) * 255
    white_closed = cv2.morphologyEx(
        white, cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (35, 35)))

    # 1) 글자 후보 성분: 크기 제한 + 흰 포켓 안에 있어야 함
    n, lab, st, _ = cv2.connectedComponentsWithStats(dark, connectivity=8)
    letters = np.zeros((H, W), np.uint8)
    for i in range(1, n):
        a = st[i, cv2.CC_STAT_AREA]
        w = st[i, cv2.CC_STAT_WIDTH]
        h = st[i, cv2.CC_STAT_HEIGHT]
        if not (12 <= a <= 2500 and w <= 70 and h <= 70):
            continue
        if a / float(w * h) < 0.15:      # 대각 스피드선 조각 등 저밀도 제거
            continue
        comp = (lab == i)
        if white_closed[comp].mean() < 0.85 * 255:
            continue
        letters[comp] = 255

    # 2) 클러스터링 (흰 영역 안으로 제한)
    # 구조선(말풍선 테두리·그림 선): 길거나 큰 어두운 성분 — 지우기 금지 대상
    structural = np.zeros((H, W), np.uint8)
    for i in range(1, n):
        a = st[i, cv2.CC_STAT_AREA]
        w = st[i, cv2.CC_STAT_WIDTH]
        h = st[i, cv2.CC_STAT_HEIGHT]
        dens = a / float(max(w * h, 1))
        # 선은 bbox 대비 저밀도, 붙어버린 글자 덩어리는 고밀도
        if (max(w, h) >= 90 and dens < 0.18) or max(w, h) >= 300 or a > 12000:
            structural[lab == i] = 255
    structural = cv2.dilate(structural, np.ones((3, 3), np.uint8))

    # 클러스터는 '원시 흰색 ∪ 글자' 영역으로 제한 — 말풍선 테두리(어두운
    # 비글자 픽셀)를 넘지 못하므로 이웃 말풍선이 한 블록으로 합쳐지지 않음
    allowed = cv2.bitwise_or((white > 0).astype(np.uint8) * 255, letters)
    clusters = cv2.dilate(letters, np.ones((29, 29), np.uint8))
    clusters = cv2.bitwise_and(clusters, allowed)

    out: list[Bubble] = []
    n2, lab2, st2, _ = cv2.connectedComponentsWithStats(clusters, connectivity=8)
    for i in range(1, n2):
        x, y, w, h = st2[i, 0], st2[i, 1], st2[i, 2], st2[i, 3]
        if w < 36 or h < 24:
            continue
        blk8 = (lab2 == i).astype(np.uint8)
        letters_in = cv2.bitwise_and(letters, letters, mask=blk8)
        lp = int(letters_in.sum() // 255)
        if lp < 50:
            continue
        nc, lab3, st3, cent3 = cv2.connectedComponentsWithStats(letters_in,
                                                                connectivity=8)
        if nc - 1 < 3:
            continue
        # 공간적 아웃라이어 글자 제거 — 같은 흰 영역의 먼 오탐이
        # 배치 bbox를 왜곡하고 그림을 지우는 것을 방지
        cx = np.median(cent3[1:, 0]); cy = np.median(cent3[1:, 1])
        d = np.hypot(cent3[1:, 0] - cx, cent3[1:, 1] - cy)
        med_d = max(float(np.median(d)), 30.0)
        keep = [j for j in range(1, nc) if d[j - 1] <= med_d * 3.0]
        if len(keep) < 3:
            continue
        if len(keep) < nc - 1:
            letters_in = np.isin(lab3, keep).astype(np.uint8) * 255
            lp = int(letters_in.sum() // 255)
            if lp < 50:
                continue
            nc, lab3, st3, cent3 = cv2.connectedComponentsWithStats(
                letters_in, connectivity=8)
        # 3) 글자 외 어두운 픽셀 검사 — 얼굴/손/소품 오탐 제거.
        #    테두리 접촉을 피하려고 침식된 코어에서 계산.
        blk_solid = cv2.morphologyEx(blk8, cv2.MORPH_CLOSE,
                                     np.ones((21, 21), np.uint8))
        core = cv2.erode(blk_solid, np.ones((15, 15), np.uint8))
        nld = int((dark.astype(bool) & (core > 0) & (letters == 0)).sum())
        lp_core = max(int(letters[core > 0].sum() // 255), 1)
        if nld / lp_core > 0.30:
            continue
        # 4) 글자 성분 높이 균일성
        hs = sorted(st3[j, cv2.CC_STAT_HEIGHT] for j in range(1, nc))
        if not (7 <= hs[len(hs) // 2] <= 64):
            continue

        # 지우기 마스크: 블록 내 글자 + 코어 내 모든 어두운 픽셀(큰 글자 포함),
        # 7px 팽창으로 열화 잔영까지 커버. 테두리는 코어 밖이라 안전.
        lbx, lby, lbw, lbh = cv2.boundingRect(letters_in)
        core_box = np.zeros_like(core)
        core_box[lby:lby + lbh, lbx:lbx + lbw] = core[lby:lby + lbh,
                                                      lbx:lbx + lbw]
        big_in_core = ((dark > 0) & (core_box > 0)).astype(np.uint8) * 255
        erase = cv2.bitwise_or(letters_in, big_in_core)
        erase = cv2.dilate(erase, np.ones((15, 15), np.uint8))
        erase = cv2.bitwise_and(erase, white_closed)
        # 구조선(테두리·그림 선)은 지우지 않음. 글자로 분류되지 못한 작은
        # 글자 조각은 여기 포함되지 않으므로 팽창 지우기로 함께 제거됨.
        erase[(structural > 0) & (core_box == 0)] = 0

        # 배치 bbox·글줄 밴드는 큰 글자(손글씨 등)까지 포함한 글리프 마스크 기준
        glyphs = cv2.bitwise_or(letters_in, big_in_core)
        lx, ly, lw, lh = cv2.boundingRect(glyphs)
        med_h = hs[len(hs) // 2]

        # 원본 글줄 밴드 감지 (가로쓰기 기준 행 투영) — 레이아웃 정합용
        rows = glyphs[ly:ly + lh, lx:lx + lw].sum(axis=1)
        line_boxes: list[tuple[int, int, int, int]] = []
        y0 = None
        gap = 0
        for r, v in enumerate(list(rows) + [0] * 4):
            if v > 0:
                if y0 is None:
                    y0 = r
                gap = 0
            elif y0 is not None:
                gap += 1
                if gap > 3:            # 3px 이하 끊김은 같은 줄로
                    band = glyphs[ly + y0:ly + r - gap + 1, lx:lx + lw]
                    bx, by, bw, bh = cv2.boundingRect(band)
                    if bh >= 5 and bw >= 5:
                        line_boxes.append((int(lx + bx), int(ly + y0 + by),
                                           int(bw), int(bh)))
                    y0, gap = None, 0

        out.append(Bubble(bbox=(int(lx), int(ly), int(lw), int(lh)),
                          mask=erase, font_cap=int(med_h * 1.35),
                          line_boxes=line_boxes))

    # 읽기 순서(위→아래, 오른쪽→왼쪽) 정렬
    out.sort(key=lambda b: (b.bbox[1] // 200, -b.bbox[0]))
    return out


# ---------------------------------------------------------------------------
# Claude API 전사
# ---------------------------------------------------------------------------
PROMPT = """스캔 만화 페이지에서 잘라낸 말풍선 이미지 {n}장입니다. 업스케일 열화로 한글 획이 손상됐을 수 있습니다.

가장 중요한 규칙 — 실제로 보이는 글자를 그대로 전사하세요:
- 문맥상 자연스러운 단어로 바꿔 쓰지 마세요. 이미지의 글자 모양이 항상 우선이고, 문맥은 획이 뭉개진 글자의 후보를 고르는 보조로만 사용합니다.
- 원문 그대로 (맞춤법 교정·의역 금지, 문장부호·말줄임표 유지)
- 원본의 줄바꿈을 그대로 살려 각 줄을 \\n으로 구분 (레이아웃 정합에 사용됨)
- kind 분류: "dialogue"=식자(인쇄체) 대사·캡션, "hand"=손글씨로 쓴 대사·메모·쪽지, "display"=제목·로고 등 장식 텍스트, "sfx"=효과음, "none"=한글 텍스트 없음
- confidence: "high"=모든 글자 확신, "medium"=한두 글자 추정, "low"=상당 부분 추정 불가. 글자 모양이 아닌 문맥으로 추정한 글자가 하나라도 있으면 "high" 금지.
- uncertain: 추정했거나 불확실한 글자를 "2번째 줄 '묵' (후보: 목/묵)" 형식으로 나열한 문자열. 전부 확실하면 null.
- 텍스트가 없거나 읽을 수 없으면 text는 null

JSON 배열만 출력 (설명 금지):
[{{"id": 1, "text": "...", "kind": "dialogue", "confidence": "high", "uncertain": null}}, ...]"""

VERIFY_PROMPT = """말풍선 이미지 {n}장과 각각의 후보 전사입니다. 후보끼리 다르거나 확신이 낮았던 항목들입니다.

이미지의 실제 글자와 후보를 글자 단위로 대조해 최종 전사를 확정하세요.
- 판단 기준은 '이미지에 실제로 보이는 글자 모양'입니다. 문맥상 자연스러움으로 고르지 마세요.
- 후보 중 이미지와 일치하는 쪽이 있으면 선택, 둘 다 틀렸으면 이미지 기준으로 수정.
- 끝까지 판독 불가한 글자가 있으면 confidence "low".
- 줄바꿈(\\n)은 이미지의 줄 구성 그대로.
- fixed: 후보에서 고친 글자 요약(예: "1줄 '목'→'묵'"), 고친 것 없으면 null.

JSON 배열만 출력 (설명 금지):
[{{"id": 1, "text": "...", "kind": "dialogue", "confidence": "high", "fixed": null}}, ...]"""


def crop_bubble(img_bgr: np.ndarray, b: Bubble, pad: int = 12) -> np.ndarray:
    x, y, w, h = b.bbox
    H, W = img_bgr.shape[:2]
    c = img_bgr[max(0, y - pad):min(H, y + h + pad),
                max(0, x - pad):min(W, x + w + pad)]
    long_side = max(c.shape[:2])
    # 클로드 비전은 ~1500px까지 유효 — 과도한 축소는 판독률만 깎는다
    if long_side > 1400:
        s = 1400 / long_side
        c = cv2.resize(c, (int(c.shape[1] * s), int(c.shape[0] * s)),
                       interpolation=cv2.INTER_AREA)
    return c


def _parse_json_reply(raw: str) -> list[dict]:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`").lstrip("json").strip()
    return json.loads(raw)


def _call_claude(client, model: str, content: list, temperature: float,
                 max_tokens: int = 4000) -> list[dict]:
    msg = client.messages.create(
        model=model, max_tokens=max_tokens, temperature=temperature,
        messages=[{"role": "user", "content": content}])
    return _parse_json_reply(msg.content[0].text)


def _img_block(c: np.ndarray) -> dict:
    ok, buf = cv2.imencode(".png", c)
    return {"type": "image", "source": {
        "type": "base64", "media_type": "image/png",
        "data": base64.b64encode(buf.tobytes()).decode()}}


BATCH = 6  # 호출당 크롭 수 — 길어질수록 후반부 정확도가 떨어져 분할


def transcribe_claude(crops: list[np.ndarray], model: str,
                      temperature: float = 0.0) -> list[dict]:
    """말풍선 크롭들을 배치 분할해 전사. 결과는 crops 순서와 정렬."""
    import anthropic
    client = anthropic.Anthropic()  # ANTHROPIC_API_KEY 환경변수 사용
    out: list[dict] = []
    for s in range(0, len(crops), BATCH):
        chunk = crops[s:s + BATCH]
        content = []
        for i, c in enumerate(chunk, 1):
            content.append({"type": "text", "text": f"[이미지 {i}]"})
            content.append(_img_block(c))
        content.append({"type": "text", "text": PROMPT.format(n=len(chunk))})
        items = _call_claude(client, model, content, temperature)
        by_id = {int(it["id"]): it for it in items}
        out += [by_id.get(i, {"text": None, "kind": "none",
                              "confidence": "low"})
                for i in range(1, len(chunk) + 1)]
    return out


def _norm_text(t) -> str:
    return "\n".join(ln.strip() for ln in (t or "").strip().splitlines())


def verify_claude(crops: list[np.ndarray], cand_pairs: list[tuple],
                  model: str) -> list[dict]:
    """불일치/비확신 크롭들을 후보와 함께 재판독(3차 대조)."""
    import anthropic
    client = anthropic.Anthropic()
    out: list[dict] = []
    for s in range(0, len(crops), BATCH):
        chunk = crops[s:s + BATCH]
        pairs = cand_pairs[s:s + BATCH]
        content = []
        for i, (c, (a, b)) in enumerate(zip(chunk, pairs), 1):
            content.append({"type": "text", "text": f"[이미지 {i}]"})
            content.append(_img_block(c))
            cand = f"후보A: {json.dumps(a, ensure_ascii=False)}"
            if b is not None:
                cand += f"\n후보B: {json.dumps(b, ensure_ascii=False)}"
            content.append({"type": "text", "text": cand})
        content.append({"type": "text",
                        "text": VERIFY_PROMPT.format(n=len(chunk))})
        items = _call_claude(client, model, content, temperature=0.0)
        by_id = {int(it["id"]): it for it in items}
        out += [by_id.get(i, {"text": None, "kind": "none",
                              "confidence": "low"})
                for i in range(1, len(chunk) + 1)]
    return out


def transcribe_consensus(crops: list[np.ndarray], model: str) -> list[dict]:
    """이중 전사 → 일치 채택, 불일치·비확신만 3차 대조 검증.

    반환 dict에 meta 필드 추가: passes(agree/adjudicated/single),
    alt(채택 안 된 후보), fixed(검증에서 고친 내용).
    """
    r1 = transcribe_claude(crops, model, temperature=0.0)
    try:
        r2 = transcribe_claude(crops, model, temperature=0.5)
    except Exception as e:
        print(f"    !! 2차 전사 실패({e}) — 1차 결과만 사용")
        for r in r1:
            r["passes"] = "single"
            if r.get("confidence") == "high" and r.get("uncertain"):
                r["confidence"] = "medium"
        return r1

    final: list[dict] = list(r1)
    need_idx: list[int] = []
    for i, (a, b) in enumerate(zip(r1, r2)):
        agree = (_norm_text(a.get("text")) == _norm_text(b.get("text"))
                 and a.get("kind") == b.get("kind"))
        no_text = not (a.get("text") or "").strip() \
            and not (b.get("text") or "").strip()
        conf_ok = a.get("confidence") == "high" \
            and b.get("confidence") == "high" \
            and not a.get("uncertain") and not b.get("uncertain")
        if no_text or (agree and conf_ok):
            a["passes"] = "agree"
            final[i] = a
        else:
            need_idx.append(i)

    if need_idx:
        try:
            fixed = verify_claude(
                [crops[i] for i in need_idx],
                [(r1[i], None if _norm_text(r1[i].get("text"))
                  == _norm_text(r2[i].get("text")) else r2[i])
                 for i in need_idx], model)
            for i, f in zip(need_idx, fixed):
                f["passes"] = "adjudicated"
                f["alt"] = {"pass1": r1[i].get("text"),
                            "pass2": r2[i].get("text")}
                final[i] = f
        except Exception as e:
            print(f"    !! 검증 패스 실패({e}) — 1차 결과 사용, 확신도 강등")
            for i in need_idx:
                r = dict(r1[i])
                if r.get("confidence") == "high":
                    r["confidence"] = "medium"
                r["passes"] = "unverified"
                r["alt"] = {"pass2": r2[i].get("text")}
                final[i] = r
    return final


# ---------------------------------------------------------------------------
# 로컬 OCR 전사 (Claude 미사용 — API 비용 0, 정확도는 낮음)
# ---------------------------------------------------------------------------
def _has_hangul_text(s: str) -> bool:
    return any("가" <= ch <= "힣" for ch in s)


def _ocr_confidence(mean_conf: float) -> str:
    return ("high" if mean_conf >= 85
            else "medium" if mean_conf >= 60 else "low")


def _find_tesseract() -> str:
    """PATH에 없을 때 Windows 기본 설치 경로에서 tesseract.exe 탐색."""
    la = os.environ.get("LOCALAPPDATA", "")
    cands = []
    if la:
        cands.append(Path(la) / "Programs" / "Tesseract-OCR"
                     / "tesseract.exe")
        cands.append(Path(la) / "Tesseract-OCR" / "tesseract.exe")
    cands.append(Path("C:/Program Files/Tesseract-OCR/tesseract.exe"))
    cands.append(Path("C:/Program Files (x86)/Tesseract-OCR/tesseract.exe"))
    for c in cands:
        if c.exists():
            return str(c)
    return ""


_TESS_GUIDE = (
    "Tesseract 본체가 없습니다. 설치 방법:\n"
    "  1) https://github.com/UB-Mannheim/tesseract/wiki 에서 "
    "설치 프로그램 다운로드\n"
    "  2) 설치 중 'Additional language data'에서 Korean 체크\n"
    "  3) 기본 경로(C:\\Program Files\\Tesseract-OCR)에 설치하면 "
    "앱이 자동 감지합니다")


def _ocr_tesseract(img_bgr: np.ndarray) -> tuple[str, float]:
    import shutil
    import pytesseract
    if not shutil.which("tesseract"):
        exe = _find_tesseract()
        if exe:
            pytesseract.pytesseract.tesseract_cmd = exe
        else:
            raise RuntimeError(_TESS_GUIDE)
    g = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    g = cv2.resize(g, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    _, bw = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    try:
        d = pytesseract.image_to_data(bw, lang="kor", config="--psm 6",
                                      output_type=pytesseract.Output.DICT)
    except pytesseract.TesseractNotFoundError:
        raise RuntimeError(_TESS_GUIDE)
    except pytesseract.TesseractError as e:
        if "kor" in str(e).lower() or "language" in str(e).lower():
            raise RuntimeError(
                "Tesseract 한국어(kor) 데이터가 없습니다. 설치 프로그램을 "
                "다시 실행해 'Additional language data'에서 Korean을 "
                "추가하거나, kor.traineddata를 tessdata 폴더에 넣으세요.\n"
                "https://github.com/tesseract-ocr/tessdata_best/raw/main/"
                "kor.traineddata")
        raise
    lines: dict = {}
    confs = []
    for i, w in enumerate(d["text"]):
        w = (w or "").strip()
        if not w:
            continue
        key = (d["block_num"][i], d["par_num"][i], d["line_num"][i])
        lines.setdefault(key, []).append(w)
        try:
            cf = float(d["conf"][i])
            if cf >= 0:
                confs.append(cf)
        except (TypeError, ValueError):
            pass
    text = "\n".join(" ".join(ws) for _, ws in sorted(lines.items()))
    return text, (sum(confs) / len(confs) if confs else 0.0)


_EASYOCR_READER = None


def _ocr_easyocr(img_bgr: np.ndarray) -> tuple[str, float]:
    global _EASYOCR_READER
    import easyocr
    if _EASYOCR_READER is None:
        _EASYOCR_READER = easyocr.Reader(["ko"], gpu=False, verbose=False)
    res = _EASYOCR_READER.readtext(img_bgr)
    items = sorted(res, key=lambda r: (min(p[1] for p in r[0]),
                                       min(p[0] for p in r[0])))
    lines, cur, confs = [], [], []
    last_y = last_h = None
    for box, txt, cf in items:
        y = min(p[1] for p in box)
        h = max(p[1] for p in box) - y
        if last_y is not None and y > last_y + max(last_h, h) * 0.6:
            lines.append(" ".join(cur))
            cur = []
        cur.append(txt)
        last_y, last_h = y, h
        confs.append(cf * 100)
    if cur:
        lines.append(" ".join(cur))
    return "\n".join(lines), (sum(confs) / len(confs) if confs else 0.0)


def _ocr_windows(img_bgr: np.ndarray) -> tuple[str, float]:
    import winocr
    r = winocr.recognize_cv2_sync(img_bgr, "ko")
    lines = (getattr(r, "lines", None)
             or (r.get("lines") if isinstance(r, dict) else None) or [])
    parts = []
    for ln in lines:
        t = (getattr(ln, "text", None)
             or (ln.get("text") if isinstance(ln, dict) else "") or "")
        if t.strip():
            parts.append(t.strip())
    if not parts:
        t = (getattr(r, "text", "")
             or (r.get("text", "") if isinstance(r, dict) else "") or "")
        parts = [t.strip()] if t.strip() else []
    # Windows OCR은 신뢰도를 주지 않음 — medium 고정 (검수 페이지 확인 권장)
    return "\n".join(parts), 75.0


_OCR_HINTS = {
    "tesseract": "pip install pytesseract 후 Tesseract 본체와 한국어(kor) "
                 "데이터를 설치하세요",
    "easyocr": "pip install easyocr (최초 실행 시 모델 다운로드)",
    "windows": "pip install winocr — Windows 한국어 언어팩 필요",
}


def transcribe_local(crops: list[np.ndarray], engine: str) -> list[dict]:
    """로컬 OCR 엔진 전사 — API 비용 0.

    한계: kind 분류 불가(한글 있으면 전부 dialogue) → 손글씨·효과음
    보존 판단이 없으므로 결과를 검수 페이지에서 꼭 확인할 것."""
    fn = {"tesseract": _ocr_tesseract, "easyocr": _ocr_easyocr,
          "windows": _ocr_windows}.get(engine)
    if fn is None:
        raise RuntimeError(f"알 수 없는 OCR 엔진: {engine}")
    out = []
    for c in crops:
        try:
            text, conf = fn(c)
        except ImportError:
            raise RuntimeError(
                f"{engine} OCR 사용 불가 — {_OCR_HINTS.get(engine, '')}")
        text = (text or "").strip()
        if not text or not _has_hangul_text(text):
            out.append({"text": None, "kind": "none", "confidence": "low",
                        "passes": f"ocr-{engine}"})
        else:
            out.append({"text": text, "kind": "dialogue",
                        "confidence": _ocr_confidence(conf),
                        "passes": f"ocr-{engine}"})
    return out


# ---------------------------------------------------------------------------
# Batch API 전사 (50% 할인 — 제출 후 완료까지 폴링 대기)
# ---------------------------------------------------------------------------
BATCH_POLL_SEC = 15


class BatchCancelled(Exception):
    pass


def prepare_crops(page: Path, args) -> list[np.ndarray]:
    """배치 준비용: process_page와 동일한 감지 경로로 크롭만 추출.

    감지는 결정적이므로 이후 process_page 재감지와 순서가 일치한다."""
    img = imread_unicode(page)
    if img is None:
        return []
    restored, _ = restore_page(img, text_black=args.text_black,
                               text_white=args.text_white,
                               thicken=args.thicken, paper=args.paper,
                               denoise=not args.no_denoise)
    bubbles = detect_bubbles(cv2.cvtColor(restored, cv2.COLOR_BGR2GRAY))
    return [crop_bubble(restored, b) for b in bubbles]


def _chunk_request(cid: str, content: list, model: str,
                   temperature: float) -> dict:
    return {"custom_id": cid,
            "params": {"model": model, "max_tokens": 4000,
                       "temperature": temperature,
                       "messages": [{"role": "user", "content": content}]}}


def _crops_content(chunk: list, prompt_text: str) -> list:
    content = []
    for i, c in enumerate(chunk, 1):
        content.append({"type": "text", "text": f"[이미지 {i}]"})
        content.append(_img_block(c))
    content.append({"type": "text", "text": prompt_text})
    return content


def _batch_run(client, requests: list[dict], log, is_cancelled) -> dict:
    """Message Batches 제출→폴링→수집. {custom_id: 파싱된 items 또는 None}."""
    import time
    out: dict = {}
    # 배치당 256MB 제한 — 대략적 크기로 분할
    groups, cur, size = [], [], 0
    for r in requests:
        sz = len(json.dumps(r, ensure_ascii=False))
        if cur and size + sz > 180_000_000:
            groups.append(cur)
            cur, size = [], 0
        cur.append(r)
        size += sz
    if cur:
        groups.append(cur)

    for gi, group in enumerate(groups, 1):
        b = client.messages.batches.create(requests=group)
        log(f"    배치 {gi}/{len(groups)} 제출 — 요청 {len(group)}건")
        while True:
            if is_cancelled and is_cancelled():
                try:
                    client.messages.batches.cancel(b.id)
                except Exception:
                    pass
                raise BatchCancelled()
            b = client.messages.batches.retrieve(b.id)
            if b.processing_status == "ended":
                break
            c = b.request_counts
            done = c.succeeded + c.errored + c.canceled + c.expired
            log(f"    배치 대기 중… {done}/{len(group)} 완료")
            time.sleep(BATCH_POLL_SEC)
        for res in client.messages.batches.results(b.id):
            if res.result.type == "succeeded":
                try:
                    out[res.custom_id] = _parse_json_reply(
                        res.result.message.content[0].text)
                except Exception:
                    out[res.custom_id] = None
            else:
                out[res.custom_id] = None
    return out


def transcribe_batch(pages: list[tuple], model: str, log=print,
                     is_cancelled=None, fast: bool = False) -> dict:
    """pages: [(page_name, [crop,...]), ...] → {page_name: [entry,...]}.

    transcribe_consensus와 같은 합의 로직을 Batch API로 수행 (비용 50%).
    entry: id/text/kind/confidence/passes/alt/fixed/uncertain — process_page의
    transcript 인자로 바로 사용 가능."""
    import anthropic
    client = anthropic.Anthropic()

    # ---- 1단계: 전 페이지 1·2차 전사 요청 ----
    reqs = []
    for pi, (name, crops) in enumerate(pages):
        for ci, s in enumerate(range(0, len(crops), BATCH)):
            chunk = crops[s:s + BATCH]
            content = _crops_content(chunk, PROMPT.format(n=len(chunk)))
            reqs.append(_chunk_request(f"p{pi:04d}-a-{ci:03d}", content,
                                       model, 0.0))
            if not fast:
                reqs.append(_chunk_request(f"p{pi:04d}-b-{ci:03d}", content,
                                           model, 0.5))
    if not reqs:
        return {}
    log(f"  배치 전사: {len(pages)}페이지, 요청 {len(reqs)}건")
    got = _batch_run(client, reqs, log, is_cancelled)

    def gather(pi: int, n: int, tag: str) -> list:
        arr: list = []
        for ci, s in enumerate(range(0, n, BATCH)):
            k = min(BATCH, n - s)
            items = got.get(f"p{pi:04d}-{tag}-{ci:03d}")
            if items is None:
                arr += [None] * k
            else:
                by_id = {int(it.get("id", 0)): it for it in items
                         if isinstance(it, dict)}
                arr += [by_id.get(j, {"text": None, "kind": "none",
                                      "confidence": "low"})
                        for j in range(1, k + 1)]
        return arr

    FAIL = {"text": None, "kind": "none", "confidence": "low"}

    # ---- 2단계: 합의 판정, 불일치 수집 ----
    page_finals: list[list] = []
    page_disputes: list[list[int]] = []
    page_r1r2: list[tuple] = []
    verify_reqs = []
    for pi, (name, crops) in enumerate(pages):
        n = len(crops)
        r1 = gather(pi, n, "a")
        r2 = gather(pi, n, "b") if not fast else [None] * n
        finals: list = [None] * n
        disputes: list[int] = []
        for i in range(n):
            a = r1[i]
            b2 = r2[i]
            if a is None and b2 is None:
                finals[i] = dict(FAIL, passes="failed")
                continue
            if a is None or b2 is None:
                r = dict(b2 if a is None else a)
                r["passes"] = "single"
                if r.get("confidence") == "high" and r.get("uncertain"):
                    r["confidence"] = "medium"
                finals[i] = r
                continue
            agree = (_norm_text(a.get("text")) == _norm_text(b2.get("text"))
                     and a.get("kind") == b2.get("kind"))
            no_text = not (a.get("text") or "").strip() \
                and not (b2.get("text") or "").strip()
            conf_ok = a.get("confidence") == "high" \
                and b2.get("confidence") == "high" \
                and not a.get("uncertain") and not b2.get("uncertain")
            if no_text or (agree and conf_ok):
                r = dict(a)
                r["passes"] = "agree"
                finals[i] = r
            else:
                disputes.append(i)
        page_finals.append(finals)
        page_disputes.append(disputes)
        page_r1r2.append((r1, r2))
        # 검증 요청 생성
        for ci, s in enumerate(range(0, len(disputes), BATCH)):
            idxs = disputes[s:s + BATCH]
            content = []
            for j, bi in enumerate(idxs, 1):
                content.append({"type": "text", "text": f"[이미지 {j}]"})
                content.append(_img_block(crops[bi]))
                r1i, r2i = page_r1r2[pi][0][bi], page_r1r2[pi][1][bi]
                cand = f"후보A: {json.dumps(r1i, ensure_ascii=False)}"
                if r2i is not None and _norm_text(r1i.get("text")) \
                        != _norm_text(r2i.get("text")):
                    cand += f"\n후보B: {json.dumps(r2i, ensure_ascii=False)}"
                content.append({"type": "text", "text": cand})
            content.append({"type": "text",
                            "text": VERIFY_PROMPT.format(n=len(idxs))})
            verify_reqs.append(_chunk_request(f"v{pi:04d}-{ci:03d}", content,
                                              model, 0.0))

    # ---- 3단계: 불일치 검증 배치 ----
    if verify_reqs:
        log(f"  검증 배치: 요청 {len(verify_reqs)}건")
        got_v = _batch_run(client, verify_reqs, log, is_cancelled)
        for pi, (name, crops) in enumerate(pages):
            disputes = page_disputes[pi]
            r1, r2 = page_r1r2[pi]
            for ci, s in enumerate(range(0, len(disputes), BATCH)):
                idxs = disputes[s:s + BATCH]
                items = got_v.get(f"v{pi:04d}-{ci:03d}")
                by_id = {}
                if items is not None:
                    by_id = {int(it.get("id", 0)): it for it in items
                             if isinstance(it, dict)}
                for j, bi in enumerate(idxs, 1):
                    f2 = by_id.get(j)
                    if f2 is not None:
                        f2 = dict(f2)
                        f2["passes"] = "adjudicated"
                    else:  # 검증 실패 — 1차 결과 강등 채택
                        f2 = dict(r1[bi] or FAIL)
                        if f2.get("confidence") == "high":
                            f2["confidence"] = "medium"
                        f2["passes"] = "unverified"
                    f2["alt"] = {"pass1": (r1[bi] or {}).get("text"),
                                 "pass2": (r2[bi] or {}).get("text")}
                    page_finals[pi][bi] = f2

    # ---- 4단계: transcript dict 조립 ----
    result: dict = {}
    for pi, (name, crops) in enumerate(pages):
        result[name] = [dict(e, id=i + 1)
                        for i, e in enumerate(page_finals[pi])]
    return result


# ---------------------------------------------------------------------------
# 재조판 렌더링
# ---------------------------------------------------------------------------
def load_font(path: str, size: int, index: int = 0) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size, index=index)


def wrap_korean(text: str, font: ImageFont.FreeTypeFont, max_w: int,
                allow_char_break: bool = False) -> Optional[list[str]]:
    """단어 우선 줄바꿈. allow_char_break=False면 단어가 폭을 넘을 때 None
    (해당 폰트 크기 포기 신호) — '장난으/로' 같은 어절 중간 절단 방지."""
    words, lines, cur = text.split(), [], ""
    def W(s): b = font.getbbox(s); return b[2] - b[0]
    for w in words:
        t = w if not cur else cur + " " + w
        if W(t) <= max_w:
            cur = t
        else:
            if cur:
                lines.append(cur)
            if W(w) > max_w:
                if not allow_char_break:
                    return None
                buf = ""
                for ch in w:
                    if W(buf + ch) <= max_w:
                        buf += ch
                    else:
                        lines.append(buf); buf = ch
                cur = buf
            else:
                cur = w
    if cur:
        lines.append(cur)
    return lines or [text]


def render_line_matched(draw: ImageDraw.ImageDraw, b: Bubble, font_path: str,
                        font_index: int, off: tuple = (0, 0),
                        stroke: int = 0) -> bool:
    """전사 줄 수와 원본 글줄 밴드 수가 일치하면 각 줄을 원위치에 렌더링.

    줄별로: 폰트 크기를 원본 줄 높이에 맞추고(잉크 높이 기준), 줄의
    원본 가로 중심에 정렬 — 포토샵에서 원본 레이어와 겹쳐 봐도 어긋나지
    않는 픽셀 정합을 목표로 한다. 성공 시 True."""
    raw_lines = [ln.rstrip() for ln in (b.text or "").split("\n")
                 if ln.strip()]
    lines = [ln.strip() for ln in raw_lines]
    if len(lines) < 1:
        return False
    if len(b.line_boxes) == 1 and len(lines) > 1:
        # 줄 간격이 붙은 손글씨 등 — 단일 밴드를 전사 줄 수로 균등 분할
        bx, by, bw, bh = b.line_boxes[0]
        step = bh / len(lines)
        line_boxes = [(bx, int(by + i * step), bw, max(8, int(step) - 2))
                      for i in range(len(lines))]
    elif len(lines) != len(b.line_boxes):
        return False
    else:
        line_boxes = b.line_boxes

    def has_hangul(s: str) -> bool:
        return any("가" <= ch <= "힣" for ch in s)

    def fit_by_height(sample: str, lh: int) -> Optional[int]:
        """잉크 높이가 원본 줄 높이(lh)와 같아지는 최대 폰트 크기."""
        lo, hi, best = 8, max(12, int(lh * 2.2)), None
        while lo <= hi:
            mid = (lo + hi) // 2
            f = load_font(font_path, mid, font_index)
            bb = f.getbbox(sample)
            if bb[3] - bb[1] <= lh * 1.06:
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1
        return best

    # 크기 결정: 한글이 있는 줄들의 높이 기준 크기 중앙값 = 원본 글자 크기.
    # 폭은 크기 결정에 관여하지 않음 (폭 초과는 자간 압축으로 해결).
    sizes = []
    for ln, (lx, ly, lw, lh) in zip(lines, line_boxes):
        if has_hangul(ln):
            s = fit_by_height(ln, lh)
            if s:
                sizes.append(s)
    if not sizes:
        return False
    sizes.sort()
    size = sizes[len(sizes) // 2]
    if size < 9:
        return False
    f = load_font(font_path, size, font_index)

    ox, oy = off
    DOTS = "·・."   # 연속 가운뎃점/마침표는 원본 만화처럼 촘촘하게

    def _dot_factors(ln: str) -> list:
        return [0.45 if (ch in DOTS and i + 1 < len(ln)
                         and ln[i + 1] in DOTS) else 1.0
                for i, ch in enumerate(ln)]

    def draw_tracked(ln: str, x: float, y: float, track: float) -> None:
        """자간 배율 track + 연속 점 촘촘 배치로 글자별 렌더링."""
        cx = x
        for ch, fc in zip(ln, _dot_factors(ln)):
            draw.text((cx - ox, y - oy), ch, font=f, fill=(0, 0, 0, 255),
                      stroke_width=stroke, stroke_fill=(0, 0, 0, 255))
            cx += f.getlength(ch) * fc * track

    def eff_width(ln: str) -> float:
        adv = [f.getlength(ch) for ch in ln]
        if not adv:
            return 0.0
        fac = _dot_factors(ln)
        return sum(a * fc for a, fc in zip(adv[:-1], fac[:-1])) + adv[-1]

    for raw_ln, ln, (lx, ly, lw, lh) in zip(raw_lines, lines, line_boxes):
        # 줄 앞 공백(반각·전각) = 사용자 들여쓰기 → 오른쪽 이동량
        lead = raw_ln[:len(raw_ln) - len(raw_ln.lstrip(" 　"))]
        lead_w = sum(f.getlength(ch) for ch in lead)
        bb = f.getbbox(ln)
        ink_h = bb[3] - bb[1]
        has_dots = any(fc != 1.0 for fc in _dot_factors(ln))
        nat_w = eff_width(ln) if has_dots else f.getlength(ln)
        ty = ly + (lh - ink_h) / 2 - bb[1]
        max_w = lw * 1.08 + 6          # 원본 줄 폭의 8%+6px까지 허용
        if nat_w <= max_w:
            tx = lx + (lw - nat_w) / 2 + lead_w
            if has_dots:   # 점 촘촘 배치는 글자별 렌더링 필요
                draw_tracked(ln, tx, ty, 1.0)
            else:
                draw.text((tx - ox, ty - oy), ln, font=f,
                          fill=(0, 0, 0, 255), stroke_width=stroke,
                          stroke_fill=(0, 0, 0, 255))
        else:
            # 자간 압축 (최대 12%) — 크기는 원본 그대로 유지
            track = max(0.88, max_w / nat_w)
            comp_w = nat_w * track
            tx = lx + (lw - comp_w) / 2 + lead_w
            draw_tracked(ln, tx, ty, track)
    return size   # 사용된 폰트 크기 (truthy)


def render_layout(tile: Image.Image, b: Bubble, font_path: str,
                  font_index: int, layout: dict, off: tuple = (0, 0),
                  stroke: int = 0, scale: int = 1) -> bool:
    """수동 레이아웃 렌더링 — 크기/오프셋/줄간격/자간/장평.

    텍스트의 줄바꿈을 그대로 사용하고 bbox 가로 중앙(+dx), 세로
    중앙(+dy)에 배치. size 미지정이면 bbox 높이/줄 수로 자동 산출
    (font_cap 상한 무시 — 사용자가 직접 제어하는 모드).
    track=자간 배율, wscale=장평(가로 배율) — 줄 단위 가로 리사이즈,
    슈퍼샘플링 후 축소되므로 품질 저하 없음.
    줄 앞 공백(반각·전각)은 들여쓰기 오프셋으로, 빈 줄은 세로 간격으로
    존중 — 텍스트만으로 세밀한 위치 조정 가능."""
    lines = [ln.rstrip() for ln in (b.text or "").split("\n")]
    while lines and not lines[-1].strip():
        lines.pop()
    if not any(ln.strip() for ln in lines):
        return False
    x, y, w, h = b.bbox
    size = int(round(float(layout.get("size") or 0) * scale))
    if size <= 0:
        size = max(10, int(h / max(len(lines), 1) / 1.3))
    spacing = float(layout.get("spacing") or 1.15)
    track = max(0.3, float(layout.get("track") or 1.0))
    wscale = max(0.2, float(layout.get("wscale") or 1.0))
    dtr = max(0.1, float(layout.get("dottrack") or 0.45))   # 연속 점 간격
    align = layout.get("align") or "left"   # 정렬 — 왼쪽 기본
    dx = int(round(float(layout.get("dx") or 0) * scale))
    dy = int(round(float(layout.get("dy") or 0) * scale))
    try:
        f = load_font(font_path, max(8, size), font_index)
    except OSError:
        return False
    asc, desc = f.getmetrics()
    if layout.get("fill"):
        # 영역 유지 모드 — 말풍선 영역 높이에 줄을 균등 분배.
        # 폰트 크기를 줄여도 블록 전체 크기는 그대로 (줄간격 자동)
        step = h / max(1, len(lines))
        if asc + desc > step and step > 8:
            # 줄 간격보다 큰 폰트는 줄끼리 겹쳐 깨짐 — 상한 클램프
            size = max(8, int(size * step / (asc + desc)))
            f = load_font(font_path, size, font_index)
            asc, desc = f.getmetrics()
        base = y + dy
        def y_line(i):
            return base + i * step + (step - (asc + desc)) / 2
    else:
        lh = int((asc + desc) * spacing)
        block_h = lh * len(lines)
        y0p = y + dy + (h - block_h) // 2   # 세로 중앙 + 오프셋
        def y_line(i):
            return y0p + i * lh
    ox, oy = off
    pad = stroke + 4
    for i, raw_ln in enumerate(lines):
        ln = raw_ln.lstrip(" 　")
        if not ln:
            continue                     # 빈 줄 — 줄 높이만 차지
        lead = raw_ln[:len(raw_ln) - len(ln)]
        lead_w = sum(f.getlength(ch) for ch in lead) * track
        adv = [f.getlength(ch) for ch in ln]
        # 연속 가운뎃점/마침표는 자간과 무관하게 촘촘히(dtr) 배치
        fac = [dtr if (ch in "·・." and i + 1 < len(ln)
                       and ln[i + 1] in "·・.") else track
               for i, ch in enumerate(ln)]
        nat_w = (sum(a * fc for a, fc in zip(adv[:-1], fac[:-1]))
                 + adv[-1]) if adv else 0
        tmp = Image.new("RGBA", (max(2, int(nat_w) + pad * 2),
                                 asc + desc + pad * 2), (0, 0, 0, 0))
        td = ImageDraw.Draw(tmp)
        cx = float(pad)
        for ch, a, fc in zip(ln, adv, fac):
            td.text((cx, pad), ch, font=f, fill=(0, 0, 0, 255),
                    stroke_width=stroke, stroke_fill=(0, 0, 0, 255))
            cx += a * fc
        if abs(wscale - 1.0) > 1e-3:
            tmp = tmp.resize((max(2, int(tmp.width * wscale)), tmp.height),
                             Image.LANCZOS)
        if align == "center":
            bx0 = x + dx + (w - tmp.width) / 2   # 패딩 대칭 — 보정 불필요
        elif align == "right":
            bx0 = x + dx + (w - tmp.width) + pad  # 글리프 우변을 영역 우변에
        else:   # left (기본) — 글리프 좌변을 영역 좌변에
            bx0 = x + dx - pad
        px = int(bx0 + lead_w * wscale - ox)
        py = int(y_line(i) - oy - pad)
        cx0, cy0 = -min(0, px), -min(0, py)
        if cx0 or cy0:   # 타일 밖으로 나가는 부분은 잘라서 합성
            if cx0 >= tmp.width or cy0 >= tmp.height:
                continue
            tmp = tmp.crop((cx0, cy0, tmp.width, tmp.height))
            px, py = max(0, px), max(0, py)
        if px < tile.width and py < tile.height:
            tile.alpha_composite(tmp, (px, py))
    return size   # 사용된 폰트 크기 (truthy)


def render_text(draw: ImageDraw.ImageDraw, b: Bubble, font_path: str,
                font_index: int, line_spacing: float = 1.35,
                off: tuple = (0, 0)) -> None:
    # bbox는 원래 글자들이 차지하던 영역 — 그 안에 맞춰 넣는다
    x, y, w, h = b.bbox
    pad_w, pad_h = 0, 0
    iw, ih = max(10, w), max(10, h)

    size_cap = b.font_cap if b.font_cap >= 10 else 120

    def search(allow_char_break: bool):
        lo, hi, best, best_lines = 10, size_cap, None, None
        while lo <= hi:
            mid = (lo + hi) // 2
            f = load_font(font_path, mid, font_index)
            lines = wrap_korean(b.text, f, iw, allow_char_break)
            ok = False
            if lines is not None:
                asc, desc = f.getmetrics()
                lh = int((asc + desc) * line_spacing)
                tw = max((f.getbbox(ln)[2] - f.getbbox(ln)[0]) for ln in lines)
                ok = lh * len(lines) <= ih and tw <= iw
            if ok:
                best, best_lines = mid, lines
                lo = mid + 1
            else:
                hi = mid - 1
        return best, best_lines

    best, best_lines = search(False)        # 1차: 어절 절단 없이
    if best is None:
        best, best_lines = search(True)     # 2차: 불가피하면 글자 단위 허용
    if best is None:
        best, best_lines = 12, [b.text]

    f = load_font(font_path, best, font_index)
    asc, desc = f.getmetrics()
    lh = int((asc + desc) * line_spacing)
    block_h = lh * len(best_lines)
    y0 = y + pad_h + max(0, (ih - block_h) // 2)
    for i, ln in enumerate(best_lines):
        bb = f.getbbox(ln)
        lx = x + pad_w + max(0, (iw - (bb[2] - bb[0])) // 2) - bb[0]
        draw.text((lx - off[0], y0 + i * lh - off[1]), ln, font=f,
                  fill=(0, 0, 0, 255))
    return best   # 사용된 폰트 크기


# ---------------------------------------------------------------------------
# PSD
# ---------------------------------------------------------------------------
def save_psd(path: Path, layers: list[tuple[str, np.ndarray, int, int]]) -> bool:
    """layers: (이름, BGR/BGRA 배열, x, y) 아래→위 순서. x,y는 레이어 오프셋."""
    try:
        from psd_tools import PSDImage
        from psd_tools.api.layers import PixelLayer
    except ImportError:
        return False
    H, W = layers[0][1].shape[:2]
    psd = PSDImage.new("RGBA", (W, H))
    for name, arr, x, y in layers:
        code = cv2.COLOR_BGRA2RGBA if arr.shape[2] == 4 else cv2.COLOR_BGR2RGB
        pil = Image.fromarray(cv2.cvtColor(arr, code)).convert("RGBA")
        psd.append(PixelLayer.frompil(pil, psd, name, int(y), int(x), 0))
    path.parent.mkdir(parents=True, exist_ok=True)
    psd.save(str(path))
    return True


# ---------------------------------------------------------------------------
# 페이지 처리
# ---------------------------------------------------------------------------
def process_page(page: Path, out_dir: Path, args,
                 transcript: Optional[dict] = None) -> dict:
    img = imread_unicode(page)
    if img is None:
        return {"file": page.name, "status": "read_error"}
    stem = page.stem

    restored, wmask = restore_page(img, text_black=args.text_black,
                                   text_white=args.text_white,
                                   thicken=args.thicken, paper=args.paper,
                                   denoise=not args.no_denoise)
    rgray = cv2.cvtColor(restored, cv2.COLOR_BGR2GRAY)
    bubbles = detect_bubbles(rgray)

    # --- 크롭 내보내기 모드 ---
    if args.export_crops:
        crop_dir = out_dir / "crops"
        crop_dir.mkdir(parents=True, exist_ok=True)
        manifest = []
        for i, b in enumerate(bubbles, 1):
            fn = f"{stem}_{i:02d}.png"
            imwrite_unicode(crop_dir / fn, crop_bubble(restored, b))
            manifest.append({"page": page.name, "id": i, "crop": fn,
                             "bbox": list(b.bbox),
                             "text": None, "kind": "dialogue",
                             "confidence": "high"})
        mf = crop_dir / "manifest.json"
        old = json.loads(mf.read_text(encoding="utf-8")) if mf.exists() else []
        old = [e for e in old if e["page"] != page.name]
        mf.write_text(json.dumps(old + manifest, ensure_ascii=False, indent=2),
                      encoding="utf-8")
        return {"file": page.name, "status": "crops_exported",
                "bubbles": len(bubbles)}

    # --- 전사 ---
    if transcript is not None:
        # 수동 지정 영역(manual_bbox) — 감지 목록 뒤에 합성 버블로 추가
        for e in sorted((e for e in transcript.get(page.name, [])
                         if e.get("manual_bbox")),
                        key=lambda x: x.get("id", 0)):
            bubbles.append(make_manual_bubble(rgray, e["manual_bbox"]))
    if bubbles:
        if transcript is not None:
            entries = [e for e in transcript.get(page.name, [])]
            by_id = {e["id"]: e for e in entries}
            results = [by_id.get(i, {"text": None, "kind": "none",
                                     "confidence": "low"})
                       for i in range(1, len(bubbles) + 1)]
            # 영역 편집(region_bbox): 해당 말풍선의 감지 영역을 사용자
            # 지정 영역으로 교체 — 마스크·글줄 밴드·배치 기준이 모두 변경
            for i, rr in enumerate(results):
                rb = rr.get("region_bbox") if isinstance(rr, dict) else None
                if rb and i < len(bubbles):
                    bubbles[i] = make_manual_bubble(rgray, rb)
        else:
            crops = [crop_bubble(restored, b) for b in bubbles]
            engine = getattr(args, "ocr_engine", "claude")
            if engine != "claude":
                results = transcribe_local(crops, engine)
            elif getattr(args, "fast_transcribe", False):
                results = transcribe_claude(crops, args.model)
            else:
                results = transcribe_consensus(crops, args.model)
        for b, r in zip(bubbles, results):
            t = r.get("text") or ""
            b.text = t if t.strip() else None   # 앞 공백 유지 (수동 들여쓰기)
            b.kind = r.get("kind", "none")
            b.confidence = r.get("confidence", "low")
            b.trans_meta = {k: r.get(k) for k in
                            ("passes", "alt", "fixed", "uncertain",
                             "manual_bbox", "region_bbox", "clean")
                            if r.get(k)}
            if r.get("font"):   # 말풍선별 폰트 오버라이드 (검수 페이지 지정)
                b.trans_meta["font"] = r["font"]
                b.trans_meta["font_index"] = int(r.get("font_index") or 0)
            if r.get("layout"):  # 말풍선별 수동 레이아웃
                b.trans_meta["layout"] = r["layout"]

    # --- 하이브리드 정책: dialogue(+옵션 hand) + high 만 재조판 ---
    def _retypable(b: Bubble) -> bool:
        if not b.text or not (b.confidence == "high" or not args.strict):
            return False
        if b.kind == "dialogue":
            return True
        # 손글씨 대사·메모·쪽지는 기본 보존.
        # --retype-hand 와 --hand-font 를 함께 지정한 경우에만 재조판.
        return (b.kind == "hand" and args.retype_hand
                and bool(args.hand_font))

    to_retype = [b for b in bubbles if _retypable(b)]

    # 기본 서식 (검수 페이지 '기본값으로 지정') — 개별 지정이 없는
    # 말풍선에 렌더 시점에만 적용 (review에는 저장하지 않아, 기본값을
    # 바꾸면 재합성만으로 전체 반영됨)
    sd = {}
    sdp = out_dir / "_style_default.json"
    if sdp.exists():
        try:
            sd = json.loads(sdp.read_text(encoding="utf-8"))
        except Exception:
            sd = {}
    sd_lay = sd.get("layout") or {}

    H, W = img.shape[:2]
    # 원본 보존 모드(기본): 배경은 업스케일 원본 그대로, 재조판 말풍선
    # 내부만 손댐. 지움 덮개는 흰색 대신 그 말풍선의 원본 종이색으로
    # 채워 이음새를 없앤다. (--no-preserve-bg면 기존 v2 보정본 배경)
    preserve = bool(getattr(args, "preserve_bg", True))

    def _paper_color(m: np.ndarray) -> tuple:
        col = (255, 255, 255)
        if preserve:
            vals = img[m].reshape(-1, 3).astype(np.float32)
            bright = vals[vals.mean(axis=1) >= 160]
            if len(bright) >= 20:
                med = np.median(bright, axis=0)
                col = (int(med[0]), int(med[1]), int(med[2]))
        return col

    clear_layer = np.zeros((H, W, 4), np.uint8)
    # 흰여백 청소(clean): 말풍선 영역 전체를 종이색으로 칠함 —
    # 재조판 여부와 무관 (잔여 얼룩·열화 흔적 제거용)
    for b in bubbles:
        if not b.trans_meta.get("clean"):
            continue
        x, y, w, h = b.bbox
        m = np.zeros((H, W), bool)
        m[max(0, y):min(H, y + h), max(0, x):min(W, x + w)] = True
        clear_layer[m] = (*_paper_color(m), 255)
    for b in to_retype:
        m = b.mask > 0
        clear_layer[m] = (*_paper_color(m), 255)   # 글자 주변만 정밀 지움
        b.retyped = True

    # 브러시(검수 페이지) — _paint/{stem}.png 의 alpha>127 픽셀.
    # 청록 획(B>R)=종이색 칠, 빨강 획(R>B)=원본 복원(자동 지움 포함 취소).
    # 재실행에도 유지됨.
    pm_path = out_dir / "_paint" / f"{stem}.png"
    if pm_path.exists():
        try:
            arr4 = np.array(Image.open(pm_path).convert("RGBA"))
        except Exception:
            arr4 = None
        if arr4 is not None:
            if arr4.shape[:2] != (H, W):
                arr4 = cv2.resize(arr4, (W, H),
                                  interpolation=cv2.INTER_LINEAR)
            on = arr4[..., 3] > 127
            restore = on & (arr4[..., 0].astype(np.int32)
                            > arr4[..., 2].astype(np.int32))
            pmask = (on & ~restore).astype(np.uint8)
            nblob, lab = cv2.connectedComponents(pmask, connectivity=8)
            for bi in range(1, nblob):
                blob = lab == bi
                ys, xs = np.where(blob)
                neigh = np.zeros((H, W), bool)
                neigh[max(0, ys.min() - 15):min(H, ys.max() + 16),
                      max(0, xs.min() - 15):min(W, xs.max() + 16)] = True
                clear_layer[blob] = (*_paper_color(neigh), 255)
            # 복원 획: 모든 종이 덮개(자동 지움·청소·칠) 제거 → 원본 노출.
            # 텍스트 레이어는 이후에 얹히므로 영향 없음.
            if restore.any():
                clear_layer[restore] = 0

    # 재조판 렌더링: 말풍선별 개별 타일 → PSD에서 각각 독립 레이어
    # (위치가 어긋난 대사는 포토샵 이동툴로 레이어만 옮기면 됨)
    text_tiles = []   # (레이어명, BGRA 타일, x, y, Bubble)
    for idx, b in enumerate(to_retype, 1):
        xs0 = [b.bbox[0]] + [lb[0] for lb in b.line_boxes]
        ys0 = [b.bbox[1]] + [lb[1] for lb in b.line_boxes]
        xs1 = [b.bbox[0] + b.bbox[2]] + [lb[0] + lb[2] for lb in b.line_boxes]
        ys1 = [b.bbox[1] + b.bbox[3]] + [lb[1] + lb[3] for lb in b.line_boxes]
        # 수동 레이아웃 오프셋/크기만큼 타일 여백 확장 (잘림 방지)
        lay0 = b.trans_meta.get("layout") or {}
        if sd_lay:   # 기본 서식 병합 — 개별 지정 값이 우선
            merged = dict(sd_lay)
            merged.update(lay0)
            lay0 = merged
        mx = 60 + abs(int(float(lay0.get("dx") or 0)))
        my = 60 + abs(int(float(lay0.get("dy") or 0)))
        s0 = float(lay0.get("size") or 0)
        if s0 > 0:
            est = int(s0 * float(lay0.get("spacing") or 1.15)
                      * max(1, (b.text or "").count("\n") + 1))
            my += max(0, est - b.bbox[3])
        widen = max(1.0, float(lay0.get("wscale") or 1)
                    * float(lay0.get("track") or 1))
        if widen > 1:   # 장평·자간으로 넓어진 만큼 가로 여백 확장
            mx += int(b.bbox[2] * (widen - 1) / 2) + 8
        x0 = max(0, min(xs0) - mx)
        y0 = max(0, min(ys0) - my)
        x1 = min(W, max(xs1) + mx)
        y1 = min(H, max(ys1) + my)
        # 슈퍼샘플링: SS배 크기로 그린 뒤 LANCZOS 축소 — 가장자리가
        # 원화 선처럼 매끈해진다 (1배 직접 렌더링은 상대적으로 거칠음)
        SS = 3
        bs = Bubble(bbox=tuple(v * SS for v in b.bbox), mask=b.mask,
                    text=b.text, kind=b.kind, font_cap=b.font_cap * SS,
                    line_boxes=[tuple(v * SS for v in lb)
                                for lb in b.line_boxes])
        tile = Image.new("RGBA", ((x1 - x0) * SS, (y1 - y0) * SS),
                         (0, 0, 0, 0))
        d = ImageDraw.Draw(tile)
        fp, fi = ((args.hand_font, args.hand_font_index)
                  if b.kind == "hand" else (args.font, args.font_index))
        ovf = b.trans_meta.get("font")
        ofi = int(b.trans_meta.get("font_index") or 0)
        if not ovf and sd.get("font"):   # 기본 서식 폰트
            ovf = sd["font"]
            ofi = int(sd.get("font_index") or 0)
        if ovf and Path(ovf).exists():   # 말풍선별/기본 폰트 오버라이드
            fp, fi = ovf, ofi
        # 손글씨 폰트는 원본 붓글씨보다 가늘어 스트로크 1px 보강
        stroke = SS if b.kind == "hand" else 0
        drawn = 0   # 렌더 함수들은 사용된 폰트 크기(truthy)를 반환
        if lay0:   # 수동 레이아웃 — 줄바꿈·크기·위치를 사용자가 직접 제어
            drawn = render_layout(tile, bs, fp, fi, lay0,
                                  off=(x0 * SS, y0 * SS), stroke=stroke,
                                  scale=SS)
        if not drawn:
            drawn = render_line_matched(d, bs, fp, fi,
                                        off=(x0 * SS, y0 * SS),
                                        stroke=stroke)
        if not drawn and "\n" in (bs.text or "") \
                and str(b.trans_meta.get("passes") or "") \
                .startswith("manual"):
            # 사용자가 직접 입력한 텍스트 — 원본 줄 수와 달라도 입력한
            # 줄바꿈 그대로 재조판 (자동 크기 수동 레이아웃)
            drawn = render_layout(tile, bs, fp, fi, {},
                                  off=(x0 * SS, y0 * SS), stroke=stroke,
                                  scale=SS)
        if not drawn:
            bs.text = (bs.text or "").replace("\n", " ")
            b.text = bs.text
            drawn = render_text(d, bs, fp, fi, off=(x0 * SS, y0 * SS))
        if drawn:   # 실사용 폰트 크기 기록 — 검수 페이지 크기 입력 프리필
            b.trans_meta["used_size"] = max(1, round(int(drawn) / SS))
        tile = tile.resize((x1 - x0, y1 - y0), Image.LANCZOS)
        arr = cv2.cvtColor(np.array(tile), cv2.COLOR_RGBA2BGRA)
        # psd-tools 레거시 레이어명은 한글 인코딩 불가 → ASCII만 사용
        text_tiles.append((f"Text{idx:02d}", arr, x0, y0, b))

    # 프리뷰 합성 + 위치 QC (글자 잉크가 어두운 배경 위에 얹힌 비율)
    # 합성 베이스 3단계:
    #  preserve_bg(기본) — 원본 100% 보존. 재조판 말풍선의 지움 덮개(원본
    #    종이색)와 텍스트만 얹음. 근백색 블렌딩도 종이·스크린톤 하이라이트를
    #    건드려 열화가 보이므로 배경엔 아무 보정도 하지 않는다.
    #  restored_base — 구 동작: v2 전면 보정 베이스.
    #  둘 다 아니면 — 근백색 영역(wmask)만 v2 블렌딩 (중간 단계).
    if getattr(args, "restored_base", False):
        base = restored.copy()
    elif preserve:
        base = img.copy()
    else:
        mf = (wmask.astype(np.float32) / 255.0)[..., None]  # 이미 블러됨
        base = (img.astype(np.float32) * (1.0 - mf)
                + restored.astype(np.float32) * mf).astype(np.uint8)
    a = clear_layer[..., 3:4].astype(np.float32) / 255
    base = (base * (1 - a) + clear_layer[..., :3] * a).astype(np.uint8)
    base_gray = cv2.cvtColor(base, cv2.COLOR_BGR2GRAY)
    preview = base.copy()
    pos_warn = 0
    for name, arr, x0, y0, b in text_tiles:
        h_t, w_t = arr.shape[:2]
        ink = arr[..., 3] > 96          # QC용 실질 잉크 픽셀
        under = base_gray[y0:y0 + h_t, x0:x0 + w_t]
        n_ink = max(int(ink.sum()), 1)
        b.pos_overlap = round(float(((under < 170) & ink).sum()) / n_ink, 3)
        if b.pos_overlap > 0.03:
            pos_warn += 1
        # 알파 블렌딩 합성 — 이진 복사는 안티앨리어싱을 깨서 글자가 거칠어짐
        region = preview[y0:y0 + h_t, x0:x0 + w_t]
        a3 = arr[..., 3:4].astype(np.float32) / 255.0
        region[:] = (region.astype(np.float32) * (1 - a3)
                     + arr[..., :3].astype(np.float32) * a3).astype(np.uint8)
    if pos_warn:
        print(f"    !! 위치 확인 필요 {pos_warn}건 (review.json의 "
              f"pos_overlap > 0.03)", flush=True)
    imwrite_unicode(out_dir / f"{stem}_final.png", preview)

    # 렌더 캐시 (앱의 실시간 폰트 교체용): 텍스트 제외 합성본 + 블록 데이터
    if getattr(args, "render_cache", False):
        cache_dir = out_dir / "_cache"
        imwrite_unicode(cache_dir / f"{stem}_base.png", base)
        (cache_dir / f"{stem}.json").write_text(json.dumps({
            "bubbles": [{"bbox": list(b.bbox),
                         "line_boxes": [list(x) for x in b.line_boxes],
                         "text": b.text, "kind": b.kind,
                         "font_cap": b.font_cap}
                        for b in to_retype]},
            ensure_ascii=False), encoding="utf-8")

    if not args.no_psd:
        layers = [("Background", img, 0, 0)]
        if not preserve:   # 원본 보존 모드에선 보정본 레이어 제외
            layers.append(("Restored", restored, 0, 0))
        layers.append(("BubbleClear", clear_layer, 0, 0))
        layers += [(nm, a2, tx, ty) for nm, a2, tx, ty, _ in text_tiles]
        save_psd(out_dir / f"{stem}.psd", layers)

    if args.debug:
        dbg = restored.copy()
        for b in bubbles:
            x, y, w, h = b.bbox
            if b.retyped and b.pos_overlap > 0.03:
                col = (255, 0, 255)          # 마젠타 = 위치 확인 필요
            elif b.retyped:
                col = (0, 180, 0)
            elif b.text:
                col = (0, 140, 255)
            else:
                col = (0, 0, 255)
            cv2.rectangle(dbg, (x, y), (x + w, y + h), col, 3)
            cv2.putText(dbg, b.confidence or "?", (x, y - 6),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.9, col, 2)
        imwrite_unicode(out_dir / "_debug" / f"{stem}_detect.png", dbg)

    return {"file": page.name, "status": "ok",
            "bubbles": len(bubbles), "retyped": len(to_retype),
            "pos_warnings": pos_warn,
            "review": [{"id": i + 1, "bbox": list(b.bbox), "text": b.text,
                        "kind": b.kind, "confidence": b.confidence,
                        "retyped": b.retyped, "pos_overlap": b.pos_overlap,
                        **b.trans_meta}
                       for i, b in enumerate(bubbles)]}


# ---------------------------------------------------------------------------
# 폰트 프리셋 — 검수 페이지 폰트 목록·앱 GUI 공용 (앱이 여기서 import)
# ---------------------------------------------------------------------------
# (표시명, 파일 패턴들, ttc 인덱스)
FONT_PRESETS = [
    ("KoPub 바탕 Bold — 출판 명조 (추천)",
     ["KoPubWorld*Batang*Bold*.ttf", "KoPub*Batang*Bold*.ttf",
      "KoPubWorld*Batang*Bold*.otf", "KoPub*Batang*Bold*.otf"], 0),
    ("나눔명조 Bold — 인쇄체 대사 (추천)",
     ["NanumMyeongjo*Bold*.ttf", "NanumMyeongjoB*.ttf"], 0),
    ("나눔명조 — 가는 인쇄체", ["NanumMyeongjo.ttf"], 0),
    ("함초롬바탕 — 부드러운 명조",
     ["HANBatang*.ttf", "함초롬바탕*.ttf"], 0),
    ("나눔고딕 ExtraBold — 굵은 고딕 대사",
     ["NanumGothicExtraBold*.ttf"], 0),
    ("나눔고딕 Bold — 고딕 대사", ["NanumGothicBold*.ttf"], 0),
    ("나눔바른고딕 Bold", ["NanumBarunGothicBold*.ttf"], 0),
    ("맑은 고딕 Bold", ["malgunbd.ttf"], 0),
    ("검은고딕 — 아주 굵은 외침·강조",
     ["BlackHanSans*.ttf", "검은고딕*.ttf"], 0),
    ("배민 도현체 — 각지고 힘 있는 강조",
     ["BMDOHYEON*.ttf", "BMDoHyeon*.ttf", "BMDOHYEON*.otf"], 0),
    ("잘난체 — 둥글고 코믹한 외침",
     ["Jalnan*.ttf", "Jalnan*.otf", "잘난체*.ttf"], 0),
    ("배민 을지로체 — 거친 붓 느낌·효과음",
     ["BMEULJIRO*.ttf", "BMEuljiro*.ttf", "BMEULJIRO*.otf"], 0),
    ("바탕체 — 옛날 인쇄 만화 느낌", ["batang.ttc"], 1),
    ("궁서체 — 사극·비장한 대사", ["batang.ttc"], 2),
    ("돋움체 — 구형 식자 느낌", ["gulim.ttc"], 3),
]
HAND_PRESETS = [
    ("나눔손글씨 예쁜 민경체 — 통통한 마커 손글씨 (추천)",
     ["나눔손글씨 예쁜 민경체.ttf", "*예쁜*민경*.ttf",
      "NanumYeBbeunMinGyeong*.ttf", "Mingyung*.ttf"], 0),
    ("나눔손글씨 하나손글씨 — 둥글둥글 손글씨",
     ["나눔손글씨 하나손글씨.ttf", "*하나손글씨*.ttf",
      "NanumHaNaSonGeurSsi*.ttf", "Hana_handwriting*.ttf"], 0),
    ("나눔손글씨 붓 — 붓글씨 (추천)", ["NanumBrush*.ttf"], 0),
    ("나눔손글씨 펜 — 펜글씨", ["NanumPen*.ttf"], 0),
    ("나눔바른펜", ["NanumBarunpen*.ttf"], 0),
    ("나눔손글씨 시리즈 — 설치된 것 중 첫 번째",
     ["나눔손글씨*.ttf", "NanumSonGeulSsi*.ttf"], 0),
    ("배민 을지로체 — 거친 손글씨 느낌",
     ["BMEULJIRO*.ttf", "BMEuljiro*.ttf"], 0),
]


def _font_dirs() -> list[Path]:
    # 앱 폴더의 fonts/ 하위를 최우선 탐색 — OS 설치 없이도 폰트 사용 가능
    dirs = [Path(__file__).resolve().parent / "fonts"]
    la = os.environ.get("LOCALAPPDATA", "")
    if la:
        dirs.append(Path(la) / "Microsoft" / "Windows" / "Fonts")
    dirs.append(Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts")
    return dirs


def resolve_presets(presets) -> list[tuple[str, str, int]]:
    """설치된 프리셋만 (표시명, 경로, 인덱스)로 반환."""
    found = []
    for label, patterns, idx in presets:
        for d in _font_dirs():
            hit = None
            for pat in patterns:
                hits = sorted(d.glob(pat)) if d.exists() else []
                if hits:
                    hit = hits[0]
                    break
            if hit:
                found.append((label, str(hit), idx))
                break
    return found


# ---------------------------------------------------------------------------
# 검수 페이지 (review.html) + 재검수 적용 (rework.json)
# ---------------------------------------------------------------------------
def crop_bubble_hires(img_bgr: np.ndarray, b: Bubble,
                      pad: int = 12) -> np.ndarray:
    """재검수용 고정밀 크롭 — 축소 없이, 작은 글자는 2배 확대."""
    x, y, w, h = b.bbox
    H, W = img_bgr.shape[:2]
    c = img_bgr[max(0, y - pad):min(H, y + h + pad),
                max(0, x - pad):min(W, x + w + pad)]
    long_side = max(c.shape[:2])
    if long_side < 700:
        s = min(2.0, 1400 / max(long_side, 1))
        c = cv2.resize(c, (int(c.shape[1] * s), int(c.shape[0] * s)),
                       interpolation=cv2.INTER_CUBIC)
    elif long_side > 1568:
        s = 1568 / long_side
        c = cv2.resize(c, (int(c.shape[1] * s), int(c.shape[0] * s)),
                       interpolation=cv2.INTER_AREA)
    return c


def make_manual_bubble(restored_gray: np.ndarray, bbox) -> Bubble:
    """사용자 지정 영역(감지 누락 보완)을 합성 Bubble로.

    지움 마스크는 박스 내부의 어두운 픽셀만 9px 팽창 — 박스 밖(테두리 등)은
    건드리지 않는다. 글줄 밴드·폰트 상한은 감지기와 같은 방식으로 산출."""
    H, W = restored_gray.shape[:2]
    x, y, w, h = [int(v) for v in bbox]
    x = max(0, min(x, W - 4))
    y = max(0, min(y, H - 4))
    w = max(4, min(w, W - x))
    h = max(4, min(h, H - y))
    roi = restored_gray[y:y + h, x:x + w]
    dark = (roi < 100).astype(np.uint8) * 255
    mask = np.zeros((H, W), np.uint8)
    mask[y:y + h, x:x + w] = cv2.dilate(dark, np.ones((9, 9), np.uint8))

    # 글줄 밴드 (행 투영)
    line_boxes: list = []
    rows = (dark > 0).sum(axis=1)
    y0 = None
    gap = 0
    for r, v in enumerate(list(rows) + [0] * 4):
        if v > 0:
            if y0 is None:
                y0 = r
            gap = 0
        elif y0 is not None:
            gap += 1
            if gap > 3:
                band = dark[y0:r - gap + 1, :]
                bx, by, bw, bh = cv2.boundingRect(band)
                if bh >= 5 and bw >= 5:
                    line_boxes.append((x + bx, y + y0 + by, bw, bh))
                y0, gap = None, 0

    n, _, st, _ = cv2.connectedComponentsWithStats(
        (dark > 0).astype(np.uint8), connectivity=8)
    hs = sorted(st[i, cv2.CC_STAT_HEIGHT] for i in range(1, n)
                if st[i, cv2.CC_STAT_AREA] >= 8)
    med_h = hs[len(hs) // 2] if hs else max(10, h // 3)
    return Bubble(bbox=(x, y, w, h), mask=mask,
                  font_cap=int(med_h * 1.35), line_boxes=line_boxes)


_REVIEW_HTML = r"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<title>재조판 검수</title>
<style>
 body{font-family:'Malgun Gothic',sans-serif;margin:0;background:#222;color:#eee}
 header{position:fixed;left:0;top:0;bottom:0;width:200px;background:#333;
        padding:14px 12px;z-index:10;display:flex;flex-direction:column;
        gap:10px;align-items:stretch;overflow-y:auto;box-sizing:border-box}
 header>button{width:100%}
 button{padding:6px 14px;border:0;border-radius:4px;background:#0b5ed7;
        color:#fff;cursor:pointer;font-size:14px}
 #pages{margin-left:200px}
 .legend{display:flex;flex-direction:column;gap:4px;border-top:1px solid #555;
         padding-top:10px}
 .legend span{font-size:12px}
 .zoomrow{display:flex;gap:6px;align-items:center}
 .zoomrow button{flex:1;width:auto;background:#555;padding:6px 0}
 .symrow{display:flex;gap:5px;flex-wrap:wrap}
 .symrow button{flex:0 0 auto;min-width:34px;background:#555;
                padding:5px 8px;font-size:14px}
 #zl{min-width:44px;text-align:center;font-size:13px}
 .dot{display:inline-block;width:10px;height:10px;border-radius:2px;
      margin-right:3px;vertical-align:middle}
 .page{margin:24px auto;max-width:1200px;padding:0 12px}
 .canvas{position:relative}
 .canvas img{width:100%;display:block}
 .bx{position:absolute;border:2px solid;cursor:pointer;box-sizing:border-box;
     opacity:var(--bxo,.4)}
 .zoomrow input[type=range]{flex:1;accent-color:#0b5ed7;min-width:0}
 .bx.ok{border-color:#2eae4f}
 .bx.warn{border-color:#ff9d2e}
 .bx.skip{border-color:#888;border-style:dashed}
 .bx.marked{border-color:#ff2e5f;border-width:3px;
            background:rgba(255,46,95,.15)}
 body.hidebx .bx{display:none}
 body.hidebx .paintcv,body.hidebx .rg{display:none}
 .bx.manual{border-color:#7a5cff}
 body.draw .canvas{cursor:crosshair}
 body.draw .bx{pointer-events:none}
 .lockbtn{margin-left:10px;background:#555;font-size:12px;padding:3px 10px}
 .pagefoot{display:flex;gap:2px;margin:8px 0 0;justify-content:flex-end}
 .page.locked .canvas{outline:3px solid #d4a017}
 .page.locked .bx{cursor:not-allowed}
 .page.locked h2::after{content:' — 수동 확정됨 (재조판·전체 실행에서 보호)';
                        color:#d4a017;font-weight:normal}
 .dock{position:sticky;bottom:6px;left:212px;z-index:8;
       max-width:calc(100vw - 248px);
       background:rgba(10,10,10,.62);backdrop-filter:blur(3px);
       border-radius:10px;padding:8px 10px;
       box-shadow:0 6px 24px rgba(0,0,0,.6)}
 .items{background:#242424;border-radius:6px;padding:8px 12px;
        max-height:38vh;overflow-y:auto}
 .dock .pagefoot{margin-top:6px}
 /* 편집 항목이 없을 땐 버튼 크기에 맞는 우측 정렬 필로 축소 */
 .dock:not(:has(.items[style*="block"])){width:fit-content;
                                         margin-left:auto}
 .item{display:flex;gap:10px;margin:8px 0;align-items:flex-start}
 .item textarea{flex:1;background:#1b1b1b;color:#eee;border:1px solid #555;
                border-radius:4px;padding:6px;font-size:14px;min-height:78px;
                max-width:720px;max-height:26vh;resize:vertical}
 .item select{background:#1b1b1b;color:#eee;border:1px solid #555;
              border-radius:4px;padding:4px;max-width:200px;margin-top:6px}
 .layrow{display:flex;gap:6px;align-items:center;font-size:12px;color:#aaa;
         flex-wrap:wrap;margin:2px 0 6px}
 .layrow input{width:58px;background:#1b1b1b;color:#eee;
               border:1px solid #555;border-radius:4px;padding:4px}
 .layrow select{background:#1b1b1b;color:#eee;border:1px solid #555;
                border-radius:4px;padding:4px}
 .rcol{display:flex;flex-direction:column;gap:6px;min-width:220px}
 .rcol select{margin-top:0;max-width:none}
 .ccol{display:flex;flex-direction:column;gap:8px;font-size:12px;
       color:#ccc;white-space:nowrap;padding-top:4px}
 .hint{margin-left:auto;color:#aaa;font-size:12px}
 .tf{position:absolute;border:1.5px dashed #19c3e6;cursor:move;
     box-sizing:border-box;background:rgba(25,195,230,.07);z-index:5}
 .tfh{position:absolute;right:-7px;bottom:-7px;width:13px;height:13px;
      background:#19c3e6;cursor:nwse-resize;border-radius:3px}
 .tfe{position:absolute;right:-7px;top:50%;margin-top:-7px;width:13px;
      height:13px;background:#19c3e6;cursor:ew-resize;border-radius:3px}
 .tfs{position:absolute;bottom:-7px;left:50%;margin-left:-7px;width:13px;
      height:13px;background:#19c3e6;cursor:ns-resize;border-radius:3px}
 #busy{position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:200;
       display:none;align-items:center;justify-content:center;
       flex-direction:column;gap:14px}
 #busy.on{display:flex}
 .spin{width:46px;height:46px;border:5px solid #555;
       border-top-color:#19c3e6;border-radius:50%;
       animation:sp 1s linear infinite}
 @keyframes sp{to{transform:rotate(360deg)}}
 #busytext{color:#eee;font-size:15px;text-shadow:0 1px 4px #000}
 #ctx{position:fixed;z-index:99;background:#333;border:1px solid #555;
      border-radius:6px;padding:4px;display:none;min-width:200px;
      box-shadow:0 4px 14px rgba(0,0,0,.5)}
 .ctxi{padding:7px 12px;font-size:13px;cursor:pointer;border-radius:4px}
 .ctxi:hover{background:#0b5ed7}
 .rg{position:absolute;border:2px dashed #ff2ea6;box-sizing:border-box;
     background:rgba(255,46,166,.08);cursor:move;z-index:6}
 .rgh{position:absolute;right:-7px;bottom:-7px;width:13px;height:13px;
      background:#ff2ea6;cursor:nwse-resize;border-radius:3px}
 .bx.clean{background:rgba(255,255,255,.35)}
 .paintcv{position:absolute;left:0;top:0;width:100%;height:100%;
          opacity:var(--bxo,.4);pointer-events:none;z-index:3}
 body.brush .canvas{cursor:crosshair}
 body.brush .bx,body.brush .tf,body.brush .rg{pointer-events:none}
 .zr{position:absolute;border:2px dashed #ffd94a;box-sizing:border-box;
     background:rgba(255,217,74,.12);z-index:7}
 body.zsel .canvas{cursor:zoom-in}
 body.zsel .bx,body.zsel .tf,body.zsel .rg{pointer-events:none}
 body.pan, body.pan *{cursor:grab !important}
 body.pan.panning, body.pan.panning *{cursor:grabbing !important}
 body.hidebx .tf{display:none}
 body.draw .tf{pointer-events:none}
 .tag{font-size:12px;color:#aaa;white-space:nowrap;padding-top:8px}
 .tagbtn{cursor:pointer;color:#8fc7ff}
 .tagbtn:hover{text-decoration:underline}
 .bx.flash{box-shadow:0 0 0 5px rgba(255,226,90,.95);
           opacity:1 !important}
 h2{font-size:16px;color:#ccc}
 #count{color:#ff9d2e}
</style></head><body>
<header>
 <button onclick="save()">rework.json 저장</button>
 <button id="tgl" onclick="toggleBoxes()"
         style="background:#555">박스 숨기기 (H)</button>
 <button id="ov" onclick="toggleOrig()"
         style="background:#555">원본 보기 (O)</button>
 <button id="dm" onclick="toggleDraw()"
         style="background:#555">✏ 영역 추가 (D)</button>
 <button id="br" onclick="toggleBrush()"
         style="background:#555">🧹 브러시 칠하기 (B)</button>
 <div class="zoomrow" id="brrow" style="display:none">
  <button id="brm" onclick="toggleBrushMode()">칠하기</button>
  <input type="range" id="brs" min="6" max="120" value="30"
         title="브러시 크기">
 </div>
 <button id="zs" onclick="toggleZoomSel()"
         style="background:#555">🔍 영역 확대 (Z)</button>
 <button id="ud" onclick="doUndo()"
         style="background:#555">↶ 실행취소 (Ctrl+Z)</button>
 <span style="font-size:12px;color:#aaa">특수문자 (커서 위치에 삽입)</span>
 <div class="symrow" id="symrow"></div>
 <div class="zoomrow">
  <button onclick="zoomAt(zoom - 0.15)" title="축소 (-)">−</button>
  <span id="zl">100%</span>
  <button onclick="zoomAt(zoom + 0.15)" title="확대 (+)">＋</button>
  <button onclick="zoomAt(1)" title="원래 크기 (0)">⟲</button>
  <button onclick="fitPage()" title="페이지 맞추기 (F)">⤢</button>
 </div>
 <div class="zoomrow" title="말풍선 박스 아웃라인 투명도">
  <span style="font-size:12px;color:#aaa">박스</span>
  <input type="range" id="bxo" min="10" max="100" value="40"
         oninput="setBoxOpacity(this.value)">
  <span id="bxol" style="font-size:12px;min-width:36px;text-align:right">
   40%</span>
 </div>
 <span>마킹 <b id="count">0</b>개</span>
 <span class="legend">
  <span><i class="dot" style="background:#2eae4f"></i>재조판됨</span>
  <span><i class="dot" style="background:#ff9d2e"></i>확신도 낮음·미처리</span>
  <span><i class="dot" style="background:#888"></i>보존(효과음·손글씨 등)</span>
  <span><i class="dot" style="background:#ff2e5f"></i>재작업 마킹</span>
 </span>
 <span style="font-size:12px;color:#aaa">
  박스 클릭=마킹/해제 · 텍스트를 직접 고치면 그대로 재조판(비용 0),
  안 고치면 AI 고정밀 재전사</span>
</header>
<div id="pages"></div>
<script>
const DATA = __DATA__;
const FONTS = __FONTS__;
const DEF_STYLE = __DEF__;
const SERVER = location.protocol === 'http:'
            || location.protocol === 'https:';
const marked = {};
let lastMark = null;   // 방향키 넛지 대상 — 마지막 마킹/조작 말풍선
// 직전 적용 폰트 — 다음 마킹/신규 영역에 자동으로 이어서 적용
let lastFont = null;
try { lastFont = JSON.parse(localStorage.getItem('rvLastFont') || 'null'); }
catch (e) { lastFont = null; }
function setLastFont(f){
  lastFont = f || null;
  try { localStorage.setItem('rvLastFont', JSON.stringify(lastFont)); }
  catch (e) {}   // 영구 저장 — 재시작 후에도 직전 폰트 유지
}
function normLay(o){
  if (!o) return null;
  const n = {};
  const sz = parseFloat(o.size); if (sz > 0) n.size = sz;
  const dx = parseFloat(o.dx) || 0; if (dx) n.dx = dx;
  const dy = parseFloat(o.dy) || 0; if (dy) n.dy = dy;
  const sp = parseFloat(o.spacing); if (sp > 0) n.spacing = sp;
  const tr = parseFloat(o.track); if (tr > 0 && tr !== 1) n.track = tr;
  const ws = parseFloat(o.wscale); if (ws > 0 && ws !== 1) n.wscale = ws;
  const dt = parseFloat(o.dottrack);
  if (dt > 0 && dt !== 0.45) n.dottrack = dt;
  if (o.align && o.align !== 'left') n.align = o.align;
  if (o.fill) n.fill = true;
  return Object.keys(n).length ? n : null;
}
function fontSelect(cur){
  const sel = document.createElement('select');
  const d = document.createElement('option');
  d.value = ''; d.textContent = '폰트: 기본 설정';
  sel.appendChild(d);
  FONTS.forEach((f, i) => {
    const o = document.createElement('option');
    o.value = String(i); o.textContent = f.label;
    if (cur && f.path === cur.path && f.index === (cur.index || 0))
      o.selected = true;
    sel.appendChild(o);
  });
  // 사용자가 고른 폰트를 기억 — '기본 설정' 선택 시 자동 유지 해제
  sel.addEventListener('change', () => {
    setLastFont(sel.value === '' ? null : FONTS[+sel.value]);
  });
  return sel;
}
function key(p, id){ return p + "|" + id; }
function toggleBoxes(){
  const on = document.body.classList.toggle('hidebx');
  document.getElementById('tgl').textContent =
    on ? '박스 보이기 (H)' : '박스 숨기기 (H)';
}
let showOrig = false;
function toggleOrig(){
  showOrig = !showOrig;
  DATA.forEach(pg => {
    if (pg.orig && pg._im) pg._im.src = showOrig ? pg.orig : pg.img;
  });
  document.getElementById('ov').textContent =
    showOrig ? '결과 보기 (O)' : '원본 보기 (O)';
  document.getElementById('ov').style.background =
    showOrig ? '#0b5ed7' : '#555';
}
let zoom = 1;
try { zoom = +(localStorage.getItem('rvZoom') || 1) || 1; } catch (e) {}
function setZoom(z){
  zoom = Math.min(6, Math.max(0.3, z));   // 영역 확대용 상한 6배
  document.querySelectorAll('.page').forEach(p => {
    // max-width만으론 부모(창) 폭을 넘지 못함 — 고정 width로 지정해야
    // 창 크기 이상 확대(가로 스크롤) 가능. 100% 이하는 창에 맞춰 축소.
    p.style.width = Math.round(1200 * zoom) + 'px';
    p.style.maxWidth = zoom <= 1 ? '100%' : 'none';
  });
  document.getElementById('zl').textContent = Math.round(zoom * 100) + '%';
  try { localStorage.setItem('rvZoom', String(zoom)); }   // 영구 저장
  catch (e) {}
}
function zoomAt(z, ax, ay){
  // 앵커 기준 줌 — 선택(마킹) 말풍선 중심 > 지정 좌표 > 화면 중앙 순
  let cv = null;
  if (ax === undefined) {
    if (lastMark && lastMark.tf && lastMark.tf.isConnected) {
      const rr = lastMark.tf.getBoundingClientRect();
      ax = rr.left + rr.width / 2;
      ay = rr.top + rr.height / 2;
      cv = lastMark.tf.parentElement;
    } else {
      ax = (window.innerWidth + 200) / 2;   // 사이드바 제외 중앙
      ay = window.innerHeight / 2;
    }
  }
  if (!cv) {
    const pg = pageInView();
    cv = pg && pg._sec ? pg._sec.querySelector('.canvas') : null;
  }
  if (!cv) { setZoom(z); return; }
  const r1 = cv.getBoundingClientRect();
  const fx = (ax - r1.left) / r1.width;
  const fy = (ay - r1.top) / r1.height;
  setZoom(z);
  const r2 = cv.getBoundingClientRect();   // 배율 반영 후 재측정
  window.scrollBy(r2.left + fx * r2.width - ax,
                  r2.top + fy * r2.height - ay);
}
document.addEventListener('wheel', ev => {
  if (!ev.ctrlKey) return;          // Ctrl+휠 = 마우스 위치 축 확대/축소
  ev.preventDefault();
  zoomAt(zoom * (ev.deltaY < 0 ? 1.1 : 0.9), ev.clientX, ev.clientY);
}, {passive: false});
function fitPage(){
  // 현재 페이지가 화면 높이에 맞도록 배율 조정 후 페이지 상단으로
  const pg = pageInView();
  if (!pg) return;
  const availW = window.innerWidth - 224;   // 사이드바+여백 제외
  const targetH = window.innerHeight - 90;  // 제목·버튼 여유
  let w = targetH * pg.w / pg.h;
  w = Math.max(120, Math.min(w, availW));
  setZoom(w / 1200);
  if (pg._sec) pg._sec.scrollIntoView({block: 'start'});
}
function setBoxOpacity(v){
  v = Math.min(100, Math.max(10, +v || 40));
  document.body.style.setProperty('--bxo', String(v / 100));
  document.getElementById('bxol').textContent = v + '%';
  const s = document.getElementById('bxo');
  if (s && +s.value !== v) s.value = v;
  try { localStorage.setItem('rvBxo', String(v)); }   // 영구 저장
  catch (e) {}
}
// ---- 스페이스+드래그 팬 (포토샵 스타일) ----
let spaceHeld = false;
document.addEventListener('keydown', ev => {
  if (ev.code !== 'Space') return;
  if (['TEXTAREA', 'INPUT', 'SELECT', 'BUTTON']
      .includes(ev.target.tagName))
    return;                       // 입력 중엔 일반 스페이스
  ev.preventDefault();            // 스페이스 페이지 스크롤 방지
  if (!spaceHeld) document.body.classList.add('pan');
  spaceHeld = true;
});
document.addEventListener('keyup', ev => {
  if (ev.code !== 'Space') return;
  spaceHeld = false;
  document.body.classList.remove('pan', 'panning');
});
document.addEventListener('mousedown', ev => {
  if (!spaceHeld) return;
  ev.preventDefault();
  ev.stopPropagation();           // 브러시/그리기/마킹보다 우선
  document.body.classList.add('panning');
  const st = {x: ev.clientX, y: ev.clientY,
              sx: window.scrollX, sy: window.scrollY};
  const mv = e2 => window.scrollTo(st.sx - (e2.clientX - st.x),
                                   st.sy - (e2.clientY - st.y));
  const up = () => {
    document.body.classList.remove('panning');
    document.removeEventListener('mousemove', mv);
    document.removeEventListener('mouseup', up);
  };
  document.addEventListener('mousemove', mv);
  document.addEventListener('mouseup', up);
}, true);   // 캡처 단계 — 다른 캔버스 핸들러 차단
let drawMode = false;
function toggleDraw(){
  drawMode = !drawMode;
  if (drawMode && brushOn) toggleBrush();
  document.body.classList.toggle('draw', drawMode);
  const b = document.getElementById('dm');
  b.textContent = drawMode ? '✏ 그리기 종료 (D)' : '✏ 영역 추가 (D)';
  b.style.background = drawMode ? '#0b5ed7' : '#555';
}
// ---- 실행취소 (브러시 획·트랜스폼·영역 조작) ----
const undoStack = [];
function pushUndo(fn){
  undoStack.push(fn);
  if (undoStack.length > 15) undoStack.shift();   // 메모리 상한
  updUndo();
}
function updUndo(){
  const b = document.getElementById('ud');
  b.disabled = !undoStack.length;
  b.style.opacity = undoStack.length ? '1' : '.45';
}
function doUndo(){
  const fn = undoStack.pop();
  if (fn) fn();
  updUndo();
}
// ---- 특수문자 프리셋 — 텍스트 입력칸 커서 위치에 삽입 ----
const SYMS = ['·', '··', '···', '…', '—', '~', '!?', '☆', '♪'];
let lastTa = null;
document.addEventListener('focusin', ev => {
  if (ev.target.tagName === 'TEXTAREA') lastTa = ev.target;
});
function insertSym(s){
  const ae = document.activeElement;
  const ta = (ae && ae.tagName === 'TEXTAREA') ? ae : lastTa;
  if (!ta || !ta.isConnected) {
    alert('먼저 텍스트 입력칸을 클릭한 뒤 사용하세요.');
    return;
  }
  const st = ta.selectionStart ?? ta.value.length;
  const en = ta.selectionEnd ?? st;
  ta.value = ta.value.slice(0, st) + s + ta.value.slice(en);
  ta.focus();
  ta.selectionStart = ta.selectionEnd = st + s.length;
  ta.dispatchEvent(new Event('input'));   // 트랜스폼 박스 등 동기화
}
{
  const sr = document.getElementById('symrow');
  SYMS.forEach(s => {
    const b = document.createElement('button');
    b.textContent = s;
    b.title = "'" + s + "' 삽입";
    b.addEventListener('mousedown', ev => {
      ev.preventDefault();   // 입력칸 포커스 유지
      insertSym(s);
    });
    sr.appendChild(b);
  });
}
// ---- 영역 확대 — 드래그한 부분을 화면에 꽉 차게 줌인 (1회용 모드) ----
let zoomSel = false;
function toggleZoomSel(){
  zoomSel = !zoomSel;
  if (zoomSel) {
    if (drawMode) toggleDraw();
    if (brushOn) toggleBrush();
  }
  document.body.classList.toggle('zsel', zoomSel);
  const b = document.getElementById('zs');
  b.textContent = zoomSel ? '🔍 확대할 부분 드래그… (Z)' : '🔍 영역 확대 (Z)';
  b.style.background = zoomSel ? '#0b5ed7' : '#555';
}
function startZoomRect(pg, cv, ev){
  ev.preventDefault();
  const r = cv.getBoundingClientRect();
  const el = document.createElement('div'); el.className = 'zr';
  cv.appendChild(el);
  const x0 = ev.clientX - r.left, y0 = ev.clientY - r.top;
  let cur = null;
  const mv = e2 => {
    const x1 = e2.clientX - r.left, y1 = e2.clientY - r.top;
    const l = Math.min(x0, x1), t = Math.min(y0, y1);
    const w = Math.abs(x1 - x0), h = Math.abs(y1 - y0);
    el.style.left = (l / r.width * 100) + '%';
    el.style.top = (t / r.height * 100) + '%';
    el.style.width = (w / r.width * 100) + '%';
    el.style.height = (h / r.height * 100) + '%';
    cur = [l, t, w, h];
  };
  const up = () => {
    document.removeEventListener('mousemove', mv);
    document.removeEventListener('mouseup', up);
    el.remove();
    toggleZoomSel();   // 1회 사용 후 자동 종료
    if (!cur || cur[2] < 12 || cur[3] < 12) return;
    const fx = cur[0] / r.width, fy = cur[1] / r.height;
    const fw = cur[2] / r.width, fh = cur[3] / r.height;
    const availW = window.innerWidth - 224;
    const availH = window.innerHeight - 80;
    // 선택 영역이 가로·세로 모두 화면에 들어가는 최대 배율
    const wByW = availW / fw;
    const wByH = availH * pg.w / (fh * pg.h);
    setZoom(Math.min(wByW, wByH) / 1200);
    const r2 = cv.getBoundingClientRect();   // 배율 반영 후 재측정
    window.scrollTo(
      window.scrollX + r2.left + fx * r2.width - 224,
      window.scrollY + r2.top + fy * r2.height - 40);
  };
  document.addEventListener('mousemove', mv);
  document.addEventListener('mouseup', up);
}
let brushOn = false, brushMode = 0;   // 0=칠 1=복원지우개 2=마크제거
function toggleBrush(){
  brushOn = !brushOn;
  if (brushOn && drawMode) toggleDraw();
  document.body.classList.toggle('brush', brushOn);
  const b = document.getElementById('br');
  b.textContent = brushOn ? '🧹 브러시 종료 (B)' : '🧹 브러시 칠하기 (B)';
  b.style.background = brushOn ? '#0b5ed7' : '#555';
  document.getElementById('brrow').style.display = brushOn ? 'flex' : 'none';
}
function toggleBrushMode(){
  brushMode = (brushMode + 1) % 3;
  const b = document.getElementById('brm');
  b.textContent = ['칠하기', '지우개(원본 복원)', '마크 지우기'][brushMode];
  b.style.background = ['#0b5ed7', '#a04040', '#555'][brushMode];
}
function ensurePaint(pg, cv){
  if (pg._pcv) return pg._pcv;
  const sc = Math.min(1, 1600 / Math.max(pg.w, pg.h));
  const c = document.createElement('canvas');
  c.className = 'paintcv';
  c.width = Math.max(2, Math.round(pg.w * sc));
  c.height = Math.max(2, Math.round(pg.h * sc));
  pg._psc = sc;
  cv.insertBefore(c, cv.children[1] || null);   // 이미지 바로 위
  pg._pcv = c;
  if (pg.paint) {
    const im2 = new Image();
    im2.onload = () => c.getContext('2d')
      .drawImage(im2, 0, 0, c.width, c.height);
    im2.src = pg.paint;
  }
  return c;
}
function startPaint(pg, cv, ev){
  if (pg._locked) return;
  ev.preventDefault();
  const c = ensurePaint(pg, cv);
  const g = c.getContext('2d');
  const snap = g.getImageData(0, 0, c.width, c.height);   // 획 단위 언두
  pushUndo(() => {
    c.getContext('2d').putImageData(snap, 0, 0);
    pg._paintDirty = true;
  });
  const r = cv.getBoundingClientRect();
  const size = +document.getElementById('brs').value;
  g.lineCap = g.lineJoin = 'round';
  g.lineWidth = Math.max(2, size * pg._psc);
  // 획 = 항상 기존 마크 제거 후 모드 색을 얹음
  //  칠하기(청록)=종이색 칠 / 지우개(빨강)=원본 복원 / 마크제거=제거만
  const seg = (a, b2) => {
    g.globalCompositeOperation = 'destination-out';
    g.strokeStyle = 'rgba(0,0,0,1)';
    g.beginPath(); g.moveTo(a[0], a[1]); g.lineTo(b2[0], b2[1]); g.stroke();
    if (brushMode < 2) {
      g.globalCompositeOperation = 'source-over';
      g.strokeStyle = brushMode === 1
        ? 'rgba(255,80,80,1)' : 'rgba(64,200,255,1)';
      g.beginPath(); g.moveTo(a[0], a[1]); g.lineTo(b2[0], b2[1]);
      g.stroke();
    }
  };
  const pt = e2 => [(e2.clientX - r.left) / r.width * c.width,
                    (e2.clientY - r.top) / r.height * c.height];
  let p0 = pt(ev);
  seg(p0, [p0[0] + 0.1, p0[1] + 0.1]);
  pg._paintDirty = true;
  const mv = e2 => {
    const p1 = pt(e2);
    seg(p0, p1);
    p0 = p1;
  };
  const up = () => {
    document.removeEventListener('mousemove', mv);
    document.removeEventListener('mouseup', up);
  };
  document.addEventListener('mousemove', mv);
  document.addEventListener('mouseup', up);
}
document.addEventListener('keydown', ev => {
  if (ev.isComposing || ev.keyCode === 229)   // 한글 IME 조합 중 무시
    return;
  if (['TEXTAREA', 'INPUT', 'SELECT', 'BUTTON']
      .includes(ev.target.tagName))
    return;
  if (ev.key.startsWith('Arrow') && lastMark && lastMark.lay) {
    // 방향키 넛지 — 마지막 마킹 말풍선 이동 (Shift=10px)
    ev.preventDefault();
    const st = ev.shiftKey ? 10 : 1;
    const nx = ev.key === 'ArrowLeft' ? -st
             : ev.key === 'ArrowRight' ? st : 0;
    const ny = ev.key === 'ArrowUp' ? -st
             : ev.key === 'ArrowDown' ? st : 0;
    lastMark.lay.dx.value = (parseFloat(lastMark.lay.dx.value) || 0) + nx;
    lastMark.lay.dy.value = (parseFloat(lastMark.lay.dy.value) || 0) + ny;
    lastMark.lay.dx.dispatchEvent(new Event('input'));
    return;
  }
  if (ev.key === 'h' || ev.key === 'H' || ev.key === 'ㅗ') toggleBoxes();
  if (ev.key === 'o' || ev.key === 'O' || ev.key === 'ㅐ') toggleOrig();
  if (ev.key === 'd' || ev.key === 'D' || ev.key === 'ㅇ') toggleDraw();
  if (ev.key === 'b' || ev.key === 'B' || ev.key === 'ㅠ') toggleBrush();
  if ((ev.ctrlKey || ev.metaKey)
      && (ev.key === 'z' || ev.key === 'Z' || ev.key === 'ㅋ')) {
    ev.preventDefault();
    doUndo();
    return;
  }
  if (ev.key === '+' || ev.key === '=') zoomAt(zoom + 0.15);
  if (ev.key === '-' || ev.key === '_') zoomAt(zoom - 0.15);
  if (ev.key === '0') zoomAt(1);
  if (ev.key === 'f' || ev.key === 'F' || ev.key === 'ㄹ') fitPage();
  if (!ev.ctrlKey && !ev.metaKey
      && (ev.key === 'z' || ev.key === 'Z' || ev.key === 'ㅋ'))
    toggleZoomSel();
  if (ev.key === 'Escape' && zoomSel) toggleZoomSel();
  if (ev.key === 'Enter' && SERVER) {
    const ae = document.activeElement;   // 편집 중이면 적용 금지 (2중 가드)
    if (ae && ['TEXTAREA', 'INPUT', 'SELECT'].includes(ae.tagName))
      return;
    ev.preventDefault();
    const pg = pageInView();   // 화면에 보이는 페이지에 적용
    if (pg && pg._aps.length && !pg._aps[0].disabled)
      applyPage(pg, pg._aps[0]);
  }
});
const root = document.getElementById('pages');
DATA.forEach(pg => {
  pg._locked = !!pg.locked;
  pg._boxes = [];
  const sec = document.createElement('div'); sec.className = 'page';
  pg._sec = sec;
  pg._lks = [];
  pg._rbs = [];
  pg._aps = [];
  pg._rebuild = false;
  pg._adds = [];
  const h = document.createElement('h2'); h.textContent = pg.file;
  h.appendChild(pageButtons(pg, sec));   // 상단 버튼
  sec.appendChild(h);
  const cv = document.createElement('div'); cv.className = 'canvas';
  const im = document.createElement('img'); im.src = pg.img;
  pg._im = im;
  cv.appendChild(im);
  if (pg.paint) ensurePaint(pg, cv);   // 저장된 브러시 마스크 표시
  // 수동 영역 드래그 지정 + 브러시
  let drag = null;
  cv.addEventListener('mousedown', ev => {
    if (zoomSel) { startZoomRect(pg, cv, ev); return; }
    if (brushOn) { startPaint(pg, cv, ev); return; }
    if (!drawMode || pg._locked) return;
    ev.preventDefault();
    const r = cv.getBoundingClientRect();
    drag = {x: ev.clientX - r.left, y: ev.clientY - r.top, r: r, cur: null,
            el: document.createElement('div')};
    drag.el.className = 'bx manual';
    cv.appendChild(drag.el);
  });
  cv.addEventListener('mousemove', ev => {
    if (!drag) return;
    const r = drag.r;
    const x2 = ev.clientX - r.left, y2 = ev.clientY - r.top;
    const l = Math.min(drag.x, x2), t = Math.min(drag.y, y2);
    const w = Math.abs(x2 - drag.x), hh = Math.abs(y2 - drag.y);
    drag.el.style.left = (l / r.width * 100) + '%';
    drag.el.style.top = (t / r.height * 100) + '%';
    drag.el.style.width = (w / r.width * 100) + '%';
    drag.el.style.height = (hh / r.height * 100) + '%';
    drag.cur = [l, t, w, hh];
  });
  cv.addEventListener('mouseup', () => {
    if (!drag) return;
    const c = drag.cur, r = drag.r;
    if (!c || c[2] < 8 || c[3] < 8) { drag.el.remove(); drag = null; return; }
    const bbox = [c[0] / r.width * pg.w, c[1] / r.height * pg.h,
                  c[2] / r.width * pg.w, c[3] / r.height * pg.h]
                 .map(Math.round);
    addManualRow(pg, sec, bbox, drag.el);
    drag = null;
  });
  pg.review.forEach(e => {
    const b = document.createElement('div');
    const cls = e.retyped ? 'ok'
      : ((e.kind === 'dialogue' || e.kind === 'hand') ? 'warn' : 'skip');
    b.className = 'bx ' + cls;
    const bb = e.bbox;
    b.style.left = (bb[0] / pg.w * 100) + '%';
    b.style.top = (bb[1] / pg.h * 100) + '%';
    b.style.width = (bb[2] / pg.w * 100) + '%';
    b.style.height = (bb[3] / pg.h * 100) + '%';
    b.title = '#' + e.id + ' ' + (e.kind || '') + ' ' + (e.confidence || '')
              + '\n' + (e.text || '');
    b.onclick = () => toggle(pg, e, b, sec);
    b.oncontextmenu = ev => bubbleMenu(ev, pg, e, b, sec);
    if (e.clean) b.classList.add('clean');
    e._box = b;   // 트랜스폼/영역 박스에서 메뉴 열 때 참조
    pg._boxes.push(b);
    cv.appendChild(b);
  });
  sec.appendChild(cv);
  // 편집창+버튼 도크 — 확대·스크롤 위치와 무관하게 화면 하단에 고정
  const dock = document.createElement('div'); dock.className = 'dock';
  const items = document.createElement('div');
  items.className = 'items'; items.style.display = 'none';
  dock.appendChild(items);
  const foot = document.createElement('div'); foot.className = 'pagefoot';
  foot.appendChild(pageButtons(pg, sec));
  dock.appendChild(foot);
  sec.appendChild(dock);
  root.appendChild(sec);
  setLockUI(pg, sec);
});
function pageButtons(pg, sec){
  const box = document.createElement('span');
  const lk = document.createElement('button'); lk.className = 'lockbtn';
  lk.onclick = () => toggleLock(pg, sec);
  box.appendChild(lk);
  pg._lks.push(lk);
  const rb = document.createElement('button'); rb.className = 'lockbtn';
  rb.textContent = '♻ 재합성';
  rb.onclick = () => toggleRebuild(pg);
  box.appendChild(rb);
  pg._rbs.push(rb);
  if (SERVER) {
    const ap = document.createElement('button'); ap.className = 'lockbtn';
    ap.textContent = '✔ 이 페이지 적용 (Enter)';
    ap.onclick = () => applyPage(pg, ap);
    box.appendChild(ap);
    pg._aps.push(ap);
    const rv = document.createElement('button'); rv.className = 'lockbtn';
    rv.textContent = '↩ 되돌리기';
    rv.title = '직전 [적용] 이전 상태로 복원 (다시 누르면 재원복)';
    if (!pg.has_prev) { rv.disabled = true; rv.style.opacity = '.45'; }
    rv.onclick = () => revertPage(pg, rv);
    box.appendChild(rv);
  }
  return box;
}
async function revertPage(pg, btn){
  if (!confirm(pg.file + '\n직전 적용 이전 상태로 되돌릴까요?')) return;
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = '되돌리는 중…';
  showBusy('직전 적용 이전으로 되돌리는 중');
  try {
    const res = await fetch('/api/rework', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify([{page: pg.file, action: 'revert'}])});
    if (!res.ok)
      throw new Error(res.status === 409
        ? '앱이 다른 작업을 실행 중입니다' : 'HTTP ' + res.status);
    sessionStorage.setItem('rvScroll', String(window.scrollY));
    location.reload();
  } catch (err) {
    hideBusy();
    alert('되돌리기 실패: ' + err.message);
    btn.disabled = false;
    btn.textContent = old;
  }
}
function pageInView(){
  // 화면 중앙에 걸친 페이지 (없으면 가장 가까운 페이지)
  const mid = window.innerHeight / 2;
  let best = null, dist = Infinity;
  DATA.forEach(pg => {
    if (!pg._sec) return;
    const r = pg._sec.getBoundingClientRect();
    if (r.top <= mid && r.bottom >= mid) { best = pg; dist = -1; return; }
    if (dist < 0) return;
    const d = Math.min(Math.abs(r.top - mid), Math.abs(r.bottom - mid));
    if (d < dist) { dist = d; best = pg; }
  });
  return best;
}
function setLockUI(pg, sec){
  pg._lks.forEach(lk => {
    lk.textContent = pg._locked ? '🔒 잠금 해제' : '🔓 수동 확정';
  });
  if (pg._locked) sec.classList.add('locked');
  else sec.classList.remove('locked');
}
function setRebuildUI(pg){
  pg._rbs.forEach(rb => {
    rb.textContent = pg._rebuild ? '♻ 재합성 예약됨' : '♻ 재합성';
    rb.style.background = pg._rebuild ? '#0b5ed7' : '#555';
  });
}
function toggleRebuild(pg){
  if (pg._locked) {
    alert('수동 확정된 페이지입니다. 잠금을 해제하면 재합성할 수 있습니다.');
    return;
  }
  pg._rebuild = !pg._rebuild;
  setRebuildUI(pg);
}
function toggleLock(pg, sec){
  pg._locked = !pg._locked;
  if (pg._locked) {
    if (pg._rebuild) {                    // 잠그면 재합성 예약도 해제
      pg._rebuild = false;
      setRebuildUI(pg);
    }
    // 잠그면 이 페이지의 마킹은 모두 해제
    const items = sec.querySelector('.items');
    for (const k in marked) {
      if (marked[k].page === pg.file) {
        if (marked[k].tf) marked[k].tf.remove();
        delete marked[k];
      }
    }
    pg._boxes.forEach(b => b.classList.remove('marked'));
    (pg._adds || []).forEach(a => a.el.remove());
    pg._adds = [];
    Object.values(pg._regions || {}).forEach(rg => rg.el.remove());
    pg._regions = {};
    pg._cleans = {};
    if (items) { items.innerHTML = ''; items.style.display = 'none'; }
    document.getElementById('count').textContent =
      Object.keys(marked).length;
  }
  setLockUI(pg, sec);
}
function addManualRow(pg, sec, bbox, boxEl){
  const items = sec.querySelector('.items');
  const a = {bbox: bbox, el: boxEl};
  const row = document.createElement('div'); row.className = 'item';
  const tag = document.createElement('span'); tag.className = 'tag';
  tag.textContent = '신규 영역';
  row.appendChild(tag);
  const ta = document.createElement('textarea');
  ta.placeholder = '아는 텍스트 입력(선택) — 비우면 AI가 전사';
  row.appendChild(ta);
  const fs = fontSelect(lastFont);   // 직전 적용 폰트 이어받기
  row.appendChild(fs);
  a.font = fs;
  const del = document.createElement('button'); del.className = 'lockbtn';
  del.textContent = '삭제';
  del.onclick = () => {
    pg._adds = pg._adds.filter(x => x !== a);
    boxEl.remove(); row.remove();
    items.style.display = items.children.length ? 'block' : 'none';
  };
  row.appendChild(del);
  a.ta = ta; a.row = row;
  pg._adds.push(a);
  items.appendChild(row);
  items.style.display = 'block';
  pushUndo(() => {
    if (pg._adds.includes(a)) del.onclick();   // 마지막 추가 영역 제거
  });
}
function toggle(pg, e, box, sec){
  if (drawMode) return;
  if (pg._locked) {
    alert('수동 확정된 페이지입니다. 잠금을 해제하면 마킹할 수 있습니다.');
    return;
  }
  const k = key(pg.file, e.id);
  const items = sec.querySelector('.items');
  if (marked[k]) {
    const m0 = marked[k];
    delete marked[k];
    box.classList.remove('marked');
    const row = items.querySelector('[data-k="' + CSS.escape(k) + '"]');
    if (row) row.remove();
    if (m0.lrow) m0.lrow.remove();
    if (m0.tf) m0.tf.remove();
    if (lastMark === m0) lastMark = null;
  } else {
    const m = {page: pg.file, id: e.id, orig: e.text || ''};
    box.classList.add('marked');
    const row = document.createElement('div');
    row.className = 'item'; row.dataset.k = k;
    const tag = document.createElement('span'); tag.className = 'tag tagbtn';
    tag.textContent = '#' + e.id + ' ' + (e.confidence || '');
    tag.title = '클릭: 말풍선 위치로 이동·선택 (방향키 넛지 대상)';
    tag.onclick = () => {
      lastMark = m;                          // 넛지·줌 앵커 대상으로
      box.scrollIntoView({block: 'center', inline: 'center',
                          behavior: 'smooth'});
      box.classList.add('flash');            // 위치 하이라이트
      setTimeout(() => box.classList.remove('flash'), 1500);
    };
    row.appendChild(tag);
    const ta = document.createElement('textarea'); ta.value = e.text || '';
    row.appendChild(ta);
    // 우측 컬럼 — 폰트 선택 + (아래에 리셋·안내)
    const rcol = document.createElement('div'); rcol.className = 'rcol';
    const fs = fontSelect(e.font   // 저장 폰트 우선, 없으면 직전 폰트 이어받기
      ? {path: e.font, index: e.font_index || 0} : lastFont);
    rcol.appendChild(fs);
    row.appendChild(rcol);
    // 체크박스 컬럼 — 원본 보존 / 영역 유지
    const ccol = document.createElement('div'); ccol.className = 'ccol';
    const kp = document.createElement('label');
    const cb = document.createElement('input'); cb.type = 'checkbox';
    kp.appendChild(cb);
    kp.appendChild(document.createTextNode(' 원본 보존(재조판 취소)'));
    ccol.appendChild(kp);
    row.appendChild(ccol);
    m.font = fs;
    m.origFont = e.font || '';
    m.origIdx = e.font_index || 0;
    m.keep = cb;
    m.ta = ta;
    marked[k] = m;
    items.appendChild(row);
    // 수동 레이아웃 행 — 크기/위치/줄간격 (비우면 자동)
    const lr = document.createElement('div'); lr.className = 'layrow';
    const lay = e.layout || {};
    const mkNum = (label, val, ph, mn, st) => {
      lr.appendChild(document.createTextNode(label));
      const inp = document.createElement('input');
      inp.type = 'number';
      inp.step = st || '1';               // 스피너 증감 단위
      inp.placeholder = ph;
      if (mn !== undefined) inp.min = mn;   // 음수 스피너 방지
      if (val !== undefined && val !== null && val !== 0)
        inp.value = val;
      lr.appendChild(inp);
      return inp;
    };
    m.lay = {
      size: mkNum('레이아웃 — 크기px', lay.size || e.used_size, '자동', 0),
      dx: mkNum('X이동', lay.dx, '0'),
      dy: mkNum('Y이동', lay.dy, '0'),
      sp: mkNum('줄간격', lay.spacing, '1.15', 0.5, '0.1'),
      tr: mkNum('자간', lay.track, '1', 0.3, '0.1'),
      ws: mkNum('장평', lay.wscale, '1', 0.2, '0.1'),
      dt: mkNum('점간격', lay.dottrack, '0.45', 0.1, '0.05'),
    };
    lr.appendChild(document.createTextNode('정렬'));
    const al = document.createElement('select');
    [['left', '왼쪽'], ['center', '가운데'], ['right', '오른쪽']]
      .forEach(p => {
        const o = document.createElement('option');
        o.value = p[0]; o.textContent = p[1];
        al.appendChild(o);
      });
    al.value = lay.align || 'left';
    lr.appendChild(al);
    m.lay.al = al;
    const fl = document.createElement('label');
    const fc = document.createElement('input'); fc.type = 'checkbox';
    fc.checked = !!lay.fill;
    fl.appendChild(fc);
    fl.appendChild(document.createTextNode(
      ' 영역 유지(폰트만 조절·줄간격 자동)'));
    ccol.appendChild(fl);   // 체크박스 컬럼(우측)으로
    const xm = document.createElement('button'); xm.className = 'lockbtn';
    xm.textContent = '✕ 마킹 해제';
    xm.style.marginLeft = '0';
    xm.onclick = () => toggle(pg, e, box, sec);   // 패널에서 바로 해제
    ccol.appendChild(xm);
    m.lay.fill = fc;
    const rs = document.createElement('button'); rs.className = 'lockbtn';
    rs.textContent = '⟲ 리셋';
    rs.title = '레이아웃 설정값을 모두 비우고 자동 배치로 복귀';
    rs.onclick = () => {
      const prev = {s: m.lay.size.value, x: m.lay.dx.value,
                    y: m.lay.dy.value, p: m.lay.sp.value,
                    t: m.lay.tr.value, w: m.lay.ws.value,
                    d: m.lay.dt.value, a: m.lay.al.value,
                    f: m.lay.fill.checked};
      pushUndo(() => {   // 리셋도 실행취소 가능
        m.lay.size.value = prev.s; m.lay.dx.value = prev.x;
        m.lay.dy.value = prev.y; m.lay.sp.value = prev.p;
        m.lay.tr.value = prev.t; m.lay.ws.value = prev.w;
        m.lay.dt.value = prev.d; m.lay.al.value = prev.a;
        m.lay.fill.checked = prev.f;
        m.lay.size.dispatchEvent(new Event('input'));
      });
      m.lay.size.value = ''; m.lay.dx.value = ''; m.lay.dy.value = '';
      m.lay.sp.value = ''; m.lay.tr.value = ''; m.lay.ws.value = '';
      m.lay.dt.value = '';
      m.lay.al.value = 'left';
      m.lay.fill.checked = false;
      m.lay.size.dispatchEvent(new Event('input'));   // 트랜스폼 갱신
    };
    // 리셋+안내는 폰트 선택 아래(우측 컬럼)로
    const rrow = document.createElement('div');
    rrow.className = 'layrow'; rrow.style.margin = '0';
    rrow.appendChild(rs);
    const nt = document.createElement('span');
    nt.textContent = '※ 값을 넣으면 텍스트 줄바꿈 그대로 수동 배치';
    rrow.appendChild(nt);
    rcol.appendChild(rrow);
    // 동작 안내 — 레이아웃 행 우측 끝
    const hint = document.createElement('span'); hint.className = 'hint';
    hint.textContent = '수정=수동확정 / 그대로=AI 재전사 / 폰트만=폰트 변경'
      + ' / 직전 폰트 자동 유지(기본 설정 선택 시 해제)';
    lr.appendChild(hint);
    // 기준 상태는 프리필된 입력값 기준 — 실사용 크기 표시만으로는
    // 변경으로 간주하지 않음 (조정했을 때만 액션 발생)
    m.origLay = JSON.stringify(normLay({
      size: m.lay.size.value, dx: m.lay.dx.value, dy: m.lay.dy.value,
      spacing: m.lay.sp.value, track: m.lay.tr.value,
      wscale: m.lay.ws.value, dottrack: m.lay.dt.value,
      align: m.lay.al.value,
      fill: m.lay.fill && m.lay.fill.checked}));
    items.appendChild(lr);
    m.lrow = lr;
    makeTransform(pg, e, m, sec.querySelector('.canvas'));
    lastMark = m;
  }
  items.style.display = items.children.length ? 'block' : 'none';
  document.getElementById('count').textContent = Object.keys(marked).length;
}
// ---- 우클릭 컨텍스트 메뉴 ----
const ctx = document.createElement('div'); ctx.id = 'ctx';
document.body.appendChild(ctx);
function hideCtx(){ ctx.style.display = 'none'; }
document.addEventListener('click', hideCtx);
document.addEventListener('scroll', hideCtx, true);
function showCtx(ev, items){
  ev.preventDefault();
  ctx.innerHTML = '';
  items.forEach(it => {
    const d = document.createElement('div'); d.className = 'ctxi';
    d.textContent = it[0];
    d.onclick = ev2 => { ev2.stopPropagation(); hideCtx(); it[1](); };
    ctx.appendChild(d);
  });
  ctx.style.display = 'block';
  ctx.style.left = Math.min(ev.clientX, window.innerWidth - 230) + 'px';
  ctx.style.top = Math.min(ev.clientY, window.innerHeight - 300) + 'px';
}
let fmtClip = null;   // 서식 클립보드 — {font, lay}
function copyFmt(pg, e){
  const m = marked[key(pg.file, e.id)];
  if (m) {   // 마킹돼 있으면 현재 입력값(미적용 포함) 기준
    fmtClip = {
      font: (m.font && m.font.value !== '') ? FONTS[+m.font.value]
        : (e.font ? {path: e.font, index: e.font_index || 0} : null),
      lay: normLay({size: m.lay.size.value, spacing: m.lay.sp.value,
                    track: m.lay.tr.value, wscale: m.lay.ws.value,
                    dottrack: m.lay.dt.value, align: m.lay.al.value,
                    fill: m.lay.fill.checked}) || {}};
  } else {   // 저장된 값 기준
    const lay = e.layout || {};
    fmtClip = {
      font: e.font ? {path: e.font, index: e.font_index || 0} : null,
      lay: normLay({size: lay.size || e.used_size, spacing: lay.spacing,
                    track: lay.track, wscale: lay.wscale,
                    dottrack: lay.dottrack, align: lay.align,
                    fill: lay.fill}) || {}};
  }
}
function pasteFmt(pg, e, box, sec){
  if (!fmtClip) { alert('먼저 [서식 복사]를 하세요.'); return; }
  let m = marked[key(pg.file, e.id)];
  if (!m) {   // 마킹 안 된 대상이면 자동 마킹 후 값 주입
    toggle(pg, e, box, sec);
    m = marked[key(pg.file, e.id)];
  }
  if (!m) return;
  const prev = {f: m.font ? m.font.value : '', s: m.lay.size.value,
                p: m.lay.sp.value, t: m.lay.tr.value,
                w: m.lay.ws.value, d: m.lay.dt.value,
                a: m.lay.al.value, fl: m.lay.fill.checked};
  pushUndo(() => {
    if (m.font) m.font.value = prev.f;
    m.lay.size.value = prev.s; m.lay.sp.value = prev.p;
    m.lay.tr.value = prev.t; m.lay.ws.value = prev.w;
    m.lay.dt.value = prev.d; m.lay.al.value = prev.a;
    m.lay.fill.checked = prev.fl;
    m.lay.size.dispatchEvent(new Event('input'));
  });
  if (m.font) {
    if (fmtClip.font) {
      const idx = FONTS.findIndex(f => f.path === fmtClip.font.path
        && f.index === (fmtClip.font.index || 0));
      m.font.value = idx >= 0 ? String(idx) : '';
    } else m.font.value = '';
    m.font.dispatchEvent(new Event('change'));   // lastFont 갱신
  }
  const L = fmtClip.lay || {};
  m.lay.size.value = L.size || '';
  m.lay.sp.value = L.spacing || '';
  m.lay.tr.value = L.track || '';
  m.lay.ws.value = L.wscale || '';
  m.lay.dt.value = L.dottrack || '';
  m.lay.al.value = L.align || 'left';
  m.lay.fill.checked = !!L.fill;
  m.lay.size.dispatchEvent(new Event('input'));
}
async function postDefault(style){
  if (!SERVER) {
    alert('기본 서식 지정은 앱의 [검수 페이지]로 열었을 때 사용 가능합니다.');
    return;
  }
  showBusy('기본 서식 저장 중');
  try {
    const res = await fetch('/api/rework', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify([{page: '*', action: 'set_default',
                             style: style}])});
    hideBusy();
    if (!res.ok) throw new Error('HTTP ' + res.status);
    alert(style
      ? '기본 서식으로 저장했습니다.\n페이지에 반영하려면 재합성 또는 적용을 실행하세요.'
      : '기본 서식을 해제했습니다.\n페이지에 반영하려면 재합성 또는 적용을 실행하세요.');
    sessionStorage.setItem('rvScroll', String(window.scrollY));
    location.reload();
  } catch (err) {
    alert('실패: ' + err.message);
  }
}
function setDefaultFmt(pg, e){
  copyFmt(pg, e);   // 현재 서식 수집
  const lay = Object.assign({}, fmtClip.lay || {});
  delete lay.size;   // 크기는 말풍선별 자동 유지
  const style = {font: fmtClip.font ? fmtClip.font.path : null,
                 font_index: fmtClip.font ? fmtClip.font.index : 0,
                 layout: Object.keys(lay).length ? lay : null};
  if (!style.font && !style.layout) {
    alert('이 말풍선에는 기본값으로 삼을 서식(폰트·자간 등)이 없습니다.');
    return;
  }
  postDefault(style);
}
function bubbleMenu(ev, pg, e, box, sec){
  if (pg._locked) { ev.preventDefault(); return; }
  const cleanCur = (pg._cleans && (e.id in pg._cleans))
    ? pg._cleans[e.id] : !!e.clean;
  const hasRegion = pg._regions && pg._regions[e.id];
  const items = [
    [marked[key(pg.file, e.id)] ? '✔ 마킹 해제' : '✔ 마킹 (편집 열기)',
     () => toggle(pg, e, box, sec)],
    ['🖌 서식 복사 (폰트·크기·자간 등)', () => copyFmt(pg, e)],
    ['🖌 서식 붙여넣기' + (fmtClip ? '' : ' — 복사한 서식 없음'),
     () => pasteFmt(pg, e, box, sec)],
    ['★ 이 서식을 기본값으로', () => setDefaultFmt(pg, e)],
  ];
  if (DEF_STYLE)
    items.push(['★ 기본 서식 해제(리셋)', () => postDefault(null)]);
  items.push(
    [hasRegion ? '✏ 영역 편집 취소' : '✏ 말풍선 영역 편집 (이동·크기)',
     () => toggleRegion(pg, e, sec)],
    [cleanCur ? '🧹 흰여백 칠하기 해제' : '🧹 영역 흰여백 지우고 칠하기',
     () => toggleClean(pg, e, box)]);
  showCtx(ev, items);
}
function toggleClean(pg, e, box){
  pg._cleans = pg._cleans || {};
  const cur = (e.id in pg._cleans) ? pg._cleans[e.id] : !!e.clean;
  const nv = !cur;
  if (nv === !!e.clean) delete pg._cleans[e.id];
  else pg._cleans[e.id] = nv;
  if (nv) box.classList.add('clean');
  else box.classList.remove('clean');
}
function toggleRegion(pg, e, sec){
  pg._regions = pg._regions || {};
  const cv = sec.querySelector('.canvas');
  if (pg._regions[e.id]) {
    pg._regions[e.id].el.remove();
    delete pg._regions[e.id];
    return;
  }
  const el = document.createElement('div'); el.className = 'rg';
  el.title = '말풍선 영역 — 드래그=이동, 모서리=크기 (우클릭 메뉴로 취소)';
  const hd = document.createElement('div'); hd.className = 'rgh';
  el.appendChild(hd);
  cv.appendChild(el);
  el.addEventListener('contextmenu',
    ev => bubbleMenu(ev, pg, e, e._box || el, sec));
  const rg = {bbox: (e.region_bbox || e.bbox).slice(), el: el};
  pg._regions[e.id] = rg;
  const paint = () => {
    el.style.left = (rg.bbox[0] / pg.w * 100) + '%';
    el.style.top = (rg.bbox[1] / pg.h * 100) + '%';
    el.style.width = (rg.bbox[2] / pg.w * 100) + '%';
    el.style.height = (rg.bbox[3] / pg.h * 100) + '%';
  };
  paint();
  function drag(ev0, mode){
    ev0.preventDefault(); ev0.stopPropagation();
    const r = cv.getBoundingClientRect();
    const st = {mx: ev0.clientX, my: ev0.clientY, b: rg.bbox.slice()};
    const mv = e2 => {
      const ddx = (e2.clientX - st.mx) / r.width * pg.w;
      const ddy = (e2.clientY - st.my) / r.height * pg.h;
      if (mode === 'move') {
        rg.bbox[0] = st.b[0] + ddx;
        rg.bbox[1] = st.b[1] + ddy;
      } else {
        rg.bbox[2] = Math.max(10, st.b[2] + ddx);
        rg.bbox[3] = Math.max(10, st.b[3] + ddy);
      }
      paint();
    };
    const up = () => {
      document.removeEventListener('mousemove', mv);
      document.removeEventListener('mouseup', up);
      if (JSON.stringify(st.b) !== JSON.stringify(rg.bbox))
        pushUndo(() => {   // 영역 편집 드래그 언두
          rg.bbox = st.b.slice();
          if (pg._regions && pg._regions[e.id] === rg) paint();
        });
    };
    document.addEventListener('mousemove', mv);
    document.addEventListener('mouseup', up);
  }
  el.addEventListener('mousedown', ev0 => {
    if (ev0.target === hd) return;
    drag(ev0, 'move');
  });
  hd.addEventListener('mousedown', ev0 => drag(ev0, 'size'));
}
function makeTransform(pg, e, m, cv){
  // 포토샵 자유 변형식 조절 박스 — 드래그=이동(dx/dy),
  // 모서리=비례 크기 / 우측=장평(가로만) / 하단=세로만(장평 자동 보정).
  const tf = document.createElement('div'); tf.className = 'tf';
  tf.title = '드래그=이동 / 모서리=비례 / 우측=가로 / 하단=세로';
  const hd = document.createElement('div'); hd.className = 'tfh';
  const he = document.createElement('div'); he.className = 'tfe';
  const hs = document.createElement('div'); hs.className = 'tfs';
  tf.appendChild(hd); tf.appendChild(he); tf.appendChild(hs);
  cv.appendChild(tf);
  m.tf = tf;
  // 마킹 시 트랜스폼 박스가 말풍선을 덮어 우클릭을 가로챔 — 메뉴 연결
  tf.addEventListener('contextmenu',
    ev => bubbleMenu(ev, pg, e, e._box || tf, pg._sec));
  const nLines = () => Math.max(1, m.ta.value
    .replace(/[\s　]+$/, '').split('\n').length);  // 빈 줄도 간격으로
  const spacing = () => parseFloat(m.lay.sp.value) || 1.15;
  const wsv = () => parseFloat(m.lay.ws.value) || 1;
  const trv = () => parseFloat(m.lay.tr.value) || 1;
  const fillv = () => !!(m.lay.fill && m.lay.fill.checked);
  function geom(){
    const bb = e.bbox;
    const dx = parseFloat(m.lay.dx.value) || 0;
    const dy = parseFloat(m.lay.dy.value) || 0;
    const sz = parseFloat(m.lay.size.value) || 0;
    const h = (!fillv() && sz > 0) ? sz * spacing() * nLines() : bb[3];
    const w = Math.max(20,
      ((!fillv() && sz > 0) ? bb[2] * (h / bb[3]) : bb[2]) * wsv() * trv());
    return [bb[0] + bb[2] / 2 + dx - w / 2,
            bb[1] + bb[3] / 2 + dy - h / 2, w, h];
  }
  function paint(){
    const g = geom();
    tf.style.left = (g[0] / pg.w * 100) + '%';
    tf.style.top = (g[1] / pg.h * 100) + '%';
    tf.style.width = (g[2] / pg.w * 100) + '%';
    tf.style.height = (g[3] / pg.h * 100) + '%';
  }
  paint();
  [m.lay.size, m.lay.dx, m.lay.dy, m.lay.sp, m.lay.tr, m.lay.ws,
   m.lay.fill, m.ta]
    .forEach(el => el.addEventListener('input', paint));
  function startDrag(ev, mode){
    ev.preventDefault(); ev.stopPropagation();
    lastMark = m;   // 넛지 대상 갱신
    const r = cv.getBoundingClientRect();
    const g0 = geom();
    const before = {s: m.lay.size.value, x: m.lay.dx.value,
                    y: m.lay.dy.value, w: m.lay.ws.value};
    const st = {mx: ev.clientX, my: ev.clientY,
                dx: parseFloat(m.lay.dx.value) || 0,
                dy: parseFloat(m.lay.dy.value) || 0,
                w0: g0[2], h0: g0[3], ws: wsv(), fill: fillv(),
                s0: parseFloat(m.lay.size.value)
                    || Math.max(10, Math.round(e.bbox[3] / nLines() / 1.3))};
    const move = e2 => {
      const ddx = (e2.clientX - st.mx) / r.width * pg.w;
      const ddy = (e2.clientY - st.my) / r.height * pg.h;
      if (mode === 'move') {
        m.lay.dx.value = Math.round(st.dx + ddx);
        m.lay.dy.value = Math.round(st.dy + ddy);
      } else if (mode === 'w') {           // 가로만 — 장평
        const nw = Math.max(10, st.w0 + ddx);
        const ws = nw / (st.w0 / st.ws);
        m.lay.ws.value = Math.max(0.2, Math.round(ws * 100) / 100);
      } else {                             // 세로 (비례 or 세로만)
        const nh = Math.max(10, st.h0 + ddy);
        if (st.fill) {                     // 영역 유지 — 폰트 크기만 비율로
          m.lay.size.value = Math.max(6,
            Math.round(st.s0 * nh / st.h0));
        } else {
          m.lay.size.value = Math.max(6,
            Math.round(nh / (spacing() * nLines())));
          if (mode === 'v') {              // 세로만 — 폭 유지 장평 보정
            const ws = st.ws * st.h0 / nh;
            m.lay.ws.value = Math.max(0.2, Math.round(ws * 100) / 100);
          }
        }
      }
      paint();
    };
    const up = () => {
      document.removeEventListener('mousemove', move);
      document.removeEventListener('mouseup', up);
      if (before.s !== m.lay.size.value || before.x !== m.lay.dx.value
          || before.y !== m.lay.dy.value || before.w !== m.lay.ws.value)
        pushUndo(() => {   // 트랜스폼 드래그 언두
          m.lay.size.value = before.s;
          m.lay.dx.value = before.x;
          m.lay.dy.value = before.y;
          m.lay.ws.value = before.w;
          paint();
        });
    };
    document.addEventListener('mousemove', move);
    document.addEventListener('mouseup', up);
  }
  tf.addEventListener('mousedown', ev => {
    if (ev.target !== tf) return;
    startDrag(ev, 'move');
  });
  hd.addEventListener('mousedown', ev => startDrag(ev, 'size'));
  he.addEventListener('mousedown', ev => startDrag(ev, 'w'));
  hs.addEventListener('mousedown', ev => startDrag(ev, 'v'));
}
function collectActions(pageFile){
  const out = [];
  DATA.forEach(pg => {
    if (pageFile && pg.file !== pageFile) return;
    if (pg._locked !== !!pg.locked)
      out.push({page: pg.file, action: pg._locked ? 'lock' : 'unlock'});
    if (pg._locked) return;
    if (pg._rebuild)
      out.push({page: pg.file, action: 'rebuild'});
    (pg._adds || []).forEach(a => {
      const it = {page: pg.file, action: 'add', bbox: a.bbox};
      const t = a.ta.value.trim();
      if (t) it.text = t;
      if (a.font && a.font.value !== '') {
        const f = FONTS[+a.font.value];
        it.font = f.path; it.font_index = f.index;
      }
      out.push(it);
    });
    Object.entries(pg._cleans || {}).forEach(kv => {
      out.push({page: pg.file, id: +kv[0], action: 'clean', clean: kv[1]});
    });
    Object.entries(pg._regions || {}).forEach(kv => {
      out.push({page: pg.file, id: +kv[0], action: 'region',
                bbox: kv[1].bbox.map(Math.round)});
    });
    if (pg._paintDirty && pg._pcv) {   // 브러시 획 변경분
      const c = pg._pcv;
      const a = c.getContext('2d')
        .getImageData(0, 0, c.width, c.height).data;
      let has = false;
      for (let i = 3; i < a.length; i += 4) {
        if (a[i] > 40) { has = true; break; }
      }
      out.push({page: pg.file, action: 'paint',
                mask: has ? c.toDataURL('image/png') : null});
    }
  });
  for (const k in marked) {
    const m = marked[k];
    if (pageFile && m.page !== pageFile) continue;
    const t = m.ta.value;
    const fsel = (m.font && m.font.value !== '') ? FONTS[+m.font.value] : null;
    const fchg = m.font && ((fsel ? fsel.path : '') !== (m.origFont || '')
                 || (fsel ? fsel.index : 0) !== (m.origIdx || 0));
    let layObj = null, lchg = false;
    if (m.lay) {
      layObj = normLay({size: m.lay.size.value, dx: m.lay.dx.value,
                        dy: m.lay.dy.value, spacing: m.lay.sp.value,
                        track: m.lay.tr.value, wscale: m.lay.ws.value,
                        dottrack: m.lay.dt.value, align: m.lay.al.value,
                        fill: m.lay.fill && m.lay.fill.checked});
      lchg = JSON.stringify(layObj) !== m.origLay;
    }
    const withExtra = it => {
      if (fchg) {
        it.font = fsel ? fsel.path : null;
        if (fsel) it.font_index = fsel.index;
      }
      if (lchg) it.layout = layObj;
      return it;
    };
    if (m.keep && m.keep.checked)
      out.push({page: m.page, id: m.id, action: 'keep'});
    else if (t.trim() !== m.orig.trim()) {
      if (!t.trim()) continue;   // 안전장치: 지우다 만 빈 텍스트는 미적용
                                 // (글자를 없애려면 '원본 보존' 체크 사용)
      out.push(withExtra({page: m.page, id: m.id, action: 'text', text: t}));
    }
    else if (fchg || lchg)
      out.push(withExtra({page: m.page, id: m.id, action: 'style'}));
    else
      out.push({page: m.page, id: m.id, action: 'ai'});
  }
  return out;
}
function save(){
  const out = collectActions(null);
  if (!out.length) { alert('마킹·잠금·영역 변경이 없습니다.'); return; }
  const blob = new Blob([JSON.stringify(out, null, 2)],
                        {type: 'application/json'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob); a.download = 'rework.json'; a.click();
  alert('rework.json 저장됨 (다운로드 폴더) — 앱의 [검수 반영]에서 선택하세요.');
}
// ---- 작업 중 오버레이 (스피너+경과 시간) ----
const busyEl = document.createElement('div'); busyEl.id = 'busy';
busyEl.innerHTML = '<div class="spin"></div><div id="busytext"></div>';
document.body.appendChild(busyEl);
let busyTimer = null;
function showBusy(msg){
  const t0 = Date.now();
  const tx = document.getElementById('busytext');
  tx.textContent = msg;
  busyEl.classList.add('on');
  if (busyTimer) clearInterval(busyTimer);
  busyTimer = setInterval(() => {
    tx.textContent = msg + ' — ' +
      Math.round((Date.now() - t0) / 1000) + '초';
  }, 1000);
}
function hideBusy(){
  busyEl.classList.remove('on');
  if (busyTimer) { clearInterval(busyTimer); busyTimer = null; }
}
function saveRemark(pageFile){
  // 새로고침 후에도 작업하던 마킹을 복원 — 편집 흐름 유지
  const ids = [];
  for (const k in marked)
    if (marked[k].page === pageFile) ids.push(marked[k].id);
  try {
    sessionStorage.setItem('rvRemark',
      JSON.stringify({page: pageFile, ids: ids}));
  } catch (e) {}
}
async function applyPage(pg, btn){
  const acts = collectActions(pg.file);
  if (!acts.length) { alert('이 페이지에 적용할 변경이 없습니다.'); return; }
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = '적용 중…';
  const hasAI = acts.some(a => a.action === 'ai'
    || (a.action === 'add' && !a.text));
  showBusy(hasAI
    ? '적용 중 (AI 재전사 포함 — 수십 초 걸릴 수 있음)'
    : '페이지 적용 중');
  try {
    const res = await fetch('/api/rework', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(acts)});
    if (!res.ok)
      throw new Error(res.status === 409
        ? '앱이 다른 작업을 실행 중입니다' : 'HTTP ' + res.status);
    saveRemark(pg.file);
    sessionStorage.setItem('rvScroll', String(window.scrollY));
    location.reload();   // 오버레이는 새 화면 로드까지 유지
  } catch (err) {
    hideBusy();
    alert('적용 실패: ' + err.message);
    btn.disabled = false;
    btn.textContent = old;
  }
}
window.addEventListener('load', () => {
  updUndo();                       // 실행취소 버튼 초기 상태
  if (zoom !== 1) setZoom(zoom);   // 저장된 배율 복원 (적용 후 새로고침 유지)
  let bo = null;
  try { bo = localStorage.getItem('rvBxo'); } catch (e) {}
  if (bo && +bo !== 40) setBoxOpacity(+bo);
  const s = sessionStorage.getItem('rvScroll');
  if (s) { window.scrollTo(0, +s); sessionStorage.removeItem('rvScroll'); }
  // 적용 직전 마킹 복원 — 패널이 사라지지 않고 이어서 편집 가능
  try {
    const rm = JSON.parse(sessionStorage.getItem('rvRemark') || 'null');
    sessionStorage.removeItem('rvRemark');
    if (rm && rm.ids && rm.ids.length) {
      const pg2 = DATA.find(p => p.file === rm.page);
      if (pg2 && !pg2._locked) {
        rm.ids.forEach(id => {
          const e2 = pg2.review.find(x => x.id === id);
          if (e2 && e2._box) toggle(pg2, e2, e2._box, pg2._sec);
        });
      }
    }
  } catch (e) {}
});
</script></body></html>"""


def merge_review(out_dir: Path, results: list) -> list:
    """이번 실행 결과와 기존 review.json 병합.

    건너뛴 페이지(skipped/locked)는 기존 리뷰 데이터를 보존하고,
    이번 실행에 없는 페이지 항목도 유지한다."""
    rj = out_dir / "review.json"
    old = {}
    if rj.exists():
        try:
            old = {r.get("file"): r
                   for r in json.loads(rj.read_text(encoding="utf-8"))}
        except Exception:
            old = {}
    seen = set()
    merged = []
    for r in results:
        f = r.get("file")
        seen.add(f)
        if r.get("status") in ("skipped", "locked") and f in old:
            merged.append(old[f])
        else:
            merged.append(r)
    merged += [r for f, r in old.items() if f not in seen]
    return merged


def load_locked(out_dir: Path) -> set:
    """review.json에서 수동 확정 잠금된 페이지 이름 집합."""
    rj = out_dir / "review.json"
    if not rj.exists():
        return set()
    try:
        return {r.get("file")
                for r in json.loads(rj.read_text(encoding="utf-8"))
                if r.get("locked")}
    except Exception:
        return set()


def write_review_html(out_dir: Path) -> Optional[Path]:
    """review.json + *_final.png → 클릭 마킹형 검수 페이지 생성."""
    rj = out_dir / "review.json"
    if not rj.exists():
        return None
    results = json.loads(rj.read_text(encoding="utf-8"))
    pages = []
    for r in results:
        if r.get("status") != "ok" or not r.get("review"):
            continue
        stem = Path(r["file"]).stem
        img = out_dir / f"{stem}_final.png"
        if not img.exists():
            continue
        with Image.open(img) as im:
            w, h = im.size
        # ?t=mtime — 적용 후 브라우저가 캐시된 옛 이미지를 보여주는 것 방지
        page = {"file": r["file"],
                "img": f"{img.name}?t={int(img.stat().st_mtime)}",
                "w": w, "h": h,
                "locked": bool(r.get("locked")),
                "has_prev": bool(r.get("prev")),
                "review": r["review"]}
        up = out_dir / "_upscaled" / f"{stem}.png"
        if up.exists():   # 업스케일 원본 (재조판 전) — 비교 보기용
            page["orig"] = f"_upscaled/{up.name}"
        pm = out_dir / "_paint" / f"{stem}.png"
        if pm.exists():   # 저장된 브러시 마스크 — 캔버스에 복원
            page["paint"] = f"_paint/{pm.name}?t={int(pm.stat().st_mtime)}"
        pages.append(page)
    if not pages:
        return None
    fonts = [{"label": lb, "path": pth, "index": ix}
             for lb, pth, ix in resolve_presets(FONT_PRESETS + HAND_PRESETS)]
    defstyle = None
    sdp = out_dir / "_style_default.json"
    if sdp.exists():
        try:
            defstyle = json.loads(sdp.read_text(encoding="utf-8"))
        except Exception:
            defstyle = None
    html = (_REVIEW_HTML
            .replace("__DATA__", json.dumps(pages, ensure_ascii=False))
            .replace("__FONTS__", json.dumps(fonts, ensure_ascii=False))
            .replace("__DEF__", json.dumps(defstyle, ensure_ascii=False)))
    p = out_dir / "review.html"
    p.write_text(html, encoding="utf-8")
    return p


def _apply_font(e: dict, it: dict) -> None:
    """액션에 폰트 지정이 실려 있으면 엔트리에 반영 (font=null이면 기본 복귀)."""
    if "font" not in it:
        return
    f = it.get("font")
    if f:
        e["font"] = f
        e["font_index"] = int(it.get("font_index") or 0)
    else:
        e.pop("font", None)
        e.pop("font_index", None)


def _entries_from_review(r: dict) -> list:
    """review 결과 딕셔너리 → process_page transcript 엔트리 목록."""
    entries = []
    for e in r.get("review", []):
        ent = {"id": e["id"], "text": e.get("text"),
               "kind": e.get("kind", "none"),
               "confidence": e.get("confidence", "low")}
        if e.get("manual_bbox"):
            ent["manual_bbox"] = e["manual_bbox"]
        if e.get("font"):
            ent["font"] = e["font"]
            ent["font_index"] = int(e.get("font_index") or 0)
        if e.get("layout"):
            ent["layout"] = e["layout"]
        if e.get("region_bbox"):
            ent["region_bbox"] = e["region_bbox"]
        if e.get("clean"):
            ent["clean"] = True
        entries.append(ent)
    return entries


def _apply_layout(e: dict, it: dict) -> None:
    """액션에 레이아웃 지정이 실려 있으면 반영 (layout=null이면 자동 복귀)."""
    if "layout" not in it:
        return
    lay = it.get("layout")
    if lay:
        e["layout"] = lay
    else:
        e.pop("layout", None)


def apply_rework(out_dir: Path, rework_path: Path, args, pages_dir: Path,
                 log=print) -> int:
    """rework.json의 마킹만 재처리. 나머지 말풍선은 기존 전사 재사용(비용 0).

    action="text":   사용자가 고친 텍스트로 확정(수동, API 없음).
    action="ai":     고정밀 크롭(축소 없음·소형 2배 확대)으로 합의 재전사.
    action="keep":   재조판 취소 — 해당 말풍선을 원본 그대로 보존.
    action="add":    감지 누락 영역 수동 지정 — bbox 필수, text 있으면
                     수동 확정, 없으면 고정밀 전사.
    action="rebuild": 페이지 재합성 — 기존 전사 재사용, API 비용 0.
    action="lock"/"unlock": 페이지 수동 확정 잠금 — 잠긴 페이지는
                     재조판·전체 실행에서 건드리지 않음 (포토샵 수정 보호).

    rework_path는 파일 경로 또는 액션 리스트를 직접 받을 수 있다."""
    rj = out_dir / "review.json"
    if not rj.exists():
        raise RuntimeError(f"review.json이 없습니다: {rj}")
    results = json.loads(rj.read_text(encoding="utf-8"))
    by_page = {r["file"]: r for r in results if r.get("status") == "ok"}
    if isinstance(rework_path, (list, tuple)):
        items = list(rework_path)
    else:
        items = json.loads(Path(rework_path).read_text(encoding="utf-8"))

    def _transcribe(cs: list) -> list[dict]:
        eng = getattr(args, "ocr_engine", "claude")
        if eng != "claude":
            return transcribe_local(cs, eng)
        return transcribe_consensus(cs, args.model)
    per_page: dict = {}
    for it in items:
        if it.get("action") in ("lock", "unlock"):
            r = by_page.get(it["page"])
            if r is None:
                log(f"  !! review.json에 없는 페이지: {it['page']} — "
                    "잠금 건너뜀")
                continue
            r["locked"] = it["action"] == "lock"
            log(f"  {it['page']}: "
                f"{'수동 확정 잠금' if r['locked'] else '잠금 해제'}")
            continue
        if it.get("action") == "rebuild":
            # 말풍선 변경 없이 페이지 재합성 — 기존 전사 재사용 (API 비용 0).
            # 배경 처리 방식 변경 등을 기존 결과물에 적용할 때 사용.
            per_page.setdefault(it["page"], [])
            continue
        if it.get("action") == "revert":
            continue   # 아래 별도 패스에서 처리
        if it.get("action") == "set_default":
            # 기본 서식 저장/해제 — 이후 재조판·재합성부터 전체 적용
            sdp = out_dir / "_style_default.json"
            style = it.get("style")
            if style and (style.get("font") or style.get("layout")):
                sdp.write_text(json.dumps(style, ensure_ascii=False,
                                          indent=2), encoding="utf-8")
                log("  기본 서식 저장 — 개별 지정 없는 말풍선에 적용됨 "
                    "(반영은 페이지 재합성/적용 시)")
            else:
                try:
                    sdp.unlink()
                except OSError:
                    pass
                log("  기본 서식 해제")
            continue
        if it.get("action") == "paint":
            # 브러시 마스크 저장/삭제 후 페이지 재합성 (API 비용 0)
            pd = out_dir / "_paint"
            fp2 = pd / f"{Path(it['page']).stem}.png"
            mk = it.get("mask")
            if mk:
                pd.mkdir(parents=True, exist_ok=True)
                fp2.write_bytes(base64.b64decode(mk.split(",", 1)[-1]))
            else:
                try:
                    fp2.unlink()
                except OSError:
                    pass
            per_page.setdefault(it["page"], [])
            continue
        per_page.setdefault(it["page"], []).append(it)

    done = 0
    for page_name, its in per_page.items():
        r = by_page.get(page_name)
        if r is None:
            log(f"  !! review.json에 없는 페이지: {page_name} — 건너뜀")
            continue
        if r.get("locked"):
            log(f"  !! {page_name}: 수동 확정 잠금 — 재조판 건너뜀")
            continue
        pf = pages_dir / page_name
        if not pf.exists():
            log(f"  !! 페이지 파일 없음: {pf} — 건너뜀")
            continue
        entries = _entries_from_review(r)   # 기존 상태 유지 항목 포함
        idmap = {e["id"]: e for e in entries}
        ai_ids = []
        adds = []
        for it in its:
            if it.get("action") == "add":
                adds.append(it)
                continue
            e = idmap.get(int(it["id"]))
            if e is None:
                log(f"  !! {page_name} id {it['id']} 없음 — 건너뜀")
                continue
            if it.get("action") == "text":
                tv = it.get("text") or ""
                e["text"] = tv if tv.strip() else None  # 들여쓰기 공백 유지
                e["confidence"] = "high"
                # 직접 입력한 텍스트는 반드시 재조판되도록 kind 보정.
                # 손글씨(hand)는 손글씨 재조판 설정+폰트가 있을 때만 유지
                # (없으면 보존 정책에 걸려 글씨가 안 나옴)
                if not (e.get("kind") == "hand"
                        and getattr(args, "retype_hand", False)
                        and getattr(args, "hand_font", None)):
                    e["kind"] = "dialogue"
                e["passes"] = "manual"
                _apply_font(e, it)
                _apply_layout(e, it)
            elif it.get("action") == "keep":
                # 재조판 취소 — 이 말풍선은 원본 그대로 보존
                e["kind"] = "preserved"
                e["passes"] = "keep"
            elif it.get("action") in ("font", "style", "layout"):
                # 폰트/레이아웃만 변경 — 기존 전사 재사용, API 비용 0
                _apply_font(e, it)
                _apply_layout(e, it)
                e["passes"] = "style"
            elif it.get("action") == "region":
                # 말풍선 영역 재정의 — 마스크·배치 기준 변경, API 비용 0
                bb2 = [int(v) for v in (it.get("bbox") or [])]
                if len(bb2) == 4 and bb2[2] >= 8 and bb2[3] >= 8:
                    e["region_bbox"] = bb2
                else:
                    e.pop("region_bbox", None)
            elif it.get("action") == "clean":
                # 흰여백 청소 토글 — 영역 전체 종이색 칠, API 비용 0
                if it.get("clean"):
                    e["clean"] = True
                else:
                    e.pop("clean", None)
            else:
                _apply_font(e, it)
                _apply_layout(e, it)
                ai_ids.append(int(it["id"]))

        if ai_ids or adds:
            img = imread_unicode(pf)
            restored, _ = restore_page(img, text_black=args.text_black,
                                       text_white=args.text_white,
                                       thicken=args.thicken, paper=args.paper,
                                       denoise=not args.no_denoise)
            rgray = cv2.cvtColor(restored, cv2.COLOR_BGR2GRAY)
            bubbles = detect_bubbles(rgray)
            # 기존 수동 영역도 감지 목록 뒤에 붙여 id 정합 유지
            for e in sorted((e for e in entries if e.get("manual_bbox")),
                            key=lambda x: x.get("id", 0)):
                bubbles.append(make_manual_bubble(rgray, e["manual_bbox"]))
            crops, valid = [], []
            for bid in ai_ids:
                if 1 <= bid <= len(bubbles):
                    crops.append(crop_bubble_hires(restored,
                                                   bubbles[bid - 1]))
                    valid.append(bid)
                else:
                    log(f"  !! {page_name} id {bid}: 감지 불일치 — 건너뜀")
            if crops:
                log(f"  {page_name}: {len(crops)}개 고정밀 재전사 중…")
                res = _transcribe(crops)
                old_retyped = {x.get("id"): bool(x.get("retyped"))
                               for x in r.get("review", [])}
                for bid, rr in zip(valid, res):
                    e = idmap[bid]
                    new_kind = rr.get("kind", "none")
                    new_conf = rr.get("confidence", "low")
                    hand_ok = (new_kind == "hand"
                               and getattr(args, "retype_hand", False)
                               and getattr(args, "hand_font", None))
                    will_show = bool((rr.get("text") or "").strip()) \
                        and new_conf == "high" \
                        and (new_kind == "dialogue" or hand_ok)
                    if old_retyped.get(bid) and not will_show:
                        # 회귀 방지 — 재전사 결과가 비확신/비대사 분류라
                        # 이미 보이던 글씨가 사라질 상황이면 기존 텍스트를
                        # 유지하고 새 후보는 alt에만 기록
                        e["alt"] = {"rework_text": rr.get("text"),
                                    "kind": new_kind,
                                    "confidence": new_conf}
                        e["passes"] = "rework-kept"
                        log(f"  !! {page_name} id {bid}: 재전사 결과 "
                            f"{new_kind}/{new_conf} — 기존 텍스트 유지"
                            " (후보는 review.json alt 참고)")
                        continue
                    e["text"] = rr.get("text")
                    e["kind"] = new_kind
                    e["confidence"] = new_conf
                    for k in ("alt", "fixed", "uncertain"):
                        if rr.get(k):
                            e[k] = rr[k]
                    e["passes"] = f"rework-{rr.get('passes') or 'ai'}"
                    if not old_retyped.get(bid) and not will_show \
                            and (rr.get("text") or "").strip():
                        # 미처리(주황) 말풍선을 사용자가 마킹해 적용 =
                        # 처리 지시 — 분류·확신도와 무관하게 재조판 승격
                        if not hand_ok:
                            e["kind"] = "dialogue"
                        e["confidence"] = "high"
                        e["passes"] = "rework-forced"
                        log(f"  {page_name} id {bid}: {new_kind}/{new_conf}"
                            " → 사용자 지시로 재조판 강제 (uncertain은 "
                            "review.json 참고)")

            # 수동 지정 영역 추가
            next_id = max((e["id"] for e in entries), default=0) + 1
            new_crops, new_entries = [], []
            for ad in adds:
                bbox = [int(v) for v in (ad.get("bbox") or [])]
                if len(bbox) != 4 or bbox[2] < 8 or bbox[3] < 8:
                    log(f"  !! {page_name}: 잘못된 영역 {bbox} — 건너뜀")
                    continue
                mb = make_manual_bubble(rgray, bbox)
                ent = {"id": next_id, "kind": "dialogue",
                       "confidence": "high",
                       "manual_bbox": list(mb.bbox)}
                _apply_font(ent, ad)
                _apply_layout(ent, ad)
                txt = ad.get("text") or ""
                if txt.strip():
                    ent["text"] = txt          # 들여쓰기 공백 유지
                    ent["passes"] = "manual-add"
                else:
                    new_crops.append(crop_bubble_hires(restored, mb))
                    new_entries.append(ent)
                entries.append(ent)
                next_id += 1
            if new_crops:
                log(f"  {page_name}: 수동 영역 {len(new_crops)}개 전사 중…")
                res2 = _transcribe(new_crops)
                for ent, rr in zip(new_entries, res2):
                    ent["text"] = rr.get("text")
                    ent["kind"] = rr.get("kind") or "dialogue"
                    ent["confidence"] = rr.get("confidence", "low")
                    for k in ("alt", "fixed", "uncertain"):
                        if rr.get(k):
                            ent[k] = rr[k]
                    ent["passes"] = f"add-{rr.get('passes') or 'ai'}"

        log(f"  {page_name}: 재조판 중…")
        new_r = process_page(pf, out_dir, args, {page_name: entries})
        # 1단계 백업 — [적용 되돌리기]용 직전 상태 보관
        new_r["prev"] = {k: v for k, v in r.items() if k != "prev"}
        if r.get("locked"):
            new_r["locked"] = True
        for i, old in enumerate(results):
            if old.get("file") == page_name:
                results[i] = new_r
                break
        by_page[page_name] = new_r
        done += 1

    # 적용 되돌리기(revert) — 직전 적용 이전 상태로 페이지 재구성.
    # 브러시 칠(_paint)은 파일 기반이라 되돌리기 대상이 아님.
    for it in [i for i in items if i.get("action") == "revert"]:
        page_name = it["page"]
        r = by_page.get(page_name)
        if r is None:
            log(f"  !! review.json에 없는 페이지: {page_name} — 건너뜀")
            continue
        if r.get("locked"):
            log(f"  !! {page_name}: 수동 확정 잠금 — 되돌리기 건너뜀")
            continue
        prev = r.get("prev")
        if not prev:
            log(f"  !! {page_name}: 되돌릴 이전 상태가 없습니다")
            continue
        pf = pages_dir / page_name
        if not pf.exists():
            log(f"  !! 페이지 파일 없음: {pf} — 건너뜀")
            continue
        log(f"  {page_name}: 직전 적용 취소 — 재조판 중…")
        new_r = process_page(pf, out_dir, args,
                             {page_name: _entries_from_review(prev)})
        new_r["prev"] = {k: v for k, v in r.items() if k != "prev"}
        for i2, old in enumerate(results):
            if old.get("file") == page_name:
                results[i2] = new_r
                break
        by_page[page_name] = new_r
        done += 1

    rj.write_text(json.dumps(results, ensure_ascii=False, indent=2),
                  encoding="utf-8")
    write_review_html(out_dir)
    return done


def main() -> int:
    ap = argparse.ArgumentParser(description="말풍선 한글 재조판 파이프라인 (v3)")
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--font", required=True, help="나눔명조 Bold .ttf 경로")
    ap.add_argument("--font-index", type=int, default=0, help=".ttc 폰트 인덱스")
    ap.add_argument("--retype-hand", action="store_true",
                    help="손글씨 대사·메모·쪽지(kind=hand)도 재조판. "
                         "기본은 보존(재조판 안 함). --hand-font 필수")
    ap.add_argument("--hand-font", default=None,
                    help="손글씨 대사용 폰트 .ttf (예: NanumBrush.ttf)")
    ap.add_argument("--hand-font-index", type=int, default=0)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--ext", default=".png,.jpg,.jpeg,.webp")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--no-psd", action="store_true")
    ap.add_argument("--model", default="claude-sonnet-4-5",
                    help="전사용 Claude 모델 (기본 claude-sonnet-4-5)")
    ap.add_argument("--fast-transcribe", action="store_true",
                    help="1회 전사만 수행 (기본은 이중 전사+불일치 검증 — "
                         "정확도 높음, API 비용 2~3배)")
    ap.add_argument("--no-resume", action="store_true",
                    help="완료본(_final.png)이 있는 페이지도 다시 처리 "
                         "(기본은 건너뜀 — 크레딧 절약)")
    ap.add_argument("--no-preserve-bg", dest="preserve_bg",
                    action="store_false", default=True,
                    help="원본 배경 100% 보존 끄기 — 근백색 영역만 "
                         "v2 보정 블렌딩 (구 중간 단계 동작)")
    ap.add_argument("--batch", action="store_true",
                    help="Message Batches API로 전사 (비용 50% 할인, "
                         "완료까지 폴링 대기)")
    ap.add_argument("--ocr-engine", default="claude",
                    choices=["claude", "windows", "tesseract", "easyocr"],
                    help="전사 엔진 — claude(기본·정확) 외 로컬 OCR은 "
                         "무료지만 정확도 낮고 손글씨/효과음 분류 불가")
    ap.add_argument("--rework", default=None,
                    help="검수 페이지에서 저장한 rework.json 경로 — "
                         "마킹된 말풍선만 재조판 (--src는 원래 재조판 "
                         "입력 폴더)")
    ap.add_argument("--html-only", action="store_true",
                    help="재조판 없이 review.json으로 검수 페이지만 "
                         "재생성하고 종료")
    ap.add_argument("--strict", action="store_true", default=True,
                    help="high 확신도만 재조판 (기본 켜짐)")
    ap.add_argument("--no-strict", dest="strict", action="store_false",
                    help="medium/low도 재조판")
    ap.add_argument("--export-crops", action="store_true",
                    help="API 호출 없이 말풍선 크롭+manifest만 내보내기")
    ap.add_argument("--transcript", default=None,
                    help="manifest.json 경로 — API 대신 파일의 text 사용")
    ap.add_argument("--restored-base", action="store_true",
                    help="최종 합성 베이스로 v2 전면 보정본 사용 (구 동작). "
                         "기본은 원본 그림 보존 + 말풍선 영역만 보정 블렌딩")
    # v2 보정 파라미터
    ap.add_argument("--text-black", type=int, default=80)
    ap.add_argument("--text-white", type=int, default=210)
    ap.add_argument("--thicken", type=float, default=0.5)
    ap.add_argument("--paper", type=int, default=215)
    ap.add_argument("--no-denoise", action="store_true")
    args = ap.parse_args()

    if not args.export_crops and not args.transcript \
            and args.ocr_engine == "claude" \
            and not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY 환경변수가 없습니다. API 없이 "
              "쓰려면 --export-crops 또는 --ocr-engine 로컬 엔진을 쓰세요.",
              file=sys.stderr)
        return 2

    transcript = None
    if args.transcript:
        entries = json.loads(Path(args.transcript).read_text(encoding="utf-8"))
        transcript = {}
        for e in entries:
            transcript.setdefault(e["page"], []).append(e)

    src, out = Path(args.src), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    # 검수 페이지만 재생성 (재조판·API 없음)
    if args.html_only:
        hp = write_review_html(out)
        print(f"검수 페이지 재생성: {hp}" if hp
              else "review.json이 없거나 표시할 페이지가 없습니다.")
        return 0

    # 재검수 모드: rework.json의 마킹만 재조판하고 종료
    if args.rework:
        n = apply_rework(out, Path(args.rework), args, src)
        print(f"검수 반영 완료 — {n}페이지 재조판. "
              f"검수 페이지: {out / 'review.html'}")
        return 0

    exts = {e.strip().lower() for e in args.ext.split(",") if e.strip()}
    files = sorted(p for p in src.iterdir() if p.suffix.lower() in exts)
    if args.limit:
        files = files[:args.limit]
    if not files:
        print(f"이미지 없음: {src}", file=sys.stderr)
        return 1

    locked = load_locked(out)   # 수동 확정 페이지 — 절대 재처리 안 함

    # Batch API 전사: 전 페이지 크롭 수집 → 배치 제출 → transcript로 주입
    if args.batch and not args.export_crops and transcript is None \
            and args.ocr_engine == "claude":
        todo = [f for f in files if f.name not in locked
                and (args.no_resume
                     or not (out / f"{f.stem}_final.png").exists())]
        if todo:
            print(f"배치 전사 준비 — {len(todo)}페이지 말풍선 감지 중…",
                  flush=True)
            pages = []
            for f in todo:
                crops = prepare_crops(f, args)
                pages.append((f.name, crops))
                print(f"  {f.name}: 말풍선 {len(crops)}개", flush=True)
            transcript = transcribe_batch(pages, args.model,
                                          fast=args.fast_transcribe)
            print("배치 전사 완료 — 재조판 시작", flush=True)

    results = []
    for i, f in enumerate(files, 1):
        if f.name in locked:
            print(f"[{i}/{len(files)}] {f.name} — 수동 확정 잠금, 건너뜀")
            results.append({"file": f.name, "status": "locked"})
            continue
        if not args.no_resume and (out / f"{f.stem}_final.png").exists():
            print(f"[{i}/{len(files)}] {f.name} — 완료본 있음, 건너뜀")
            results.append({"file": f.name, "status": "skipped"})
            continue
        print(f"[{i}/{len(files)}] {f.name}", flush=True)
        try:
            r = process_page(f, out, args, transcript)
        except Exception as e:
            r = {"file": f.name, "status": "error", "error": str(e),
                 "trace": traceback.format_exc()}
            results.append(r)
            if "credit balance" in str(e).lower():
                print("!! API 크레딧 소진 — 남은 페이지를 중단합니다. "
                      "충전 후 재실행하면 완료된 페이지는 건너뜁니다.",
                      file=sys.stderr)
                break
            print(f"    -> 오류: {e}", file=sys.stderr)
            continue
        results.append(r)
        if r.get("status") == "ok":
            print(f"    -> 말풍선 {r['bubbles']}개, 재조판 {r['retyped']}개")

    (out / "review.json").write_text(
        json.dumps(merge_review(out, results), ensure_ascii=False, indent=2),
        encoding="utf-8")
    ok = sum(1 for r in results if r.get("status") in ("ok", "crops_exported"))
    print(f"\n완료 {ok}/{len(results)}장. 리포트: {out / 'review.json'}")
    hp = write_review_html(out)
    if hp:
        print(f"검수 페이지: {hp} — 브라우저로 열어 재작업할 말풍선을 "
              "마킹하세요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
# EOF
