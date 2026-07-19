# -*- coding: utf-8 -*-
"""스캔 이북 한글 번역 도구 v0.4.0
================================

스캔된 책 페이지 이미지(폴더) 또는 PDF를 받아 한글 전자책(TXT + EPUB)으로
번역한다. 코믹스 파이프라인(comic_retype_pipeline)과 같은 폴더에 두고
번역 백엔드(Claude/Gemini/Ollama)·Tesseract 언어 데이터·API 키 설정을
재사용한다.

흐름:
  1. 소스 로드 — 이미지 폴더 / 스캔 PDF(렌더) / 텍스트 PDF(직접 추출)
  2. 페이지 전사 — Claude 비전(정확)·Gemini 비전(저가)·Tesseract(무료),
     페이지별 resume
  3. 병합 — 분철 하이픈·페이지 걸친 문장 이어붙임, 쪽번호·머리글 제거
  4. 번역 — 문단 청크 단위, 용어집 + 직전 문맥 전달, 청크별 resume
  5. 출력 — <제목>_ko.txt / <제목>_ko.epub (+ 원문 _src.txt 검수용)
  6. 편집 모드 — 브라우저에서 원문·번역 문단을 나란히 검토/수정, 스캔
     원본 이미지 대조, 문단 재번역, TXT/EPUB 재생성 (GUI [편집 페이지]
     버튼 또는 --edit). 원문 수정은 _work/book.json 에 영속된다.

전사 방식과 번역 엔진은 따로 고른다 — 가성비 조합: Gemini 비전 전사 +
Claude 번역. 실행이 끝나면(취소 포함) 전사/번역 파트별 API 토큰 사용량과
예상 요금을 로그에 표시한다 (코믹스 파이프라인 track_usage 재사용).

실행:
  python ebook_translate.py                 → GUI
  python ebook_translate.py 소스 [옵션...]  → CLI (--help 참고)
"""
from __future__ import annotations

__version__ = "0.13.7"  # 읽기 폰트(제목 포함)·연속 공백 유지·버전 표시

import argparse
import html
import json
import os
import re
import sys
import zipfile
from pathlib import Path
from typing import Optional

import comic_retype_pipeline as retype

APP_DIR = (Path(sys.executable).parent if getattr(sys, "frozen", False)
           else Path(__file__).parent)
COMIC_CONFIG = APP_DIR / "app_config.json"      # 코믹스 앱 설정 (키 공유)
CONFIG_PATH = APP_DIR / "ebook_config.json"
IMG_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}

LANG_NAMES = {"de": "독일어", "en": "영어", "ja": "일본어"}

# 언어 자동 감지용 불용어 (전사 표본에서 빈도 비교)
_DE_WORDS = {"der", "die", "das", "und", "nicht", "ist", "ein", "eine",
             "zu", "mit", "den", "von", "sich", "auf", "auch", "als",
             "aber", "wir", "nur", "wie", "noch", "nach", "bei", "aus",
             "wenn", "dann", "war", "ihm", "ihr", "sein", "hatte"}
_EN_WORDS = {"the", "and", "of", "to", "in", "is", "that", "it", "was",
             "for", "with", "as", "his", "her", "on", "at", "by", "not",
             "this", "but", "from", "they", "have", "had", "were", "been",
             "would", "there", "what", "when"}
CHUNK_CHARS = 3000          # 번역 요청당 원문 문자 수 상한
PDF_DPI = 220               # 스캔 PDF 렌더 해상도
MIN_TEXT_PDF_CHARS = 200    # 페이지당 이 이상 텍스트 있으면 텍스트 PDF로 간주

# Gemini (Google) — OpenAI 호환 엔드포인트, 저가 비전 대안.
# 키 발급: https://aistudio.google.com/apikey (GEMINI_API_KEY 환경변수 가능)
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
GEMINI_MODEL = "gemini-3.1-flash-lite"
GEMINI_TIMEOUT = 180

# DeepSeek (OpenAI 호환) — 전사용 초저가 대안. 키: platform.deepseek.com
# ※2026-07 현재 공식 API의 이미지 입력 지원이 막 열리는 중 — 거부되면
#   'DeepSeek URL'을 비전 지원 OpenAI 호환 서버(DeepInfra deepseek-ocr,
#   Qwen DashScope 등)로 바꾸고 그쪽 키·모델명을 쓰면 된다.
DEEPSEEK_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-v4-flash"


# ---------------------------------------------------------------------------
# 프롬프트
# ---------------------------------------------------------------------------
PROMPT_SCAN = """스캔된 책 한 페이지입니다. 본문 텍스트를 정확히 전사하세요.

- 본문만: 쪽번호, 머리글/꼬리글(장 제목·책 제목 반복), 워터마크는 제외
- 문단 구분은 빈 줄 한 줄로 표시 (문단 안의 줄바꿈은 공백으로 이어붙임)
- 줄 끝 분철 하이픈(예: "Ge-" + "fahr")은 한 단어로 이어붙임
- 장 제목/소제목은 그 줄만 단독 문단으로 하고 줄 앞에 "## " 를 붙여
  표시 (레이아웃상 제목으로 보이는 줄만 — 본문 문장에는 절대 금지)
- 페이지 마지막 문장이 끝나지 않았어도 보이는 데까지만 전사
- 본문이 없는 페이지(그림·백지)면 아무것도 출력하지 않음
- 본문은 요약·생략 없이 빠짐없이 전부 전사 (일부만 전사 금지)
- 세로쓰기 페이지(일본어 등)는 읽기 순서(오른쪽 단부터 아래로)대로
  가로쓰기 평문으로 전사, 루비(후리가나)는 제외

전사 텍스트만 출력 (설명·머리말 금지)."""

PROMPT_SPLIT_NOTE = """이미지 2장은 같은 페이지의 위/아래 절반입니다 \
(겹침 없음). 이어지는 하나의 페이지로 전사하세요.

"""

PROMPT_XLAT_BOOK = """책 본문 {lang} 문단 목록입니다 (이어지는 순서).

- 자연스러운 한국어 문학 번역체 — 직역투·번역투 지양
- 등장인물 말투(존댓말/반말, 호칭)는 문맥에 맞게 정하고 일관성 유지
- 문단 수와 순서 유지 — 문단을 합치거나 쪼개지 말 것
- "## " 로 시작하는 문단은 장 제목/소제목 — 번역문에도 "## " 접두사를
  그대로 유지
- 대화 인용부호는 곡선 따옴표 “ ” 로 통일 — JSON을 깨뜨리는 일반
  큰따옴표(")는 쓰지 말고, 부득이하면 반드시 \\" 로 이스케이프
- 번역할 수 없는 문단은 text에 null
{gloss}{ctx}
JSON 배열만 출력 (설명 금지):
[{{"id": 1, "text": "..."}}, ...]"""


# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------
def load_defaults() -> dict:
    """ebook_config.json + 코믹스 앱 설정(api_key·모델)에서 기본값."""
    cfg = {}
    try:
        if COMIC_CONFIG.exists():
            c = json.loads(COMIC_CONFIG.read_text(encoding="utf-8"))
            cfg["api_key"] = c.get("api_key", "")
            cfg["claude_model"] = c.get("claude_model", "claude-sonnet-4-5")
            cfg["ollama_model"] = c.get("ollama_model") or retype.OLLAMA_MODEL
    except Exception:
        pass
    try:
        if CONFIG_PATH.exists():
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except Exception:
        pass
    return cfg


class Cancelled(Exception):
    pass


# ---------------------------------------------------------------------------
# 1. 소스 로드
# ---------------------------------------------------------------------------
def _pdf_doc(path: Path):
    try:
        import fitz
    except ImportError:
        raise RuntimeError("PDF 입력에는 PyMuPDF가 필요합니다: "
                           "pip install pymupdf")
    return fitz.open(str(path))


def probe_source(src: Path) -> tuple[str, int]:
    """소스 종류와 페이지 수 — ("images"|"pdf-text"|"pdf-scan", n)."""
    if src.is_dir():
        files = [p for p in src.iterdir() if p.suffix.lower() in IMG_EXTS]
        if not files:
            raise RuntimeError(f"폴더에 이미지가 없습니다: {src}")
        return "images", len(files)
    if src.suffix.lower() != ".pdf":
        raise RuntimeError("소스는 이미지 폴더 또는 PDF 파일이어야 합니다")
    doc = _pdf_doc(src)
    try:
        n = doc.page_count
        # 앞쪽 몇 페이지에 텍스트 레이어가 충분하면 텍스트 PDF
        probe = range(min(8, n))
        texty = sum(1 for i in probe
                    if len(doc[i].get_text("text").strip())
                    >= MIN_TEXT_PDF_CHARS)
        return ("pdf-text" if texty >= max(1, len(probe) // 2)
                else "pdf-scan"), n
    finally:
        doc.close()


def list_source_pages(src: Path, kind: str) -> list:
    """페이지 식별자 목록 (정렬) — 이미지 경로 또는 PDF 페이지 번호."""
    if kind == "images":
        return sorted(p for p in src.iterdir()
                      if p.suffix.lower() in IMG_EXTS)
    doc = _pdf_doc(src)
    try:
        return list(range(doc.page_count))
    finally:
        doc.close()


def load_page_image(src: Path, kind: str, page) -> "object":
    """페이지를 BGR ndarray로 (전사용). 텍스트 PDF는 호출되지 않음."""
    import numpy as np
    if kind == "images":
        img = retype.imread_unicode(page)
        if img is None:
            raise RuntimeError(f"이미지를 읽을 수 없습니다: {page}")
        return img
    import fitz
    doc = _pdf_doc(src)
    try:
        pix = doc[page].get_pixmap(dpi=PDF_DPI)
        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
            pix.height, pix.width, pix.n)
        return arr[:, :, 2::-1].copy() if pix.n >= 3 \
            else np.repeat(arr, 3, axis=2)
    finally:
        doc.close()


def page_label(kind: str, page) -> str:
    return page.name if kind == "images" else f"p{page + 1:04d}"


# ---------------------------------------------------------------------------
# 2. 페이지 전사
# ---------------------------------------------------------------------------
def _prep_for_claude(img):
    """비전 요청용 축소 — 긴 변 1600px."""
    import cv2
    long_side = max(img.shape[:2])
    if long_side > 1600:
        s = 1600 / long_side
        img = cv2.resize(img, (int(img.shape[1] * s), int(img.shape[0] * s)),
                         interpolation=cv2.INTER_AREA)
    return img


def _page_halves(img) -> list:
    """세로로 긴 책 페이지를 위/아래 절반으로 분할 (비전 전사용).

    전사 이미지는 긴 변 1600px로 축소되는데, 전장 스캔은 본문 글씨가
    8px대까지 작아져 비전 모델이 뭉텅이로 건너뛴다. 절반씩 보내면
    유효 해상도가 2배. 글줄이 잘리지 않도록 중앙 40~60% 구간에서
    가장 밝은(글 없는) 행을 절단선으로 고른다."""
    import cv2
    import numpy as np
    h, w = img.shape[:2]
    if h < w * 1.25 or h <= 1800:      # 가로형·저해상도는 분할 불필요
        return [img]
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    b0, b1 = int(h * 0.40), int(h * 0.60)
    rows = g[b0:b1].astype(np.float32).mean(axis=1)
    cut = b0 + int(rows.argmax())
    return [img[:cut], img[cut:]]


def transcribe_page_claude(img, model: str) -> str:
    """Claude 비전으로 한 페이지 전사 — 문단은 빈 줄 구분 평문.

    세로로 긴 페이지는 상/하 분할해 한 요청에 이미지 2장으로 전송."""
    import anthropic
    client = anthropic.Anthropic()
    parts = _page_halves(img)
    prompt = (PROMPT_SCAN if len(parts) == 1
              else PROMPT_SPLIT_NOTE + PROMPT_SCAN)
    content = [retype._img_block(_prep_for_claude(p)) for p in parts]
    content.append({"type": "text", "text": prompt})
    msg = client.messages.create(
        model=model, max_tokens=6000, temperature=0.0,
        messages=[{"role": "user", "content": content}])
    u = getattr(msg, "usage", None)
    retype.track_usage("전사", model, getattr(u, "input_tokens", 0),
                       getattr(u, "output_tokens", 0))
    return retype._clean_ws(msg.content[0].text.strip())


def _call_oai(base_url: str, messages: list, model: str, key: str,
              max_tokens: int = 8000, part: str = "기타",
              extra: Optional[dict] = None, name: str = "API",
              key_hint: str = "") -> str:
    """OpenAI 호환 chat/completions 호출 (표준lib urllib만 사용) → 텍스트.

    Gemini·DeepSeek 등 공용. part(전사/번역)별 토큰 사용량을
    retype.track_usage 로 집계."""
    import urllib.request
    import urllib.error
    payload = {"model": model, "stream": False, "temperature": 0.0,
               "max_tokens": max_tokens, "messages": messages}
    if extra:
        payload.update(extra)
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=GEMINI_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            detail = ""
        hint = (f" — API 키 확인{key_hint}" if e.code in (401, 403) else "")
        raise RuntimeError(f"{name} 오류 {e.code}{hint}: {detail}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"{name} 연결 실패: {e}")
    uu = data.get("usage") or {}
    retype.track_usage(part, model, uu.get("prompt_tokens"),
                       uu.get("completion_tokens"))
    return ((data.get("choices") or [{}])[0].get("message", {})
            .get("content") or "")


def _call_gemini(messages: list, model: str, key: str,
                 max_tokens: int = 8000, part: str = "기타") -> str:
    return _call_oai(GEMINI_URL, messages, model, key, max_tokens, part,
                     name="Gemini API",
                     key_hint=" (https://aistudio.google.com/apikey)")


def _repair_json(raw: str) -> str:
    """문자열 안의 비이스케이프 큰따옴표·제어문자 보정.

    산문 번역엔 대사 인용부호가 많아 모델이 JSON 문자열 안에 "를
    그대로 넣는 사고가 잦다. 문자열을 따라가며 만난 "가 실제 종료인지
    (뒤에 , } ] : 또는 ,+다음 키/객체) 판단해, 아니면 \\" 로 바꾼다."""
    out: list[str] = []
    ins = esc = False
    n = len(raw)
    key_re = re.compile(r',\s*("(?:[^"\\]|\\.)*"\s*:|[{\[])')
    for i, c in enumerate(raw):
        if not ins:
            if c == '"':
                ins = True
            out.append(c)
            continue
        if esc:
            esc = False
            out.append(c)
        elif c == "\\":
            esc = True
            out.append(c)
        elif c == "\n":
            out.append("\\n")           # 문자열 안 제어문자
        elif c == "\t":
            out.append("\\t")
        elif c == '"':
            j = i + 1
            while j < n and raw[j] in " \t\r\n":
                j += 1
            if (j >= n or raw[j] in "}]:"
                    or (raw[j] == "," and key_re.match(raw, j))):
                ins = False             # 진짜 문자열 종료
                out.append(c)
            else:
                out.append('\\"')       # 내부 따옴표 → 이스케이프
        else:
            out.append(c)
    return "".join(out)


def _parse_loose(raw: str) -> list:
    """JSON 배열 응답 관대 파싱 — 사고 블록 제거 + 배열 부분 추출 +
    문자열 내 따옴표 보정(_repair_json) 폴백."""
    raw = re.sub(r"<think>.*?(</think>|$)", "", raw, flags=re.S).strip()
    try:
        return retype._parse_json_reply(raw)
    except Exception:
        s, e = raw.find("["), raw.rfind("]")
        seg = raw[s:e + 1] if (s != -1 and e > s) else raw
        try:
            return retype._parse_json_reply(seg)
        except Exception:
            return retype._parse_json_reply(_repair_json(seg))


def transcribe_page_gemini(img, model: str, key: str) -> str:
    """Gemini 비전으로 한 페이지 전사 — Claude 비전의 저가 대안.

    세로로 긴 페이지는 상/하 분할해 한 요청에 이미지 2장으로 전송."""
    import base64
    import cv2
    parts = _page_halves(img)
    prompt = (PROMPT_SCAN if len(parts) == 1
              else PROMPT_SPLIT_NOTE + PROMPT_SCAN)
    content = []
    for p in parts:
        ok, buf = cv2.imencode(".png", _prep_for_claude(p))
        b64 = base64.b64encode(buf.tobytes()).decode()
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b64}"}})
    content.append({"type": "text", "text": prompt})
    raw = _call_gemini([{"role": "user", "content": content}],
                       model, key, max_tokens=6000, part="전사")
    return retype._clean_ws(raw.strip())


def transcribe_page_deepseek(img, model: str, key: str, url: str) -> str:
    """DeepSeek(또는 임의 OpenAI 호환 서버) 비전 전사 — 초저가 대안.

    thinking 모드는 끔(전사에 불필요·토큰 절약). 이미지 입력이 400으로
    거부되면 비전 미지원 서버 — 즉시 중단하고 URL 교체를 안내한다."""
    import base64
    import cv2
    parts = _page_halves(img)
    prompt = (PROMPT_SCAN if len(parts) == 1
              else PROMPT_SPLIT_NOTE + PROMPT_SCAN)
    content = []
    for p in parts:
        ok, buf = cv2.imencode(".png", _prep_for_claude(p))
        b64 = base64.b64encode(buf.tobytes()).decode()
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b64}"}})
    content.append({"type": "text", "text": prompt})
    try:
        raw = _call_oai(url or DEEPSEEK_URL,
                        [{"role": "user", "content": content}],
                        model, key, max_tokens=6000, part="전사",
                        extra={"thinking": {"type": "disabled"}},
                        name="DeepSeek API",
                        key_hint=" (https://platform.deepseek.com)")
    except RuntimeError as e:
        if "오류 400" in str(e):
            raise RuntimeError(
                f"{e}\n→ 이미지 입력 거부(비전 미지원)일 수 있습니다 — "
                "'DeepSeek URL'을 비전 지원 OpenAI 호환 서버로 바꾸고 "
                "그쪽 모델명·키를 입력하세요.")
        raise
    return retype._clean_ws(_parse_think_strip(raw))


def _parse_think_strip(raw: str) -> str:
    """reasoning 모델이 <think> 블록을 섞어 보낼 때 제거."""
    return re.sub(r"<think>.*?(</think>|$)", "", raw or "",
                  flags=re.S).strip()


def transcribe_page_winocr(img, lang: str) -> str:
    """Windows 기본 OCR(Windows.Media.Ocr) 전사 — 무료·로컬.

    깨끗한 디지털 렌더 텍스트(이북 캡처)에 강함. 줄 단위 결과만 주므로
    줄 좌표(세로 간격·들여쓰기)로 문단을 재구성한다. Windows 전용,
    설정>언어에서 해당 언어팩 설치 필요."""
    try:
        import winocr
    except ImportError:
        raise RuntimeError(
            "Windows OCR에는 winocr 패키지가 필요합니다: pip install winocr "
            "(Windows 전용 — 설정>언어에서 원서 언어팩도 설치)")
    r = winocr.recognize_cv2_sync(img, lang)

    def g(o, k, d=None):
        return o.get(k, d) if isinstance(o, dict) else getattr(o, k, d)

    rows = []                       # (y0, y1, x0, 텍스트)
    for ln in (g(r, "lines") or []):
        t = (g(ln, "text") or "").strip()
        if not t:
            continue
        xs, y0s, y1s = [], [], []
        for w in (g(ln, "words") or []):
            br = g(w, "bounding_rect") or g(w, "boundingRect") or {}
            xs.append(float(g(br, "x", 0) or 0))
            y = float(g(br, "y", 0) or 0)
            y0s.append(y)
            y1s.append(y + float(g(br, "height", 0) or 0))
        rows.append((min(y0s) if y0s else 0.0, max(y1s) if y1s else 0.0,
                     min(xs) if xs else 0.0, t))
    if not rows:
        return ""
    rows.sort(key=lambda x: x[0])
    import statistics
    hs = [b - a for a, b, _, _ in rows if b > a]
    line_h = statistics.median(hs) if hs else 20.0
    gaps = [max(0.0, rows[i][0] - rows[i - 1][1])
            for i in range(1, len(rows))]
    med_gap = statistics.median(gaps) if gaps else 0.0
    x_base = sorted(x for _, _, x, _ in rows)[max(0, len(rows) // 10)]
    paras, cur = [], [rows[0][3]]
    for i in range(1, len(rows)):
        gap = rows[i][0] - rows[i - 1][1]
        indent = rows[i][2] - x_base > line_h * 1.2   # 첫 줄 들여쓰기
        if gap > max(med_gap * 1.8, line_h * 0.55) or indent:
            paras.append("\n".join(cur))
            cur = [rows[i][3]]
        else:
            cur.append(rows[i][3])
    paras.append("\n".join(cur))
    return "\n\n".join(paras)


def transcribe_page_tesseract(img, lang_code: str) -> str:
    """Tesseract로 한 페이지 전사 — par_num 기반 문단 구분.

    앱 폴더 tessdata\\ 자동 인식(retype._tess_config)과 동일 규칙.
    ★config에 따옴표 금지 (Windows pytesseract shlex posix=False)."""
    import shutil
    import cv2
    import pytesseract
    if not shutil.which("tesseract"):
        exe = retype._find_tesseract()
        if exe:
            pytesseract.pytesseract.tesseract_cmd = exe
        else:
            raise RuntimeError(retype._TESS_GUIDE)
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    if g.shape[1] < 1500:      # 저해상도 스캔만 확대
        g = cv2.resize(g, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    _, bw = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    cfg = ("--psm 4 " + retype._tess_config(lang_code)).strip()
    td = retype._tessdata_dir(lang_code)
    prev = os.environ.get("TESSDATA_PREFIX")
    if td:
        os.environ["TESSDATA_PREFIX"] = str(td)
    try:
        d = pytesseract.image_to_data(bw, lang=lang_code, config=cfg,
                                      output_type=pytesseract.Output.DICT)
    except pytesseract.TesseractError as e:
        if lang_code in str(e).lower() or "language" in str(e).lower():
            raise RuntimeError(
                f"Tesseract '{lang_code}' 언어 데이터가 없습니다 — "
                f"{lang_code}.traineddata 를 앱 폴더 tessdata\\ 에 "
                "넣으세요 (코믹스 앱과 공용).")
        raise
    finally:
        if td:
            if prev is None:
                os.environ.pop("TESSDATA_PREFIX", None)
            else:
                os.environ["TESSDATA_PREFIX"] = prev
    paras: dict = {}
    for i, w in enumerate(d["text"]):
        w = (w or "").strip()
        if not w:
            continue
        try:
            if float(d["conf"][i]) < 25:    # 노이즈 컷
                continue
        except (TypeError, ValueError):
            pass
        pk = (d["block_num"][i], d["par_num"][i])
        paras.setdefault(pk, {}).setdefault(d["line_num"][i], []).append(w)
    out = []
    for pk in sorted(paras):
        lines = [" ".join(ws) for _, ws in sorted(paras[pk].items())]
        out.append("\n".join(lines))
    return "\n\n".join(out)


# ---------------------------------------------------------------------------
# 3. 병합 — 문단 재구성
# ---------------------------------------------------------------------------
_PAGENUM_RE = re.compile(r"^[\s\-–—·.]*[0-9０-９]{1,4}[\s\-–—·.]*$")
_SENT_END = tuple('.!?"»«“”’:;' + '。」』？！…')
# CJK(한자·가나·전각) — 줄 병합 시 공백 없이 이어붙일 문자
_CJK_RE = re.compile(r"[　-ヿ㐀-鿿豈-﫿"
                     r"！-｠]")


def _no_space_join(a: str, b: str) -> bool:
    """일본어 등 띄어쓰기 없는 문자끼리는 공백 없이 병합."""
    return bool(a and b and _CJK_RE.match(a[-1]) and _CJK_RE.match(b[0]))


def _join_hyphen_lines(par: str) -> str:
    """문단 안 줄바꿈을 공백으로, 분철 하이픈은 이어붙임."""
    lines = [ln.strip() for ln in par.splitlines() if ln.strip()]
    out = ""
    for ln in lines:
        if not out:
            out = ln
        elif out.endswith("-") and not out.endswith(" -"):
            out = out[:-1] + ln            # Ge- + fahr → Gefahr
        elif _no_space_join(out, ln):
            out += ln                      # 일본어 — 공백 없이
        else:
            out += " " + ln
    return out.strip()


def merge_pages(page_texts: list[str], drop_heads: bool = True) -> list[str]:
    """페이지별 전사 → 책 전체 문단 목록 (페이지 추적 없는 간이형)."""
    return merge_pages_tagged(page_texts, None, drop_heads)[0]


def _repeated_heads(page_texts: list[str]) -> set:
    """여러 페이지에서 반복되는 짧은 첫 문단(머리글) 집합."""
    from collections import Counter
    heads: Counter = Counter()
    for t in page_texts:
        ps = [p for p in t.split("\n\n") if p.strip()]
        if ps:
            h = _join_hyphen_lines(ps[0])
            if len(h) <= 60:
                heads[h.lower()] += 1
    return {h for h, n in heads.items()
            if n >= max(3, len(page_texts) // 10)}


def _join_paras(a: str, b: str) -> str:
    """페이지·문단 경계 병합 (분철 하이픈·CJK 무공백 규칙 공통)."""
    a = a.rstrip()
    if a.endswith("-") and not a.endswith(" -"):
        return a[:-1] + b
    if _no_space_join(a, b):
        return a + b
    return a + " " + b


def _should_join(prev: str, nxt: str) -> bool:
    """앞 문단이 문장 종결 없이 끝났고 양쪽 다 제목이 아니면 병합."""
    return (not _is_heading(nxt) and not _is_heading(prev)
            and not prev.rstrip().endswith(_SENT_END))


def merge_pages_tagged(page_texts: list[str], labels: Optional[list[str]],
                       drop_heads: bool = True,
                       known_heads: Optional[set] = None
                       ) -> tuple[list[str], list[str], list[str]]:
    """페이지별 전사 → (문단 목록, 시작 페이지 라벨, 끝 페이지 라벨).

    쪽번호 제거, 자주 반복되는 짧은 줄(머리글) 제거, 페이지 경계에서
    끝나지 않은 문장은 다음 페이지 첫 문단과 병합. 병합으로 페이지에
    걸친 문단은 시작≠끝 라벨 (편집 모드가 '↪ 걸침' 표시에 사용).
    known_heads: 부분 재병합 시 책 전체 기준으로 계산한 머리글 집합."""
    if known_heads is not None:
        repeated = known_heads
    elif drop_heads:
        repeated = _repeated_heads(page_texts)
    else:
        repeated = set()

    if labels is None:
        labels = [""] * len(page_texts)
    paras: list[str] = []
    ppage: list[str] = []
    ppend: list[str] = []
    for t, lbl in zip(page_texts, labels):
        pending = [p for p in (x.strip() for x in t.split("\n\n")) if p]
        for j, p in enumerate(pending):
            p = _join_hyphen_lines(p)
            if not p or _PAGENUM_RE.match(p):
                continue
            if j == 0 and p.lower() in repeated:
                continue                    # 반복 머리글
            # 앞 문단이 문장 종결 없이 끝났으면 이어붙임 — OCR이 문단을
            # 잘게 쪼개거나(par_num) 페이지 경계에서 끊긴 경우 공통 처리.
            # 제목류(장 표제·전체 대문자)는 양쪽 모두 병합 금지.
            if paras and _should_join(paras[-1], p):
                paras[-1] = _join_paras(paras[-1], p)
                ppend[-1] = lbl                 # 걸침 — 끝 페이지 갱신
            else:
                paras.append(p)
                ppage.append(lbl)
                ppend.append(lbl)
    return paras, ppage, ppend


_CHAPTER_RE = re.compile(
    r"^(kapitel|chapter|teil|part|prolog|prologue|epilog|epilogue|buch|"
    r"book)\b[\s\d.:IVXLC-]*$", re.I)
_CHAPTER_JA_RE = re.compile(
    r"^(第[〇一二三四五六七八九十百千万0-9０-９]+[章話部巻節]"
    r"|序章|終章|序|プロローグ|エピローグ|まえがき|あとがき|目次)")


_HEAD_MARK = "## "      # 전사가 제목 줄에 붙이는 마커 (출력 시 제거)


def _strip_mark(s: str) -> str:
    s = (s or "").strip()
    return s[len(_HEAD_MARK):].lstrip() if s.startswith(_HEAD_MARK) else s


def _is_heading(s: str) -> bool:
    """제목 판정 (병합 금지 + 장 분할 기준).

    1순위: 전사 마커 "## " (비전 전사가 레이아웃 보고 표시 — 문장형
    소제목도 잡힘). 2순위: 장 표제 정규식·짧은 전대문자 휴리스틱
    (Tesseract·구버전 전사 폴백)."""
    s = s.strip()
    if s.startswith(_HEAD_MARK):
        return True
    if not s or len(s) > 60 or s.rstrip().endswith(_SENT_END):
        return False
    return (bool(_CHAPTER_RE.match(s)) or bool(_CHAPTER_JA_RE.match(s))
            or (len(s) <= 30 and s.isupper()))


def detect_chapters(paras: list[str]) -> list[tuple[str, int]]:
    """(제목, 시작 문단 인덱스) 목록 — 못 찾으면 단일 장."""
    marks = [(_strip_mark(p), i) for i, p in enumerate(paras)
             if _is_heading(p)]
    if not marks or marks[0][1] > len(paras) * 0.2:
        marks.insert(0, ("", 0))
    return marks


# ---------------------------------------------------------------------------
# 4. 번역
# ---------------------------------------------------------------------------
def _load_glossary(path: Optional[str], out_dir: Path) -> str:
    for c in ([Path(path)] if path else []) + [out_dir / "_glossary.txt"]:
        try:
            if c.exists():
                t = c.read_text(encoding="utf-8").strip()
                if t:
                    return ("\n용어집·표기 규칙 (반드시 따를 것):\n"
                            + t + "\n")
        except Exception:
            pass
    return ""


def translate_chunk(items: list[tuple[int, str]], cfg: dict, gloss: str,
                    ctx: str) -> dict[int, Optional[str]]:
    """문단 묶음 하나 번역 — {전역 문단 인덱스: 한국어 or None(실패)}."""
    listing = "\n\n".join(f"[{n}]\n{t}"
                          for n, (_, t) in enumerate(items, 1))
    ctx_txt = (f"\n직전 문맥 (참고용 — 번역하지 말 것):\n{ctx}\n"
               if ctx else "")
    prompt = PROMPT_XLAT_BOOK.format(
        lang=LANG_NAMES.get(cfg["source_lang"], cfg["source_lang"]),
        gloss=gloss, ctx=ctx_txt)
    body = f"{listing}\n\n{prompt}"
    if cfg["backend"] == "ollama":
        parsed = retype._call_ollama(
            {"ollama_model": cfg.get("ollama_model"),
             "ollama_url": cfg.get("ollama_url")}, body)
    elif cfg["backend"] == "gemini":
        parsed = _parse_loose(_call_gemini(
            [{"role": "user", "content": body}],
            cfg.get("gemini_model") or GEMINI_MODEL,
            cfg.get("gemini_key") or "", max_tokens=8000, part="번역"))
    else:
        # retype._call_claude는 엄격 json.loads라 대사 따옴표에 취약 —
        # 직접 호출해 _parse_loose(따옴표 보정 폴백)로 파싱한다.
        import anthropic
        client = anthropic.Anthropic()
        msg = client.messages.create(
            model=cfg["claude_model"], max_tokens=8000, temperature=0.0,
            messages=[{"role": "user",
                       "content": [{"type": "text", "text": body}]}])
        u = getattr(msg, "usage", None)
        retype.track_usage("번역", cfg["claude_model"],
                           getattr(u, "input_tokens", 0),
                           getattr(u, "output_tokens", 0))
        parsed = _parse_loose(msg.content[0].text)
    by_id = {int(it.get("id", 0)): it for it in parsed
             if isinstance(it, dict)}
    if len(by_id) < len(items) and len(parsed) == len(items):
        by_id = {n: it for n, it in enumerate(parsed, 1)
                 if isinstance(it, dict)}
    out = {}
    for n, (gi, _) in enumerate(items, 1):
        tv = ((by_id.get(n) or {}).get("text") or "").strip()
        out[gi] = retype._clean_ws(tv) if tv else None
    return out


# ---------------------------------------------------------------------------
# 5. 출력
# ---------------------------------------------------------------------------
def write_txt(path: Path, paras_ko: list[str]) -> None:
    path.write_text("\n\n".join(paras_ko) + "\n", encoding="utf-8")


# 인라인 서식 마커 — 편집칸에서 입력, EPUB에서 실제 서식으로.
#   ~~취소선~~  ++밑줄++  **굵게**  *기울임*   / 문단 내 줄바꿈은 <br/>
#   줄 맨 앞 '>> ' = 그 줄만 오른쪽 정렬 (예: 헌사·서명 — 줄마다 지정)
_INLINE = [(re.compile(r"\*\*(.+?)\*\*", re.S), "strong"),
           (re.compile(r"~~(.+?)~~", re.S), "s"),
           (re.compile(r"\+\+(.+?)\+\+", re.S), "u"),
           (re.compile(r"\*([^*\n]+?)\*"), "em")]


def _cover_jpg(cfg: dict, book: dict):
    """EPUB 표지 이미지 — book["cover"](없으면 첫 페이지) 스캔을 JPEG로.

    소스 접근이 필요하므로 PC에서 재생성할 때만 만들어진다. 실패하면
    None → 표지 없는 EPUB (기존과 동일)."""
    try:
        import cv2
        src = Path(cfg.get("src") or "")
        labels = (book or {}).get("page_labels") or []
        label = (book or {}).get("cover") or (labels[0] if labels else "")
        if not label or not src.exists():
            return None
        kind, _n = probe_source(src)
        if kind == "pdf-text":
            kind = "pdf-scan"                  # 표지는 렌더가 필요
        pg = (src / label) if kind == "images" else int(label[1:]) - 1
        img = load_page_image(src, kind, pg)
        h, w = img.shape[:2]
        if max(h, w) > 1600:
            sc = 1600 / max(h, w)
            img = cv2.resize(img, (int(w * sc), int(h * sc)),
                             interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 88])
        return buf.tobytes() if ok else None
    except Exception:
        return None


_ALIGN_RX = re.compile(r"^[ \t\u3000]*(?:&gt;&gt;|＞＞)\s+")


def _inline_html(s: str) -> str:
    """이스케이프 후 마커 → 태그, 개행 → <br/>, '>> ' 줄은 오른쪽 정렬."""
    out = html.escape(s or "")
    for rx, tag in _INLINE:
        out = rx.sub(rf"<{tag}>\1</{tag}>", out)
    out = re.sub(r" {2,}",                      # 연속 공백 유지 (&#160;)
                 lambda m: "&#160;" * (len(m.group()) - 1) + " ", out)
    parts, buf = [], []

    def _flush():
        if buf:
            parts.append("<br/>".join(buf))
            buf.clear()

    for ln in out.split("\n"):
        m = _ALIGN_RX.match(ln)                 # '>> '(앞 공백·전각 허용)
        if m:
            _flush()
            parts.append('<span style="display:block;text-align:right;'
                         'text-indent:0">'
                         + ln[m.end():].lstrip() + "</span>")
        elif not ln.strip():                    # 빈 줄 → 명시적 여백
            _flush()
            parts.append("<br/><br/>")
        else:
            buf.append(ln)
    _flush()
    return "".join(parts)


def _strip_inline(s: str) -> str:
    """마커 제거한 플레인 텍스트 (TXT 출력·목차 라벨용, 개행은 유지)."""
    out = re.sub(r"(?m)^[ \t\u3000]*(?:>>|＞＞)\s+", "", s or "")
    for rx, _tag in _INLINE:
        out = rx.sub(r"\1", out)
    return out


def _clean_para(s) -> str:
    """원문(src) 저장 정리 — 빈 줄은 1개 개행으로 축약 (_src.txt의 문단
    구분 "\n\n"과 충돌 방지), 비표준 공백 정리."""
    s = re.sub(r"[ \t]*\n[ \t]*\n+", "\n", str(s))
    return retype._clean_ws(s.strip())


def _clean_text(s) -> str:
    """번역(text) 저장 정리 — 빈 줄은 1개까지 유지 (문단 내 여백,
    번역은 "\n\n" 구분 파일 왕복이 없어 안전). 3연속 이상만 축약."""
    s = re.sub(r"[ \t]+\n", "\n", str(s))
    s = re.sub(r"\n{3,}", "\n\n", s)
    return retype._clean_ws(s.strip())


def _xhtml(title: str, paras: list[str]) -> str:
    body = "\n".join(
        (f"<h2>{_inline_html(p)}</h2>" if i == 0 and p and len(p) <= 60
         else f"<p>{_inline_html(p)}</p>")
        for i, p in enumerate(paras) if p)
    return ('<?xml version="1.0" encoding="utf-8"?>\n'
            '<!DOCTYPE html>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml" '
            'xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="ko">\n'
            f"<head><title>{html.escape(title)}</title>\n"
            "<style>p{margin:0 0 .1em 0;text-indent:1em;line-height:1.7}"
            "h2{margin:1.4em 0 .8em 0}</style></head>\n"
            f"<body>\n{body}\n</body>\n</html>\n")


def write_epub(path: Path, title: str, chapters: list[tuple[str, list[str]]],
               src_lang: str, cover: bytes = None) -> None:
    """최소 구조 EPUB3 생성 — mimetype은 첫 항목·무압축(규격)."""
    uid = "ebook-xlat-" + re.sub(r"\W+", "-", title.lower()).strip("-")
    manifest, spine, navli, files = [], [], [], []
    for i, (ct, paras) in enumerate(chapters, 1):
        fn = f"ch{i:03d}.xhtml"
        label = ct or f"{i}"
        manifest.append(f'<item id="c{i}" href="{fn}" '
                        'media-type="application/xhtml+xml"/>')
        spine.append(f'<itemref idref="c{i}"/>')
        navli.append(f'<li><a href="{fn}">{html.escape(_strip_inline(label))}</a></li>')
        files.append((fn, _xhtml(label if ct else title, paras)))
    cov_meta = cov_manifest = cov_spine = ""
    if cover:
        cov_meta = '<meta name="cover" content="cimg"/>\n'
        cov_manifest = ('<item id="cimg" href="cover.jpg" '
                        'media-type="image/jpeg" properties="cover-image"/>\n'
                        '<item id="cpg" href="cover.xhtml" '
                        'media-type="application/xhtml+xml"/>\n')
        cov_spine = '<itemref idref="cpg"/>\n'
        files.insert(0, ("cover.xhtml",
                         '<?xml version="1.0" encoding="utf-8"?>\n'
                         '<!DOCTYPE html>\n'
                         '<html xmlns="http://www.w3.org/1999/xhtml" '
                         'xml:lang="ko"><head><title>표지</title></head>\n'
                         '<body style="margin:0;text-align:center">'
                         '<img src="cover.jpg" alt="표지" '
                         'style="max-width:100%;height:auto"/>'
                         '</body></html>\n'))
    nav = ('<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n' 
           '<html xmlns="http://www.w3.org/1999/xhtml" '
           'xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="ko">\n'
           f"<head><title>{html.escape(title)}</title></head>\n"
           '<body><nav epub:type="toc"><h1>목차</h1><ol>\n'
           + "\n".join(navli) + "\n</ol></nav></body></html>\n")
    opf = ('<?xml version="1.0" encoding="utf-8"?>\n'
           '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
           'unique-identifier="uid">\n'
           '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
           f"<dc:identifier id=\"uid\">{uid}</dc:identifier>\n"
           f"<dc:title>{html.escape(title)}</dc:title>\n"
           "<dc:language>ko</dc:language>\n"
           f"<dc:source>{src_lang} 원서 스캔 개인 번역</dc:source>\n"
           "<meta property=\"dcterms:modified\">"
           "2026-01-01T00:00:00Z</meta>\n"
           + cov_meta +
           "</metadata>\n<manifest>\n"
           '<item id="nav" href="nav.xhtml" '
           'media-type="application/xhtml+xml" properties="nav"/>\n'
           + cov_manifest + "\n".join(manifest)
           + "\n</manifest>\n<spine>\n"
           + cov_spine + "\n".join(spine) + "\n</spine>\n</package>\n")
    container = ('<?xml version="1.0" encoding="utf-8"?>\n'
                 '<container version="1.0" xmlns="urn:oasis:names:tc:'
                 'opendocument:xmlns:container">\n<rootfiles>\n'
                 '<rootfile full-path="OEBPS/content.opf" '
                 'media-type="application/oebps-package+xml"/>\n'
                 "</rootfiles>\n</container>\n")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip",
                   compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", container,
                   compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/content.opf", opf,
                   compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/nav.xhtml", nav,
                   compress_type=zipfile.ZIP_DEFLATED)
        if cover:
            z.writestr("OEBPS/cover.jpg", cover,
                       compress_type=zipfile.ZIP_STORED)
        for fn, content in files:
            z.writestr(f"OEBPS/{fn}", content,
                       compress_type=zipfile.ZIP_DEFLATED)


def export_outputs(out: Path, title: str, src_lang: str,
                   paras: list[str], done: dict,
                   cover: bytes = None) -> dict:
    """번역본 TXT+EPUB 생성 (run_book·편집 모드 공용).

    done: {문단 인덱스: 번역 or None} — None/누락은 원문 그대로.
    "## " 제목 마커는 장 분할에 쓰고 출력 텍스트에선 제거한다."""
    paras_ko = [_strip_mark(done.get(i) or paras[i])
                for i in range(len(paras))]
    marks = detect_chapters(paras)
    chapters = []
    for k, (ct, start) in enumerate(marks):
        end = marks[k + 1][1] if k + 1 < len(marks) else len(paras)
        ct_ko = _strip_mark(done.get(start) or ct) if ct else ""
        chapters.append((ct_ko, paras_ko[start:end]))
    txt_path = out / f"{title}_ko.txt"
    epub_path = out / f"{title}_ko.epub"
    write_txt(txt_path, [_strip_inline(p) for p in paras_ko])
    write_epub(epub_path, title, chapters,
               LANG_NAMES.get(src_lang, src_lang), cover=cover)
    return {"txt": txt_path, "epub": epub_path, "chapters": len(chapters)}


# ---------------------------------------------------------------------------
# 작업 데이터 (_work/book.json + _work/xlat.json)
# ---------------------------------------------------------------------------
def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _atomic_json(path: Path, obj) -> None:
    """원자적 저장 — 편집 서버·워커가 동시에 읽어도 반쪽 JSON 안 봄."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def load_book(out: Path) -> Optional[dict]:
    """_work/book.json — {title, source_lang, page_labels,
    paras:[{src, page}]}. 없으면 구버전 출력(_src.txt)에서 복원 시도
    (페이지 라벨 없이 — 원본 이미지 대조만 불가)."""
    bp = out / "_work" / "book.json"
    book = _read_json(bp)
    if isinstance(book, dict) and book.get("paras"):
        return book
    for sp in sorted(out.glob("*_src.txt")):
        try:
            paras = [p.strip() for p in
                     sp.read_text(encoding="utf-8").split("\n\n")
                     if p.strip()]
        except OSError:
            continue
        if paras:
            book = {"title": sp.name[:-len("_src.txt")], "source_lang": "de",
                    "page_labels": [],
                    "paras": [{"src": p, "page": ""} for p in paras]}
            bp.parent.mkdir(parents=True, exist_ok=True)
            _atomic_json(bp, book)
            return book
    return None


def load_xlat(out: Path) -> dict[int, Optional[str]]:
    d = _read_json(out / "_work" / "xlat.json") or {}
    try:
        return {int(k): v for k, v in d.items()}
    except (TypeError, ValueError, AttributeError):
        return {}


# ---------------------------------------------------------------------------
# 전체 실행
# ---------------------------------------------------------------------------
def book_fingerprint(book: dict) -> int:
    """전체 원문(src) 결합의 crc32 — 모바일 수정분 반영 시 구조 일치 검증."""
    import zlib
    return zlib.crc32("\n".join(e.get("src") or ""
                                for e in book["paras"]).encode("utf-8"))


def apply_mobile_edits(out: Path, payload: dict, log) -> dict:
    """폰 수정 큐({v,fp,count,edits:{i:{src?,text?}}})를 fp 검증 후 반영.

    fp/count가 현재 book.json과 다르면 차단 — 그 사이 재전사·재실행으로
    문단 구조가 바뀐 경우 인덱스가 밀려 엉뚱한 문단을 덮어쓰는 사고 방지.
    통과하면 기존 _edit_save 경로(원자적 저장·src.txt 동기화) 재사용."""
    book = load_book(out)
    if not book:
        raise RuntimeError("편집 데이터(book.json)가 없습니다")
    ed = payload.get("edits") or {}
    if not ed:
        return {"src": 0, "text": 0}
    fp, cnt = book_fingerprint(book), len(book["paras"])
    if payload.get("fp") != fp or payload.get("count") != cnt:
        raise RuntimeError(
            "수정분이 다른 버전의 스냅샷 기준이라 반영을 차단했습니다 "
            f"(현재 {cnt}문단 fp={fp} / 수정분 {payload.get('count')}문단 "
            f"fp={payload.get('fp')}). [☁ 업로드]로 폰 페이지를 새로 연 뒤 "
            "다시 수정하세요")
    edits = []
    for k, v in ed.items():
        try:
            e = {"i": int(k)}
        except (TypeError, ValueError):
            continue
        if isinstance(v, dict):
            if "src" in v:
                e["src"] = v["src"]
            if "text" in v:
                e["text"] = v["text"]
        edits.append(e)
    return _edit_save(out, edits, log)


def resolve_out(cfg: dict) -> tuple[Path, str]:
    """출력 폴더와 책 제목 (run_book·편집 서버 공용)."""
    src = Path(cfg["src"])
    title = (cfg.get("title") or "").strip() or src.stem
    out = Path(cfg.get("out") or (src.parent if src.is_file() else src)
               / f"{title}_한글번역")
    return out, title


def _apply_keys(cfg: dict) -> None:
    if cfg.get("api_key"):
        os.environ["ANTHROPIC_API_KEY"] = cfg["api_key"].strip()
    cfg["gemini_key"] = (cfg.get("gemini_key")
                         or os.environ.get("GEMINI_API_KEY") or "").strip()
    cfg["deepseek_key"] = (cfg.get("deepseek_key")
                           or os.environ.get("DEEPSEEK_API_KEY") or "").strip()


def detect_lang(text: str) -> tuple[str, dict]:
    """전사 표본으로 독/영/일 판별 — (언어, 단서 점수 dict).

    가나(히라가나·가타카나)가 있으면 일본어 확정. 라틴 문자면 불용어
    빈도 비교 + 움라우트·ß 가중. 단서가 전혀 없으면 de (주 사용례)."""
    low = (text or "").lower()
    ja = len(re.findall(r"[ぁ-んァ-ヶー]", low))
    words = re.findall(r"[a-zäöüß]+", low)[:1000]
    de = sum(1 for w in words if w in _DE_WORDS)
    en = sum(1 for w in words if w in _EN_WORDS)
    de += 3 * min(30, len(re.findall(r"[äöüß]", low)))
    scores = {"ja": ja, "de": de, "en": en}
    if ja >= 5 and ja >= max(de, en) // 3:  # 가나 = 일본어 강한 신호
        return "ja", scores
    return ("de" if de >= en else "en"), scores


def _resolve_auto_lang(cfg: dict, src: Path, kind: str, pages: list,
                       work: Path, log) -> None:
    """source_lang=auto → 표본 확보 후 감지 결과로 확정.

    표본 우선순위: 텍스트 PDF 직접 추출 → 기존 전사 캐시 → 앞 페이지
    즉석 전사 (비전 전사는 캐시에 저장해 본 실행에서 재사용,
    Tesseract는 판별용 eng 임시 전사라 캐시 안 함)."""
    if cfg.get("source_lang") != "auto":
        return
    sample = ""
    if kind == "pdf-text":
        doc = _pdf_doc(src)
        try:
            for p in pages[:5]:
                sample += doc[p].get_text("text")
                if len(sample) >= 600:
                    break
        finally:
            doc.close()
    else:
        for p in pages:                      # 전사 캐시 우선 (resume)
            c = work / "pages" / f"{Path(page_label(kind, p)).stem}.txt"
            if c.exists():
                sample += c.read_text(encoding="utf-8")
                if len(sample) >= 600:
                    break
        if len(sample) < 200:
            ocr = cfg["ocr"]
            for p in pages[:4]:
                try:
                    img = load_page_image(src, kind, p)
                    if ocr == "claude":
                        t = transcribe_page_claude(img, cfg["claude_model"])
                    elif ocr == "gemini":
                        t = transcribe_page_gemini(
                            img, cfg.get("gemini_model") or GEMINI_MODEL,
                            cfg["gemini_key"])
                    elif ocr == "deepseek":
                        t = transcribe_page_deepseek(
                            img, cfg.get("deepseek_model") or DEEPSEEK_MODEL,
                            cfg.get("deepseek_key") or "",
                            cfg.get("deepseek_url") or DEEPSEEK_URL)
                    elif ocr == "winocr":
                        t = transcribe_page_winocr(img, "en")
                    else:
                        t = transcribe_page_tesseract(img, "eng")
                except Exception as e:
                    if _fatal_api_error(e):
                        raise
                    continue            # 표지·그림 페이지 등 — 다음 페이지로
                if ocr != "tesseract":
                    lbl = page_label(kind, p)
                    (work / "pages" / f"{Path(lbl).stem}.txt").write_text(
                        t, encoding="utf-8")
                sample += "\n" + t
                if len(sample.strip()) >= 300:
                    break
    lang, sc = detect_lang(sample)
    cfg["source_lang"] = lang
    log(f"언어 자동 감지: {LANG_NAMES[lang]} "
        f"(일 {sc['ja']} · 독 {sc['de']} · 영 {sc['en']})")
    if lang == "ja" and cfg.get("ocr") == "tesseract":
        log("!! 일본어 세로쓰기는 Tesseract 인식률이 낮습니다 — "
            "전사 방식을 Gemini/Claude 비전으로 권장")


def _needs_api_key(cfg: dict) -> bool:
    if cfg.get("kind") == "pdf-text":
        return cfg["backend"] == "claude"
    return cfg["ocr"] == "claude" or cfg["backend"] == "claude"


def _uses_gemini(cfg: dict) -> bool:
    if cfg.get("kind") == "pdf-text":
        return cfg["backend"] == "gemini"
    return cfg["ocr"] == "gemini" or cfg["backend"] == "gemini"


def _fatal_api_error(e: Exception) -> bool:
    """페이지 단위로 건너뛰면 안 되는 오류 — 키·크레딧·비전 미지원은
    즉시 중단 (전 페이지가 똑같이 실패할 문제)."""
    s = str(e).lower()
    return any(k in s for k in ("credit balance", "api key", "api 키",
                                "unauthorized", "401", "403",
                                "비전 미지원"))


def run_book(cfg: dict, log, is_cancelled) -> dict:
    """전체 파이프라인. cfg 키:
    src, out(비면 자동), title(비면 소스명), source_lang(de/en),
    ocr(claude/tesseract), backend(claude/ollama), claude_model,
    ollama_model, ollama_url, glossary, page_range, api_key."""
    src = Path(cfg["src"])
    kind, total = probe_source(src)
    cfg["kind"] = kind
    out, title = resolve_out(cfg)
    out.mkdir(parents=True, exist_ok=True)
    work = out / "_work"
    (work / "pages").mkdir(parents=True, exist_ok=True)

    _apply_keys(cfg)
    if _needs_api_key(cfg) and not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "ANTHROPIC_API_KEY 가 없습니다 — 키를 입력하거나 전사 방식 "
            "Tesseract + 번역 엔진 Ollama 조합(완전 로컬)을 쓰세요.")
    if _uses_gemini(cfg) and not cfg["gemini_key"]:
        raise RuntimeError(
            "Gemini API 키가 없습니다 — GUI의 'Gemini API 키'에 입력하거나 "
            "GEMINI_API_KEY 환경변수를 설정하세요 "
            "(https://aistudio.google.com/apikey 무료 발급).")
    if (cfg.get("ocr") == "deepseek" and kind != "pdf-text"
            and not cfg["deepseek_key"]):
        raise RuntimeError(
            "DeepSeek API 키가 없습니다 — 'DeepSeek API 키'에 입력하거나 "
            "DEEPSEEK_API_KEY 환경변수를 설정하세요 "
            "(https://platform.deepseek.com 발급).")

    kind_ko = {"images": "이미지 폴더", "pdf-text": "텍스트 PDF",
               "pdf-scan": "스캔 PDF"}[kind]
    log(f"소스: {kind_ko}, {total}페이지 — "
        f"{LANG_NAMES.get(cfg['source_lang'], '자동 감지')}"
        f" → 한글, 제목 '{title}'")

    pages = list_source_pages(src, kind)
    if (cfg.get("page_range") or "").strip():
        pages = retype.apply_page_range(pages, cfg["page_range"])
        log(f"페이지 범위 {cfg['page_range'].strip()} → {len(pages)}페이지")
    _resolve_auto_lang(cfg, src, kind, pages, work, log)

    # ---- 전사 (페이지별 resume: _work/pages/<라벨>.txt) ----
    lang_codes = {"de": "deu", "en": "eng", "ja": "jpn"}
    page_texts: list[str] = []
    if kind == "pdf-text":
        doc = _pdf_doc(src)
        try:
            for p in pages:
                page_texts.append(doc[p].get_text("text"))
        finally:
            doc.close()
        log("텍스트 레이어 추출 완료 (OCR 불필요)")
    else:
        ocr = cfg["ocr"]
        ocr_name = {"claude": "Claude 비전",
                    "gemini": f"Gemini 비전 "
                              f"({cfg.get('gemini_model') or GEMINI_MODEL})",
                    "deepseek": "DeepSeek 비전 ("
                                + (cfg.get("deepseek_model")
                                   or DEEPSEEK_MODEL) + ")",
                    "winocr": "Windows OCR",
                    "tesseract": "Tesseract"}[ocr]
        log(f"전사 시작 — {ocr_name}"
            f" ({len(pages)}페이지, 완료 페이지는 건너뜀)")
        for i, p in enumerate(pages, 1):
            if is_cancelled():
                raise Cancelled()
            lbl = page_label(kind, p)
            cache = work / "pages" / f"{Path(lbl).stem}.txt"
            if cache.exists():
                page_texts.append(cache.read_text(encoding="utf-8"))
                continue
            try:
                img = load_page_image(src, kind, p)
                if ocr == "claude":
                    t = transcribe_page_claude(img, cfg["claude_model"])
                elif ocr == "gemini":
                    t = transcribe_page_gemini(
                        img, cfg.get("gemini_model") or GEMINI_MODEL,
                        cfg["gemini_key"])
                elif ocr == "deepseek":
                    t = transcribe_page_deepseek(
                        img, cfg.get("deepseek_model") or DEEPSEEK_MODEL,
                        cfg["deepseek_key"],
                        cfg.get("deepseek_url") or DEEPSEEK_URL)
                elif ocr == "winocr":
                    t = transcribe_page_winocr(img, cfg["source_lang"])
                else:
                    t = transcribe_page_tesseract(
                        img, lang_codes[cfg["source_lang"]])
            except Exception as e:
                if _fatal_api_error(e):
                    raise
                log(f"  !! {lbl} 전사 실패({e}) — 빈 페이지로 계속")
                t = ""
            cache.write_text(t, encoding="utf-8")
            page_texts.append(t)
            if i % 10 == 0 or i == len(pages):
                log(f"  전사 {i}/{len(pages)}")

    # ---- 병합 (+ 편집 모드용 book.json — 원문 수정 영속) ----
    labels = [page_label(kind, p) for p in pages]
    bp = work / "book.json"
    book = _read_json(bp)
    if (isinstance(book, dict) and book.get("paras")
            and book.get("page_labels") == labels):
        paras = [e.get("src") or "" for e in book["paras"]]
        log(f"본문 이어받기: 기존 {len(paras)}문단 재사용 — 편집 모드 "
            "수정 반영 (재병합하려면 _work\\book.json 삭제)")
        book.update(title=title, source_lang=cfg["source_lang"])
    else:
        if isinstance(book, dict) and book.get("paras"):
            log("!! 페이지 구성이 달라져 본문을 새로 병합합니다 — "
                "기존 편집 내용은 무시됨")
        paras, ppage, ppend = merge_pages_tagged(page_texts, labels)
        book = {"title": title, "source_lang": cfg["source_lang"],
                "page_labels": labels,
                "paras": [dict({"src": s, "page": g},
                               **({"page_end": e} if e != g else {}))
                          for s, g, e in zip(paras, ppage, ppend)]}
    if not paras:
        raise RuntimeError("전사된 본문이 없습니다 — 전사 방식을 바꾸거나 "
                           "페이지 범위를 확인하세요")
    _atomic_json(bp, book)
    (out / f"{title}_src.txt").write_text("\n\n".join(paras) + "\n",
                                          encoding="utf-8")
    log(f"본문 재구성: {len(paras)}문단 (원문 저장: {title}_src.txt)")

    # ---- 번역 (청크별 resume: _work/xlat.json) ----
    gloss = _load_glossary(cfg.get("glossary"), out)
    log("용어집 적용" if gloss else
        "용어집 없음 — 출력폴더에 _glossary.txt 를 두면 인명 표기가 "
        "책 전체에서 일관됩니다")
    xp = work / "xlat.json"
    done = load_xlat(out)
    if done:
        n_ok = sum(1 for v in done.values() if v is not None)
        msg = f"이어하기: 기존 번역 {n_ok}문단 재사용"
        if len(done) > n_ok:
            msg += f" (실패 {len(done) - n_ok}문단은 다시 시도)"
        log(msg)
    be = {"ollama": "Ollama " + str(cfg.get("ollama_model")),
          "gemini": "Gemini " + str(cfg.get("gemini_model")
                                    or GEMINI_MODEL)}.get(
        cfg["backend"], cfg["claude_model"])
    log(f"번역 시작 — {be}, {len(paras)}문단")
    chunk: list = []
    size = 0
    prev_ctx = ""
    n_fail = 0

    def flush():
        nonlocal chunk, size, prev_ctx, n_fail
        if not chunk:
            return
        if is_cancelled():
            raise Cancelled()
        try:
            res = translate_chunk(chunk, cfg, gloss, prev_ctx)
        except Exception as e:
            if _fatal_api_error(e):
                raise
            log(f"  !! 청크 번역 실패({e}) — 원문 유지")
            res = {gi: None for gi, _ in chunk}
        for gi, t in res.items():
            if t is None:
                n_fail += 1
            done[gi] = t
        tail = [(gi, t) for gi, t in sorted(res.items()) if t]
        if tail:
            gi, t = tail[-1]
            prev_ctx = f"[원문] {paras[gi][-300:]}\n[번역] {t[-300:]}"
        _atomic_json(xp, done)
        log(f"  번역 {sum(1 for v in done.values() if v is not None)}"
            f"/{len(paras)}문단")
        chunk, size = [], 0

    for gi, p in enumerate(paras):
        if done.get(gi) is not None:
            continue                    # 실패(None)했던 문단은 재시도
        chunk.append((gi, p))
        size += len(p)
        if size >= CHUNK_CHARS:
            flush()
    flush()
    if n_fail:
        log(f"  !! {n_fail}문단 번역 실패 — 원문 그대로 출력됩니다")

    # ---- 출력 ----
    r = export_outputs(out, title, cfg["source_lang"], paras, done,
                       cover=_cover_jpg(cfg, load_book(out)))
    log(f"완료: {r['epub'].name} / {r['txt'].name} "
        f"({r['chapters']}개 장, {len(paras)}문단)")
    log("검토·수정: GUI [편집 페이지] 버튼 또는 --edit 옵션")
    usage = retype.usage_summary()          # 전사/번역 파트별 예상 요금
    if usage:
        log(usage)
    return {"epub": str(r["epub"]), "txt": str(r["txt"]),
            "paras": len(paras), "chapters": r["chapters"],
            "failed": n_fail}


# ---------------------------------------------------------------------------
# 편집 모드 — 브라우저에서 원문·번역 검토/수정 (코믹스 검수 페이지 패턴)
# ---------------------------------------------------------------------------
EDIT_HTML = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__ — 이북 편집</title>
<style>
:root{--bd:#ddd;--src:#b45309;--fail:#dc2626;--acc:#2563eb}
body{font-family:'Malgun Gothic',sans-serif;margin:0;background:#f5f5f4;
 color:#222}
header{position:sticky;top:0;z-index:9;background:#fff;
 border-bottom:1px solid var(--bd);padding:8px 12px;display:flex;gap:10px;
 align-items:center;flex-wrap:wrap}
header b{font-size:15px}
button{cursor:pointer;padding:4px 10px;border:1px solid #999;
 border-radius:6px;background:#fff;font-size:12px}
button:disabled{opacity:.45;cursor:default}
#save{background:var(--acc);color:#fff;border-color:var(--acc)}
select{padding:3px 6px;border-radius:6px}
#wrap{display:flex;align-items:flex-start}
main{flex:1;min-width:0;padding:10px 12px 60vh}
aside{display:none;width:42%;max-width:640px;border-left:1px solid var(--bd);
 background:#fff;position:sticky;top:46px;height:calc(100vh - 46px);
 overflow:auto}
aside.on{display:block}
aside img{width:100%;display:block}
#pv{display:none;padding:20px 26px;font-family:Batang,'Noto Serif KR',
 Georgia,serif;font-size:15px;color:#1c1c1c;background:#fff}
#pv p{margin:0 0 .1em;text-indent:1em;line-height:1.75}
#pv h2{margin:1.2em 0 .8em;font-size:19px}
#pv .empty{color:#999;font-size:13px;text-indent:0}
#pv .untr{color:#8a6d3b}
aside .cap{padding:6px 10px;color:#666;font-size:12px;display:flex;
 justify-content:space-between;align-items:center}
.pgh{margin:18px 4px 6px;font-size:13px;color:#555;font-weight:bold;
 display:flex;gap:8px;align-items:center}
.row{background:#fff;border:1px solid var(--bd);border-radius:8px;
 padding:8px 10px;margin:6px 0;content-visibility:auto;
 contain-intrinsic-size:130px}
.row.dirty{border-color:var(--acc);box-shadow:0 0 0 1px var(--acc)}
.row.fail{border-left:4px solid var(--fail)}
.rh{display:flex;gap:8px;align-items:center;font-size:12px;color:#888;
 margin-bottom:4px}
.badge{color:var(--fail);font-size:11px}
textarea{width:100%;box-sizing:border-box;border:1px solid var(--bd);
 border-radius:6px;padding:6px;font:13px/1.55 'Malgun Gothic',sans-serif;
 resize:vertical;margin-bottom:4px}
textarea.src{color:var(--src);background:#fffbf3}
#st{color:#666;font-size:12px}
@media (pointer:coarse){textarea{font-size:16px}
 button{min-height:36px}header{gap:8px}}
body.nosrc textarea.src{display:none}
body.nosrc .row{padding-top:6px}
#help{display:none;position:fixed;top:0;left:0;right:0;bottom:0;
 z-index:50;background:#fff;overflow:auto;padding:16px 16px 40px}
#help.on{display:block}
#help h3{margin:4px 0 12px}
#help table{border-collapse:collapse;width:100%;font-size:14px}
#help td{border:1px solid var(--bd);padding:7px 9px;vertical-align:top;
 line-height:1.55}
#help td:first-child{white-space:nowrap;color:var(--src);
 font-family:Consolas,monospace}
#help .hnote{color:#666;font-size:12px;margin:12px 0 0;line-height:1.8}
#help .hclose{margin:14px 0 0;padding:9px 22px}
#bmenu{display:none;position:fixed;z-index:40;background:#fff;
 border:1px solid var(--bd);border-radius:10px;padding:8px;
 box-shadow:0 4px 16px rgba(0,0,0,.18)}
#bmenu.on{display:block}
#bmenu div{color:#888;font-size:12px;margin:0 2px 6px}
#bmenu button{display:block;width:100%;text-align:left;margin:4px 0;
 min-height:36px}
.rd{display:none;font-family:Batang,'Noto Serif KR',Georgia,serif;
 font-size:16px;line-height:1.8;color:#1c1c1c;padding:1px 4px;
 cursor:pointer;text-indent:1em}
.rd.untr{color:#8a6d3b}
.rd.rdh{font-weight:700;font-size:1.15em;text-indent:0;margin:8px 0 4px}
body.rdmode .row{border:0;background:none;padding:1px 6px;margin:0;
 box-shadow:none;contain-intrinsic-size:60px}
body.rdmode .row .rh,body.rdmode .row textarea{display:none}
body.rdmode .rd{display:block}
body.rdmode .pgh{opacity:.45;font-size:11px;margin:10px 4px 2px}
body.rdmode .pgh button{display:none}
@media (max-width:640px){
 main{padding:6px 6px 60vh}
 .row{padding:7px 8px;margin:5px 0;border-radius:10px;
  contain-intrinsic-size:110px}
 .rh{margin-bottom:2px;flex-wrap:wrap}
 header{padding:6px 6px;gap:4px}
 header::before{content:"";flex-basis:100%;order:1;height:0}
 header select,header button{order:2;padding:4px 6px;font-size:12px;
  min-height:34px}
 #hpv{display:none!important}
 header b{font-size:14px;max-width:44vw;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
 #cnt{font-size:11px;flex-grow:1}
 #st{flex-basis:100%;order:9}
 .pgh{margin:14px 2px 4px}
 textarea{margin-bottom:3px}
 aside{position:fixed;top:0;left:0;right:0;bottom:0;width:auto;
  max-width:none;height:100%;z-index:30;border-left:0}
 aside .cap{position:sticky;top:0;background:#fff;
  border-bottom:1px solid var(--bd);z-index:1}}
</style></head><body>
<header>
 <b>__TITLE__</b><span id="cnt"></span>
 <select id="flt"><option value="all">전체</option>
 <option value="fail">번역 실패만</option>
 <option value="dirty">수정됨만</option>
 <option value="help">❓ 기호 도움말</option></select>
 <select id="ocrsel" title="🔄 재전사에 사용할 전사 방식"></select>
 <button id="save" disabled>💾 저장</button>
 <button id="mexp" style="display:none"
  title="수정분 JSON 내보내기 — 오프라인 폴백, PC 웹앱 [📥 파일 반영]으로 적용">📤</button>
 <button id="hpv" style="display:none"
  title="지금 보이는 페이지의 출력(TXT/EPUB) 미리보기">👁</button>
 <button id="bmk" style="display:none"
  title="현재 위치를 북마크로 저장 — 다음에 열 때 이 문단에서 이어서">🔖</button>
 <button id="mode"
  title="읽기 모드 ↔ 편집 모드 — 읽기 모드에서 문단을 탭하면 편집">📖 읽기</button>
 <button id="fsm" style="display:none" title="글자 작게">A−</button>
 <button id="fsp" style="display:none" title="글자 크게">A+</button>
 <button id="exp">📖 TXT/EPUB 재생성</button>
 <span id="st"></span>
</header>
<div id="wrap"><main id="list"></main>
<aside id="ip"><div class="cap">
<span><button onclick="ipNav(-1)" id="ipprev">◀</button>
<button onclick="ipNav(1)" id="ipnext">▶</button></span>
<span id="ipl"></span>
<button onclick="ipOff()">닫기</button></div><img id="pimg" alt="">
<div id="pv"></div></aside>
</div>
<div id="help">
 <h3>❓ 본문 편집 기호 <small style="color:#999">v__VER__</small></h3>
 <table>
  <tr><td>## </td><td>문단 맨 앞 — 제목/소제목 (EPUB 장 분할·목차,
   [§ 제목] 버튼과 동일)</td></tr>
  <tr><td>&gt;&gt; </td><td>줄 맨 앞 — 그 줄만 오른쪽 정렬 (헌사·서명·날짜.
   전각 ＞＞·앞 공백 인식, 뒤 공백 필수)</td></tr>
  <tr><td>**굵게**</td><td><strong>굵게</strong></td></tr>
  <tr><td>*기울임*</td><td><em>기울임</em></td></tr>
  <tr><td>~~취소선~~</td><td><s>취소선</s></td></tr>
  <tr><td>++밑줄++</td><td><u>밑줄</u></td></tr>
  <tr><td>엔터</td><td>문단 안 줄바꿈 유지</td></tr>
  <tr><td>빈 줄</td><td>번역 칸만 세로 여백으로 유지 (원문 칸은 자동
   정리 — 문단 구분과 충돌 방지)</td></tr>
  <tr><td>(비우기)</td><td>번역 칸을 비우면 그 문단은 원문 그대로 출력
   — 페이지 단위는 [↩ 원문] 버튼</td></tr>
 </table>
 <div class="hnote">· 기호는 쌍이어야 변환 — 홀수 개는 문자 그대로<br>
  · TXT 출력·목차에는 기호 제거(플레인)<br>
  · 👁 미리보기·📖 읽기 모드에서 실제 서식 확인<br>
  · 출력 반영은 [📖 TXT/EPUB 재생성] 시 · 상세: docs/편집기호_참조.md</div>
 <button class="hclose" onclick="document.getElementById('help')
  .classList.remove('on')">닫기</button>
</div>
<script>
const D=__DATA__;
const GURL='__GASURL__',GKEY='__GASKEY__';   // GAS 서빙 시 주입됨
const CLOUD=GURL.indexOf('__')!==0;
const SRV=!CLOUD&&(location.protocol==='http:'||location.protocol==='https:');
const dirty=new Map();          // i -> {src?, text?}
const rows=[];
const list=document.getElementById('list');
const st=document.getElementById('st');
let lastPg=null;
D.paras.forEach((e,i)=>{
 if(e.page!==lastPg){lastPg=e.page;
  const h=document.createElement('div');h.className='pgh';
  h.textContent=e.page||'(페이지 정보 없음)';
  const pvb=document.createElement('button');pvb.textContent='👁 미리보기';
  pvb.title='이 페이지 문단들이 최종 TXT/EPUB에 어떻게 나오는지 표시 '
   +'(지금 편집 중인 내용 기준 — 다시 누르면 갱신)';
  const pvpg=e.page;
  pvb.onclick=()=>showPv(pvpg);h.appendChild(pvb);
  if(e.page&&SRV){const b=document.createElement('button');
   b.textContent='📄 원본 보기';b.onclick=()=>showImg(e.page);
   h.appendChild(b);
   const rs=document.createElement('button');rs.textContent='🔄 재전사';
   rs.title='이 페이지만 다시 전사 — 해당 문단들의 원문·번역이 새로 '
    +'만들어지고 다른 페이지 번역은 유지됩니다';
   rs.onclick=()=>rescan(e.page,rs);h.appendChild(rs);
   const cvb=document.createElement('button');cvb.textContent='🖼 표지';
   cvb.title='이 페이지 스캔을 EPUB 표지 이미지로 (재생성 시 반영)';
   if(D.cover===pvpg){cvb.style.background='var(--acc)';
    cvb.style.color='#fff';cvb.style.borderColor='var(--acc)';}
   cvb.onclick=async()=>{try{await api('/api/cover',{page:pvpg});
    sessionStorage.setItem('edScroll',String(scrollY));location.reload();}
    catch(er){st.textContent='표지 설정 실패: '+er.message;}};
   h.appendChild(cvb);}
  if(e.page){const rvb=document.createElement('button');
   rvb.textContent='↩ 원문';
   rvb.title='이 페이지 문단들의 번역을 비워 원문 그대로 출력되게 합니다 '
    +'(문단 하나만은 번역칸을 직접 비우면 됨)';
   rvb.onclick=()=>{if(!confirm('『'+pvpg+'』 페이지의 번역을 모두 비우고 '
     +'원문 그대로 출력할까요?'))return;
    D.paras.forEach((x,j)=>{if(x.page!==pvpg||!rows[j])return;
     if(rows[j].tk.value){rows[j].tk.value='';mark(j,'text','');}});};
   h.appendChild(rvb);}
  list.appendChild(h);}
 const t=D.xlat[String(i)];
 const r=document.createElement('div');r.className='row';
 if(t==null)r.classList.add('fail');
 const rh=document.createElement('div');rh.className='rh';
 const sp=document.createElement('span');sp.textContent='#'+(i+1);
 rh.appendChild(sp);
 if(t==null){const bd=document.createElement('span');bd.className='badge';
  bd.textContent='번역 실패 — 원문 그대로 출력됨';rh.appendChild(bd);}
 if(e.page_end&&e.page_end!==e.page){
  const sb=document.createElement('button');
  sb.textContent='↪ '+e.page_end+' 에 걸침';
  sb.title='페이지 경계에서 병합된 문단 — 뒷부분은 이 페이지에 있습니다';
  if(SRV)sb.onclick=()=>showImg(e.page_end);
  rh.appendChild(sb);}
 if(SRV){const rb=document.createElement('button');rb.textContent='🌐 재번역';
  rb.title='원문을 고쳤으면 이 버튼으로 해당 문단만 다시 번역합니다';
  rb.onclick=()=>retx(i,rb);rh.appendChild(rb);}
 const ts=document.createElement('textarea');ts.className='src';
 ts.value=e.src;ts.title='원문 (전사 오류 수정 가능)';
 const hb=document.createElement('button');hb.textContent='§ 제목';
 hb.title='제목/소제목 지정 토글 — 켜면 EPUB 장 분할·목차 기준이 되고 '
  +'앞뒤 문단과 병합되지 않습니다 (원문 앞 ## 마커로 저장, '
  +'출력에선 마커 제거)';
 function updHb(){const on=ts.value.startsWith('## ');
  hb.style.background=on?'var(--acc)':'';
  hb.style.color=on?'#fff':'';
  hb.style.borderColor=on?'var(--acc)':'';}
 hb.onclick=function(){
  ts.value=ts.value.startsWith('## ')?ts.value.slice(3):'## '+ts.value;
  mark(i,'src',ts.value);updHb();};
 ts.addEventListener('input',updHb);updHb();
 rh.appendChild(hb);
 const tk=document.createElement('textarea');tk.value=t||'';
 tk.placeholder='(비우면 원문이 그대로 출력됩니다)';
 [ts,tk].forEach(x=>{x.rows=Math.min(14,Math.max(2,
  Math.ceil((x.value.length||60)/60)));});
 ts.addEventListener('input',()=>mark(i,'src',ts.value));
 tk.addEventListener('input',()=>mark(i,'text',tk.value));
 const rd=document.createElement('div');rd.className='rd';
 rd.title='탭하면 이 문단을 편집합니다';
 rd.onclick=()=>{setMode(false);r.scrollIntoView();tk.focus();};
 r.append(rh,ts,tk,rd);list.appendChild(r);
 rows[i]={r,ts,tk,rd};
});
function mark(i,k,v){const d=dirty.get(i)||{};d[k]=v;dirty.set(i,d);
 rows[i].r.classList.add('dirty');upd();if(CLOUD)cloudQueue();
 rdDirty.add(i);}
function curIdx(){const hh=document.querySelector('header').offsetHeight+4;
 for(let i=0;i<rows.length;i++){
  const rc=rows[i].r.getBoundingClientRect();
  if(rc.bottom>hh&&(rc.bottom||rc.top))return i;}
 return 0;}
const rdDirty=new Set();let rdFilled=false;
const NARROW=matchMedia('(max-width:640px)').matches;
function rdFill(i){const o=rows[i];if(!o||!o.rd)return;
 const ko=(o.tk.value||'').trim();
 o.rd.innerHTML=fmtHtml(stripMark(ko||o.ts.value));
 o.rd.classList.toggle('untr',!ko);
 o.rd.classList.toggle('rdh',o.ts.value.indexOf('## ')===0);}
function setMode(on){
 const ci=curIdx();
 document.body.classList.toggle('rdmode',on);
 document.getElementById('mode').textContent=
  on?(NARROW?'✏':'✏ 편집'):(NARROW?'📖':'📖 읽기');
 if(on){if(!rdFilled){rows.forEach((o,i)=>rdFill(i));rdFilled=true;}
  else{rdDirty.forEach(i=>rdFill(i));rdDirty.clear();}}
 try{localStorage.setItem('edMode',on?'1':'')}catch(e){}
 const o=rows[ci];if(o)o.r.scrollIntoView();}
document.getElementById('mode').onclick=()=>
 setMode(!document.body.classList.contains('rdmode'));
if(NARROW)document.getElementById('mode').textContent='📖';
if(localStorage.getItem('edMode')==='1')setMode(true);
const BKEY='ebookBmk:'+(D.title||'');
function initBmk(cloud){
 const bk=document.getElementById('bmk');bk.style.display='';
 let cur=null;
 const mnu=document.createElement('div');mnu.id='bmenu';
 const info=document.createElement('div');
 const b1=document.createElement('button');b1.textContent='📍 현재 위치 저장';
 const b2=document.createElement('button');b2.textContent='➡ 북마크로 이동';
 const b3=document.createElement('button');b3.textContent='🗑 북마크 삭제';
 mnu.append(info,b1,b2,b3);document.body.appendChild(mnu);
 const close=()=>mnu.classList.remove('on');
 bk.onclick=()=>{
  if(mnu.classList.contains('on')){close();return}
  info.textContent=cur!=null?'저장된 북마크: #'+(cur+1)
   :'저장된 북마크 없음';
  b2.disabled=b3.disabled=cur==null;
  mnu.style.top=(bk.getBoundingClientRect().bottom+6)+'px';
  mnu.style.right='8px';
  mnu.classList.add('on');};
 document.addEventListener('click',e=>{
  if(!mnu.contains(e.target)&&e.target!==bk)close();},true);
 const go=j=>{j=parseInt(j,10);
  if(isNaN(j)||j<0)return false;
  j=Math.min(j,rows.length-1);
  const o=rows[j];if(!o)return false;
  o.r.scrollIntoView();
  o.r.style.boxShadow='0 0 0 2px var(--acc)';
  setTimeout(()=>{o.r.style.boxShadow=''},1600);
  st.textContent='🔖 #'+(j+1)+'에서 이어서';return true;};
 b1.onclick=async()=>{const i=curIdx();cur=i;close();
  try{localStorage.setItem(BKEY,String(i))}catch(e){}
  st.textContent='🔖 #'+(i+1)+' 위치 저장됨';
  if(cloud){try{await gasCall({op:'state',pos:i});
    st.textContent='🔖 #'+(i+1)+' 위치 저장됨 ☁';}catch(e){}}};
 b2.onclick=()=>{close();if(cur!=null)go(cur);};
 b3.onclick=async()=>{cur=null;close();
  try{localStorage.removeItem(BKEY)}catch(e){}
  st.textContent='🔖 북마크 삭제됨';
  if(cloud){try{await gasCall({op:'state',pos:null});
    st.textContent='🔖 북마크 삭제됨 ☁ (서재 진행률도 초기화)';}
   catch(e){}}};
 const li=localStorage.getItem(BKEY);
 if(cloud)gasCall({op:'get'}).then(q=>{
   const sp=q&&q.state&&q.state.pos;
   if(sp!=null){if(go(sp))cur=parseInt(sp,10);}
   else if(li!=null&&go(li))cur=parseInt(li,10);}).catch(()=>{
   if(li!=null&&go(li))cur=parseInt(li,10);});
 else if(li!=null&&go(li))cur=parseInt(li,10);}
if(matchMedia('(pointer:coarse)').matches){
 const grow=t=>{t.style.height='auto';
  const cap=Math.max(160,innerHeight*0.5),h=t.scrollHeight+2;
  if(h>cap){t.style.height=cap+'px';t.style.overflowY='auto';}
  else{t.style.height=h+'px';t.style.overflowY='hidden';}};
 list.addEventListener('focusin',e=>{
  if(e.target.tagName==='TEXTAREA')grow(e.target);});
 list.addEventListener('input',e=>{
  if(e.target.tagName==='TEXTAREA')grow(e.target);});
 addEventListener('resize',()=>{   // 키보드 개폐 → 포커스 칸 재계산
  const a=document.activeElement;
  if(a&&a.tagName==='TEXTAREA'&&list.contains(a))grow(a);});}
function upd(){
 document.getElementById('save').disabled=!dirty.size||(!SRV&&!CLOUD);
 const nf=rows.filter(o=>o.r.classList.contains('fail')).length;
 document.getElementById('cnt').textContent=
  ' '+D.paras.length+'문단 · 실패 '+nf+' · 수정 '+dirty.size
  +(window._stale?' · ⚠ PC 새 버전 — 새로 여세요':'');}
async function api(p,body){
 const r=await fetch(p,{method:'POST',
  headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
 if(!r.ok){let m='HTTP '+r.status;
  if(r.status===409)m='작업 실행 중 — 완료 후 다시 시도하세요';
  else try{m=(await r.json()).error||m}catch(_){}
  throw new Error(m);}
 return r.json();}
async function doSave(){if(!dirty.size)return true;
 if(CLOUD){clearTimeout(cq);await cloudSend();return !dirty.size;}
 const edits=[...dirty.entries()].map(([i,d])=>({i:+i,...d}));
 st.textContent='저장 중…';
 try{await api('/api/save',{edits});
  edits.forEach(e=>{const o=rows[e.i];o.r.classList.remove('dirty');
   if('text'in e)o.r.classList.toggle('fail',!e.text.trim());});
  dirty.clear();upd();st.textContent='저장 완료 — 재생성해야 TXT/EPUB에 반영';
  return true;}
 catch(e){st.textContent='저장 실패: '+e.message;return false;}}
async function retx(i,btn){btn.disabled=true;
 st.textContent='#'+(i+1)+' 재번역 중…';
 try{if(!await doSave())return;
  const r=await api('/api/xlat',{ids:[i]});
  const t=r[String(i)];
  if(t){rows[i].tk.value=t;rows[i].r.classList.remove('fail');
   D.xlat[String(i)]=t;st.textContent='#'+(i+1)+' 재번역 완료';}
  else st.textContent='#'+(i+1)+' 재번역 실패 — 기존 번역 유지';
  upd();}
 catch(e){st.textContent='재번역 오류: '+e.message;}
 finally{btn.disabled=false;}}
document.getElementById('save').onclick=doSave;
document.getElementById('exp').onclick=async()=>{
 if(!SRV)return;
 try{if(!await doSave())return;st.textContent='재생성 중…';
  const r=await api('/api/export',{});
  st.textContent='재생성 완료: '+r.files.join(' / ');}
 catch(e){st.textContent='재생성 실패: '+e.message;}};
document.getElementById('flt').onchange=function(){
 const m=this.value;
 if(m==='help'){this.value=window._pflt||'all';
  document.getElementById('help').classList.add('on');return}
 window._pflt=m;
 rows.forEach(o=>{o.r.style.display=
  m==='all'||(m==='fail'&&o.r.classList.contains('fail'))
  ||(m==='dirty'&&o.r.classList.contains('dirty'))?'':'none';});};
const ip=document.getElementById('ip');
const pimg=document.getElementById('pimg');
const pv=document.getElementById('pv');
let ipCur=-1,ipMode='img';
function stripMark(s){return (s||'').replace(/^##\s*/,'');}
function fmtHtml(s){s=(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;')
 .replace(/>/g,'&gt;');
 s=s.replace(/\*\*([\s\S]+?)\*\*/g,'<strong>$1</strong>')
  .replace(/~~([\s\S]+?)~~/g,'<s>$1</s>')
  .replace(/\+\+([\s\S]+?)\+\+/g,'<u>$1</u>')
  .replace(/\*([^*\\n]+?)\*/g,'<em>$1</em>')
  .replace(/ {2,}/g,m=>' '.repeat(m.length-1)+' ');
 const out=[];let buf=[];
 const fl=()=>{if(buf.length){out.push(buf.join('<br>'));buf=[];}};
 s.split('\\n').forEach(ln=>{
  const m=ln.match(/^[ \t　]*(?:&gt;&gt;|＞＞)\s+/);
  if(m){fl();
   out.push('<span style="display:block;text-align:right;text-indent:0">'
    +ln.slice(m[0].length)+'</span>');}
  else if(!ln.trim()){fl();out.push('<br><br>');}
  else buf.push(ln);});
 fl();return out.join('');}
function ipHead(pg,suffix){ip.classList.add('on');
 ipCur=D.pages.indexOf(pg);
 document.getElementById('ipl').textContent=pg+(suffix||'');
 document.getElementById('ipprev').disabled=ipCur<=0;
 document.getElementById('ipnext').disabled=
  ipCur<0||ipCur>=D.pages.length-1;}
function showImg(pg){ipMode='img';ipHead(pg,'');
 pv.style.display='none';pimg.style.display='block';
 pimg.src='/_pageimg/'+encodeURIComponent(pg);}
function showPv(pg){ipMode='pv';ipHead(pg,' — 출력 미리보기');
 pimg.style.display='none';pv.style.display='block';
 pv.innerHTML='';
 let n=0;
 D.paras.forEach(function(e,i){
  if(e.page!==pg||n>=200)return;   // 페이지 정보 없는 옛 데이터 폭주 방지
  n++;
  const src=rows[i]?rows[i].ts.value:e.src;
  const ko=rows[i]?rows[i].tk.value:(D.xlat[String(i)]||'');
  const isHead=src.startsWith('## ');
  const el=document.createElement(isHead?'h2':'p');
  el.innerHTML=fmtHtml(stripMark(ko.trim()||src));
  if(!ko.trim())el.classList.add('untr');   // 미번역 — 원문 그대로
  pv.appendChild(el);});
 if(!n){const d2=document.createElement('div');d2.className='empty';
  d2.textContent='이 페이지에서 시작하는 문단이 없습니다 '
   +'(앞 페이지에서 걸쳐 온 문장은 앞 페이지 미리보기에 포함)';
  pv.appendChild(d2);}}
function ipNav(d){const i=ipCur+d;
 if(i<0||i>=D.pages.length)return;
 if(ipMode==='pv')showPv(D.pages[i]);else showImg(D.pages[i]);}
function ipOff(){ip.classList.remove('on');}
const ocrSel=document.getElementById('ocrsel');
(D.ocr_modes||[]).forEach(function(m,i){
 const o=document.createElement('option');
 o.value=i===0?'':m[1];
 o.textContent=(i===0?'재전사: 실행 설정':'재전사: '+m[0].split(' (')[0]);
 ocrSel.appendChild(o);});
ocrSel.value=localStorage.getItem('edOcr')||'';
if(ocrSel.selectedIndex<0)ocrSel.selectedIndex=0;
ocrSel.onchange=()=>localStorage.setItem('edOcr',ocrSel.value);
async function rescan(pg,btn){
 if(dirty.size&&!await doSave())return;   // 수정분 먼저 저장
 const eng=ocrSel.value;
 const engLb=eng?ocrSel.selectedOptions[0].textContent
  .replace('재전사: ',''):'실행 설정의 전사 방식';
 if(!confirm('『'+pg+'』 페이지를 '+engLb+'(으)로 다시 전사합니다.\\n'
  +'바뀐 문단의 원문·번역이 새로 만들어지고(경계 걸침 포함),\\n'
  +'내용이 같은 문단과 다른 페이지의 번역은 유지됩니다. 계속할까요?'))
  return;
 btn.disabled=true;st.textContent=pg+' 재전사 중… ('+engLb+')';
 try{await api('/api/rescan',eng?{page:pg,ocr:eng}:{page:pg});
  sessionStorage.setItem('edScroll',String(scrollY));
  location.reload();}
 catch(e){st.textContent='재전사 실패: '+e.message;btn.disabled=false;}}
const _sc=sessionStorage.getItem('edScroll');
if(_sc!==null){sessionStorage.removeItem('edScroll');
 requestAnimationFrame(()=>scrollTo(0,+_sc));}
/* ---- CLOUD(모바일 동기화) — GAS 서빙 시 URL·키 주입 ---- */
const PKEY='ebookEdits:'+(D.title||'')+':'+(D.fp||'');
let cq=null,sending=false;
function KEY(){if(GKEY.indexOf('__')!==0)return GKEY;
 let k=localStorage.getItem('gasKey')||'';
 if(!k){k=prompt('동기화 키를 입력하세요')||'';
  if(k)localStorage.setItem('gasKey',k);}
 return k;}
function gasCall(body){body=Object.assign(
  {key:KEY(),book:D.title||'이북'},body);
 return new Promise((res,rej)=>{
  const done=t=>{let r;
   try{r=typeof t==='string'?JSON.parse(t):t}
   catch(e){rej(new Error('응답 파싱 실패'));return}
   r&&r.err?rej(new Error(r.err)):res(r||{});};
  if(window.google&&google.script&&google.script.run)
   google.script.run.withSuccessHandler(done)
    .withFailureHandler(e=>rej(new Error((e&&e.message)||'통신 실패')))
    .apiCall(JSON.stringify(body));
  else fetch(GURL,{method:'POST',body:JSON.stringify(body)})
   .then(r=>r.text()).then(done).catch(rej);});}
function savePend(){try{localStorage.setItem(PKEY,
 JSON.stringify([...dirty.entries()]))}catch(e){}}
function cloudQueue(ms){savePend();clearTimeout(cq);
 cq=setTimeout(cloudSend,ms||2000);}
async function cloudSend(){
 if(sending)return;
 if(!dirty.size){savePend();return}
 sending=true;st.textContent='☁ 저장 중…';
 const snap=[...dirty.entries()].map(([i,d])=>[i,JSON.stringify(d)]);
 try{const r=await gasCall({op:'edits',fp:D.fp,count:D.count,
   edits:Object.fromEntries(snap.map(([i,s])=>[i,JSON.parse(s)]))});
  snap.forEach(([i,s])=>{const d=dirty.get(i);   // 전송 중 재수정분은 유지
   if(d&&JSON.stringify(d)===s){dirty.delete(i);
    const o=rows[i];if(o){o.r.classList.remove('dirty');
     if(d.text!==undefined)o.r.classList.toggle('fail',!d.text.trim());}}});
  savePend();
  if(r.snap!=null&&D.fp&&r.snap!==D.fp)window._stale=true;
  upd();
  st.textContent=dirty.size?'⏳ 대기 '+dirty.size+'건'
   :'☁ 저장됨 '+new Date().toLocaleTimeString();}
 catch(e){st.textContent='⚠ 전송 실패('+e.message+') — 10초 후 재시도';
  cloudQueue(10000);}
 finally{sending=false;}}
function initCloud(){
 document.getElementById('exp').style.display='none';
 document.getElementById('ocrsel').style.display='none';
 const mx=document.getElementById('mexp');mx.style.display='';
 mx.onclick=()=>{const pl={v:1,fp:D.fp,count:D.count,
   edits:Object.fromEntries(dirty)};
  const b=new Blob([JSON.stringify(pl,null,1)],{type:'application/json'});
  const a=document.createElement('a');a.href=URL.createObjectURL(b);
  a.download=(D.title||'이북')+'_edits.json';a.click();};
 function setFs(px){window._fs=px;
  try{localStorage.setItem('edFs',String(px))}catch(e){}
  let se=document.getElementById('fscss');
  if(!se){se=document.createElement('style');se.id='fscss';
   document.head.appendChild(se);}
  se.textContent='#list textarea{font-size:'+px+'px}'
   +'#pv{font-size:'+(px+1)+'px}'
   +'#list .rd{font-size:'+(px+1)+'px}'
   +'#list .rd.rdh{font-size:'+Math.round((px+1)*1.15)+'px}';}
 if(matchMedia('(max-width:640px)').matches)
  document.getElementById('save').textContent='💾';
 const fsm=document.getElementById('fsm'),fsp=document.getElementById('fsp');
 fsm.style.display=fsp.style.display='';
 const fs0=parseInt(localStorage.getItem('edFs')||'16',10);
 setFs(isNaN(fs0)?16:Math.min(24,Math.max(12,fs0)));
 fsm.onclick=()=>setFs(Math.max(12,(window._fs||16)-2));
 fsp.onclick=()=>setFs(Math.min(24,(window._fs||16)+2));
 const hb2=document.getElementById('hpv');hb2.style.display='';
 hb2.onclick=()=>{const pg=D.paras[curIdx()]
   &&D.paras[curIdx()].page||D.pages[0];
  if(pg)showPv(pg);};
 const sb=document.createElement('button');
 sb.textContent='원문';sb.title='원문(전사) 칸 접기/펼치기 — 번역만 읽으며 검수';
 mx.after(sb);
 const setSrc=on=>{document.body.classList.toggle('nosrc',!on);
  sb.style.opacity=on?'':'.5';
  try{localStorage.setItem('edSrcShow',on?'1':'')}catch(e){}};
 sb.onclick=()=>setSrc(document.body.classList.contains('nosrc'));
 setSrc(localStorage.getItem('edSrcShow')!=='');
 try{const pd=JSON.parse(localStorage.getItem(PKEY)||'[]');
  pd.forEach(([i,d])=>{const o=rows[i];if(!o)return;   // 미전송분 복원
   if(d.src!==undefined)o.ts.value=d.src;
   if(d.text!==undefined)o.tk.value=d.text;
   dirty.set(+i,d);o.r.classList.add('dirty');});}catch(e){}
 window.addEventListener('beforeunload',e=>{
  if(dirty.size){e.preventDefault();e.returnValue='';}});
 if(dirty.size){upd();cloudQueue(500);}
 st.textContent='☁ 클라우드 검수 — 수정은 자동 저장됩니다';
 initBmk(true);}
if(CLOUD)initCloud();
else if(SRV)initBmk(false);
else if(!SRV){st.textContent=
 '파일로 열림 — 저장·재번역은 GUI [편집 페이지] 또는 --edit 로 여세요';
 document.getElementById('exp').disabled=true;}
upd();
</script></body></html>
"""


def write_edit_html(out: Path) -> Optional[Path]:
    """_work/book.json + xlat.json → out/edit.html (열 때마다 재생성)."""
    book = load_book(out)
    if not book:
        return None
    done = load_xlat(out)
    data = {"title": book.get("title") or "", "paras": book["paras"],
            "pages": book.get("page_labels") or [],
            "fp": book_fingerprint(book), "count": len(book["paras"]),
            "cover": book.get("cover") or "",
            "ocr_modes": [["실행 설정", ""]] + [[lb, k]
                                               for lb, k in OCR_MODES],
            "xlat": {str(k): v for k, v in done.items()}}
    page = (EDIT_HTML
            .replace("__VER__", __version__)
            .replace("__TITLE__", html.escape(data["title"] or "이북"))
            .replace("__DATA__", json.dumps(data, ensure_ascii=False)
                     .replace("</", "<\\/")))
    fp = out / "edit.html"
    fp.write_text(page, encoding="utf-8")
    return fp


def _edit_save(out: Path, edits: list, log) -> dict:
    """원문(book.json)·번역(xlat.json) 수정 반영 — 원자적 저장."""
    book = load_book(out)
    if not book:
        raise RuntimeError("편집 데이터(book.json)가 없습니다")
    done = load_xlat(out)
    ns = nt = 0
    for e in edits:
        try:
            i = int(e.get("i", -1))
        except (TypeError, ValueError):
            continue
        if not 0 <= i < len(book["paras"]):
            continue
        if "src" in e:
            book["paras"][i]["src"] = _clean_para(e["src"])
            ns += 1
        if "text" in e:
            t = _clean_text(e["text"])
            done[i] = t or None
            nt += 1
    work = out / "_work"
    work.mkdir(parents=True, exist_ok=True)
    _atomic_json(work / "book.json", book)
    _atomic_json(work / "xlat.json", {str(k): v for k, v in done.items()})
    if ns:      # 원문 검수 파일도 동기화
        title = book.get("title") or "book"
        (out / f"{title}_src.txt").write_text(
            "\n\n".join(e["src"] for e in book["paras"]) + "\n",
            encoding="utf-8")
    log(f"편집 저장: 원문 {ns}건, 번역 {nt}건")
    return {"src": ns, "text": nt}


def _edit_xlat(out: Path, cfg: dict, ids: list, log) -> dict:
    """지정 문단만 재번역 (원문 수정 후 등) — 실패 시 기존 번역 유지."""
    book = load_book(out)
    if not book:
        raise RuntimeError("편집 데이터(book.json)가 없습니다")
    done = load_xlat(out)
    paras = [e.get("src") or "" for e in book["paras"]]
    try:
        ids = sorted({int(i) for i in ids if 0 <= int(i) < len(paras)})
    except (TypeError, ValueError):
        ids = []
    if not ids:
        return {}
    gloss = _load_glossary(cfg.get("glossary"), out)
    j = ids[0] - 1                          # 직전 문단을 문맥으로
    ctx = (f"[원문] {paras[j][-300:]}\n[번역] {(done.get(j) or '')[-300:]}"
           if j >= 0 else "")
    sl = book.get("source_lang") or cfg.get("source_lang") or "de"
    if sl not in LANG_NAMES:                # "auto" 등은 기본값으로
        sl = "de"
    cfg = {**cfg, "source_lang": sl}
    res = translate_chunk([(i, paras[i]) for i in ids], cfg, gloss, ctx)
    for i, t in res.items():
        if t:
            done[i] = t
    _atomic_json(out / "_work" / "xlat.json",
                 {str(k): v for k, v in done.items()})
    log(f"재번역 {sum(1 for t in res.values() if t)}/{len(ids)}건")
    us = retype.usage_summary()
    if us:
        log(us)
    return {str(i): t for i, t in res.items()}


def _edit_export(out: Path, cfg: dict, log) -> dict:
    book = load_book(out)
    if not book:
        raise RuntimeError("편집 데이터(book.json)가 없습니다")
    done = load_xlat(out)
    paras = [e.get("src") or "" for e in book["paras"]]
    r = export_outputs(out, book.get("title") or "book",
                       book.get("source_lang") or "de", paras, done,
                       cover=_cover_jpg(cfg, book))
    log(f"재생성: {r['txt'].name} / {r['epub'].name} ({r['chapters']}개 장)")
    return {"files": [r["txt"].name, r["epub"].name],
            "chapters": r["chapters"]}


def _edit_cover(out: Path, label: str, log) -> dict:
    """편집 페이지 [🖼 표지] — EPUB 표지로 쓸 페이지를 book.json에 저장."""
    book = load_book(out)
    if not book:
        raise RuntimeError("편집 데이터(book.json)가 없습니다")
    if label not in (book.get("page_labels") or []):
        raise RuntimeError(f"페이지 정보가 없습니다: {label}")
    book["cover"] = label
    _atomic_json(out / "_work" / "book.json", book)
    log(f"EPUB 표지 지정: {label} — 재생성 시 반영")
    return {"ok": True, "cover": label}


def _edit_page_image(handler, out: Path, cfg: dict, label: str) -> None:
    """/_pageimg/<라벨> — 스캔 원본 서빙 (PDF는 렌더 후 캐시)."""
    label = Path(label).name                # 경로 탈출 방지
    src = Path(cfg.get("src") or "")
    mime = {".png": "image/png", ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg", ".webp": "image/webp",
            ".bmp": "image/bmp", ".tif": "image/tiff", ".tiff": "image/tiff"}

    def send(data: bytes, ctype: str) -> None:
        handler.send_response(200)
        handler.send_header("Content-Type", ctype)
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)

    if src.is_dir():
        fp = src / label
        if fp.exists() and fp.suffix.lower() in mime:
            send(fp.read_bytes(), mime[fp.suffix.lower()])
            return
    elif src.suffix.lower() == ".pdf" and re.fullmatch(r"p\d+", label):
        cache = out / "_work" / "imgcache" / f"{label}.png"
        if not cache.exists():
            import cv2
            img = load_page_image(src, "pdf-scan", int(label[1:]) - 1)
            cache.parent.mkdir(parents=True, exist_ok=True)
            ok, buf = cv2.imencode(".png", _prep_for_claude(img))
            if ok:
                cache.write_bytes(buf.tobytes())
        if cache.exists():
            send(cache.read_bytes(), "image/png")
            return
    handler.send_error(404)


def _edit_rescan(out: Path, cfg: dict, label: str, log) -> dict:
    """편집 페이지: 한 페이지만 재전사 후 book.json 국소 교체.

    해당 페이지에서 시작(또는 걸침)하는 문단 블록을 새 전사·재병합
    결과로 바꾸고, 뒤 문단들의 번역(xlat) 인덱스를 시프트해 보존한다.
    교체된 블록의 번역은 초기화 → [▶ 번역 시작] 재실행(실패분 재시도)
    또는 행별 🌐 재번역으로 채운다. 경계에 걸친 병합도 재계산."""
    book = load_book(out)
    if not book:
        raise RuntimeError("편집 데이터(book.json)가 없습니다")
    labels = book.get("page_labels") or []
    if label not in labels:
        raise RuntimeError(f"페이지 정보가 없습니다: {label}")
    src = Path(cfg.get("src") or "")
    kind, _total = probe_source(src)
    li = labels.index(label)
    sl = book.get("source_lang") or "de"
    if sl not in LANG_NAMES:
        sl = "de"

    # ---- 1. 재전사 (본 실행과 같은 캐시 파일 갱신) ----
    def _pg(lb):
        return (src / lb) if kind == "images" else int(lb[1:]) - 1

    _apply_keys(cfg)
    if kind == "pdf-text":
        doc = _pdf_doc(src)
        try:
            t = doc[_pg(label)].get_text("text")
        finally:
            doc.close()
    else:
        img = load_page_image(src, kind, _pg(label))
        ocr = cfg.get("ocr") or "claude"
        if ocr == "claude":
            t = transcribe_page_claude(
                img, cfg.get("claude_model") or "claude-sonnet-4-5")
        elif ocr == "gemini":
            t = transcribe_page_gemini(
                img, cfg.get("gemini_model") or GEMINI_MODEL,
                cfg["gemini_key"])
        elif ocr == "deepseek":
            t = transcribe_page_deepseek(
                img, cfg.get("deepseek_model") or DEEPSEEK_MODEL,
                cfg["deepseek_key"], cfg.get("deepseek_url") or DEEPSEEK_URL)
        elif ocr == "winocr":
            t = transcribe_page_winocr(img, sl)
        else:
            codes = {"de": "deu", "en": "eng", "ja": "jpn"}
            t = transcribe_page_tesseract(img, codes.get(sl, "eng"))
    pages_dir = out / "_work" / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    (pages_dir / f"{Path(label).stem}.txt").write_text(t, encoding="utf-8")

    # ---- 2. 교체 윈도 결정 (걸침 문단 포함) ----
    paras = book["paras"]
    lidx = {lb: i for i, lb in enumerate(labels)}

    def pi(x):
        return lidx.get(x.get("page"), li)

    def pe(x):
        return lidx.get(x.get("page_end") or x.get("page"), li)

    aff = [i for i, x in enumerate(paras) if pi(x) <= li <= pe(x)]
    if aff:
        q = min(min(pi(paras[i]) for i in aff), li)
        r = max(max(pe(paras[i]) for i in aff), li)
        s, e = min(aff), max(aff) + 1
        while e < len(paras) and pi(paras[e]) <= r:
            e += 1              # 윈도 페이지에서 시작하는 문단 전부 포함
    else:                       # 원래 빈 페이지였던 경우 — 삽입 지점
        q = r = li
        s = e = next((i for i, x in enumerate(paras) if pi(x) > li),
                     len(paras))

    # ---- 3. 윈도 재병합 (머리글 기준은 책 전체 캐시로 계산) ----
    def _page_text(lb):
        if kind == "pdf-text":
            return None                     # 아래에서 일괄 추출
        cp = pages_dir / f"{Path(lb).stem}.txt"
        if not cp.exists():
            raise RuntimeError(f"전사 캐시가 없는 페이지입니다: {lb} — "
                               "먼저 [▶ 번역 시작]으로 전체 전사를 하세요")
        return cp.read_text(encoding="utf-8")

    if kind == "pdf-text":
        doc = _pdf_doc(src)
        try:
            wtexts = [doc[_pg(lb)].get_text("text")
                      for lb in labels[q:r + 1]]
        finally:
            doc.close()
        heads = None                        # 텍스트 PDF는 머리글 판정 생략
        wp, wpp, wpe = merge_pages_tagged(wtexts, labels[q:r + 1],
                                          drop_heads=False)
    else:
        all_texts = [_page_text(lb) for lb in labels]
        heads = _repeated_heads(all_texts)
        wtexts = [all_texts[i] for i in range(q, r + 1)]
        wp, wpp, wpe = merge_pages_tagged(wtexts, labels[q:r + 1],
                                          known_heads=heads)

    # ---- 4. 경계 병합 재계산 (앞뒤 이웃 문단과) ----
    if wp and s > 0 and _should_join(paras[s - 1]["src"], wp[0]):
        wp[0] = _join_paras(paras[s - 1]["src"], wp[0])
        wpp[0] = paras[s - 1]["page"]
        s -= 1                              # 이웃도 교체 (번역 초기화)
    if wp and e < len(paras) and _should_join(wp[-1], paras[e]["src"]):
        nxt = paras[e]
        wp[-1] = _join_paras(wp[-1], nxt["src"])
        wpe[-1] = nxt.get("page_end") or nxt["page"]
        e += 1

    # ---- 5. 교체 + 번역 인덱스 시프트 ----
    new = [dict({"src": a, "page": b},
                **({"page_end": c} if c != b else {}))
           for a, b, c in zip(wp, wpp, wpe)]
    delta = len(new) - (e - s)
    done = load_xlat(out)
    nd = {}
    for k, v in done.items():
        if k < s:
            nd[k] = v
        elif k >= e:
            nd[k + delta] = v               # 뒤 문단 번역 보존 (시프트)
    # 블록 안에서도 원문이 그대로인 머리/꼬리 문단은 번역 보존
    old = paras[s:e]
    hm = 0
    lim = min(len(old), len(new))
    while hm < lim and old[hm]["src"] == new[hm]["src"]:
        v = done.get(s + hm)
        if v is not None:
            nd[s + hm] = v
        hm += 1
    tm = 0
    while (tm < lim - hm
           and old[len(old) - 1 - tm]["src"] == new[len(new) - 1 - tm]["src"]):
        v = done.get(s + len(old) - 1 - tm)
        if v is not None:
            nd[s + len(new) - 1 - tm] = v
        tm += 1
    book["paras"][s:e] = new
    work = out / "_work"
    _atomic_json(work / "book.json", book)
    _atomic_json(work / "xlat.json", {str(k): v for k, v in nd.items()})
    title = book.get("title") or "book"
    (out / f"{title}_src.txt").write_text(
        "\n\n".join(x["src"] for x in book["paras"]) + "\n",
        encoding="utf-8")
    write_edit_html(out)                    # 새로고침용 재생성
    log(f"재전사 {label}: 문단 {e - s}→{len(new)}개 교체 (#{s + 1}부터) — "
        "새 문단은 번역 필요 상태 (재실행 시 실패분과 함께 번역됨)")
    us = retype.usage_summary()
    if us:
        log(us)
    return {"start": s, "removed": e - s, "added": len(new)}


def run_edit_server(cfg: dict, log, is_busy=None) -> Optional[str]:
    """편집 서버 시작 (데몬 스레드) → URL. 편집 데이터 없으면 None.

    is_busy(): True면 저장·재번역·재생성 요청을 409로 거절 — GUI에서
    번역 작업이 도는 동안 xlat.json 쓰기 경합을 막는다."""
    import functools
    import threading
    import zlib
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
    out, _ = resolve_out(cfg)
    if not write_edit_html(out):
        return None
    _apply_keys(cfg)
    busy = {"on": False}
    ext_busy = is_busy or (lambda: False)

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *a):          # 콘솔 소음 제거
            pass

        def end_headers(self):
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

        def _json(self, obj, code=200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type",
                             "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            p = self.path.split("?")[0]
            if p.startswith("/_pageimg/"):
                from urllib.parse import unquote
                try:
                    _edit_page_image(self, out, cfg,
                                     unquote(p[len("/_pageimg/"):]))
                except Exception:
                    try:
                        self.send_error(404)
                    except Exception:
                        pass
                return
            super().do_GET()

        def do_POST(self):
            p = self.path.split("?")[0]
            if p not in ("/api/save", "/api/xlat", "/api/export",
                         "/api/rescan", "/api/cover"):
                self.send_error(404)
                return
            if busy["on"] or ext_busy():
                self.send_error(409, "busy")
                return
            busy["on"] = True
            try:
                n = int(self.headers.get("Content-Length", 0))
                req = (json.loads(self.rfile.read(n).decode("utf-8"))
                       if n else {})
                if p == "/api/save":
                    self._json(_edit_save(out, req.get("edits") or [], log))
                elif p == "/api/xlat":
                    self._json(_edit_xlat(out, cfg,
                                          req.get("ids") or [], log))
                elif p == "/api/rescan":
                    c2 = dict(cfg)
                    if req.get("ocr") in ("claude", "gemini", "deepseek",
                                          "winocr", "tesseract"):
                        c2["ocr"] = req["ocr"]   # 편집 페이지에서 선택
                    self._json(_edit_rescan(out, c2,
                                            req.get("page") or "", log))
                elif p == "/api/cover":
                    self._json(_edit_cover(out, req.get("page") or "",
                                           log))
                else:
                    self._json(_edit_export(out, cfg, log))
            except Exception as e:
                log(f"!! 편집 서버 오류: {e}")
                try:
                    self._json({"error": str(e)[:200]}, code=500)
                except Exception:
                    pass
            finally:
                busy["on"] = False

    # 출력 폴더별 고정 포트 — 껐다 켜도 같은 origin (코믹스 검수와 동일)
    port = 49152 + zlib.crc32(str(out).encode("utf-8")) % 16000
    try:
        srv = ThreadingHTTPServer(
            ("127.0.0.1", port),
            functools.partial(Handler, directory=str(out)))
    except OSError:                         # 포트 충돌 — 임의 포트 폴백
        try:
            srv = ThreadingHTTPServer(
                ("127.0.0.1", 0),
                functools.partial(Handler, directory=str(out)))
        except Exception as e:
            log(f"!! 편집 서버 시작 실패: {e}")
            return None
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    log(f"편집 서버 시작: {url}/edit.html")
    return url


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _cli() -> int:
    d = load_defaults()
    ap = argparse.ArgumentParser(
        description="스캔 이북(이미지 폴더/PDF) → 한글 TXT+EPUB 번역")
    ap.add_argument("src", help="스캔 이미지 폴더 또는 PDF 파일")
    ap.add_argument("--out", default=None, help="출력 폴더 (기본 자동)")
    ap.add_argument("--title", default=None, help="책 제목 (기본 소스명)")
    ap.add_argument("--source-lang", default="auto",
                    choices=["auto", "de", "en", "ja"],
                    help="원서 언어 (기본 auto = 첫 페이지 전사로 감지)")
    ap.add_argument("--ocr", default="claude",
                    choices=["claude", "gemini", "deepseek", "winocr",
                             "tesseract"],
                    help="전사 방식 (텍스트 PDF는 자동으로 OCR 생략) — "
                         "gemini/deepseek은 저가 비전, winocr는 Windows "
                         "기본 OCR(무료)")
    ap.add_argument("--translate-backend", dest="backend", default="claude",
                    choices=["claude", "gemini", "ollama"])
    ap.add_argument("--model", dest="claude_model",
                    default=d.get("claude_model", "claude-sonnet-4-5"))
    ap.add_argument("--gemini-model",
                    default=d.get("gemini_model", GEMINI_MODEL))
    ap.add_argument("--gemini-key",
                    default=d.get("gemini_key", ""),
                    help="Gemini API 키 (기본: GEMINI_API_KEY 환경변수)")
    ap.add_argument("--deepseek-model",
                    default=d.get("deepseek_model", DEEPSEEK_MODEL))
    ap.add_argument("--deepseek-key", default=d.get("deepseek_key", ""),
                    help="DeepSeek API 키 (기본: DEEPSEEK_API_KEY 환경변수)")
    ap.add_argument("--deepseek-url", default=d.get("deepseek_url", ""),
                    help="OpenAI 호환 비전 서버 URL (기본 공식 API — "
                         "DeepInfra·DashScope 등으로 교체 가능)")
    ap.add_argument("--ollama-model",
                    default=d.get("ollama_model", retype.OLLAMA_MODEL))
    ap.add_argument("--ollama-url", default=retype.OLLAMA_URL)
    ap.add_argument("--glossary", default=None)
    ap.add_argument("--range", dest="page_range", default=None,
                    help="페이지 범위 (예: 5-20, 5-, -20)")
    ap.add_argument("--edit", action="store_true",
                    help="편집 모드 — 번역 결과를 브라우저에서 검토·수정")
    a = ap.parse_args()
    cfg = {**vars(a), "api_key": d.get("api_key", "")}
    if a.edit:
        url = run_edit_server(cfg, print)
        if not url:
            print("편집할 데이터가 없습니다 — 먼저 번역을 실행하세요 "
                  "(출력 폴더에 _work\\book.json 이 생깁니다)",
                  file=sys.stderr)
            return 2
        import time
        import webbrowser
        webbrowser.open(url + "/edit.html")
        print("편집 서버 실행 중 — 종료: Ctrl+C")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return 0
    try:
        run_book(cfg, print, lambda: False)
        return 0
    except (RuntimeError, Cancelled) as e:
        print(f"오류: {e}", file=sys.stderr)
        u = retype.usage_summary()      # 중단 시에도 그때까지의 요금 표시
        if u:
            print(u)
        return 2


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
OCR_MODES = [("Claude 비전 (정확 — API 크레딧 사용)", "claude"),
             ("Gemini 비전 (저렴 — Google API 키 필요)", "gemini"),
             ("DeepSeek 비전 (초저가 실험적 — DeepSeek 키 필요)",
              "deepseek"),
             ("Windows OCR (무료 — 이북 캡처·디지털 텍스트에 강함)",
              "winocr"),
             ("Tesseract (무료 — 깨끗한 인쇄 스캔용)", "tesseract")]
BACKENDS = [("Claude API (권장 — 문학 번역 품질)", "claude"),
            ("Gemini (저렴 — Google API 키 필요)", "gemini"),
            ("Ollama 로컬 (무료·오프라인)", "ollama")]
LANGS = [("자동 감지 (독/영/일)", "auto"),
         ("독일어 → 한글", "de"), ("영어 → 한글", "en"),
         ("일본어 → 한글", "ja")]


def _gui() -> None:
    import queue
    import threading
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    d = load_defaults()
    root = tk.Tk()
    root.title(f"스캔 이북 한글 번역 v{__version__}")
    root.minsize(660, 560)
    frm = ttk.Frame(root, padding=10)
    frm.pack(fill="both", expand=True)
    frm.columnconfigure(1, weight=1)
    row = [0]

    def nrow() -> int:
        row[0] += 1
        return row[0] - 1

    def pick_lbl(k, keys):
        return next((lb for lb, key in keys if key == d.get(k, keys[0][1])),
                    keys[0][0])

    v = {
        "src": tk.StringVar(value=d.get("src", "")),
        "title": tk.StringVar(value=d.get("title", "")),
        "source_lang": tk.StringVar(value=pick_lbl("source_lang", LANGS)),
        "ocr": tk.StringVar(value=pick_lbl("ocr", OCR_MODES)),
        "backend": tk.StringVar(value=pick_lbl("backend", BACKENDS)),
        "claude_model": tk.StringVar(
            value=d.get("claude_model", "claude-sonnet-4-5")),
        "gemini_model": tk.StringVar(
            value=d.get("gemini_model", GEMINI_MODEL)),
        "gemini_key": tk.StringVar(value=d.get("gemini_key", "")),
        "deepseek_model": tk.StringVar(
            value=d.get("deepseek_model", DEEPSEEK_MODEL)),
        "deepseek_key": tk.StringVar(value=d.get("deepseek_key", "")),
        "deepseek_url": tk.StringVar(value=d.get("deepseek_url", "")),
        "ollama_model": tk.StringVar(
            value=d.get("ollama_model", retype.OLLAMA_MODEL)),
        "glossary": tk.StringVar(value=d.get("glossary", "")),
        "page_range": tk.StringVar(value=""),
        "api_key": tk.StringVar(value=d.get("api_key", "")),
    }

    def add_entry(label, key, browse=None):
        r = nrow()
        ttk.Label(frm, text=label).grid(row=r, column=0, sticky="w", pady=2)
        e = ttk.Entry(frm, textvariable=v[key])
        e.grid(row=r, column=1, sticky="ew", padx=4)
        if browse:
            ttk.Button(frm, text="찾기", width=6, command=browse).grid(
                row=r, column=2)
        return e

    def add_combo(label, key, pairs):
        r = nrow()
        ttk.Label(frm, text=label).grid(row=r, column=0, sticky="w", pady=2)
        cb = ttk.Combobox(frm, textvariable=v[key], state="readonly",
                          values=[lb for lb, _ in pairs])
        cb.grid(row=r, column=1, sticky="ew", padx=4)
        return cb

    def browse_src():
        p = filedialog.askopenfilename(
            title="PDF 선택 (이미지 폴더는 옆의 '폴더')",
            filetypes=[("PDF", "*.pdf"), ("모든 파일", "*.*")])
        if p:
            v["src"].set(p)

    def browse_dir():
        p = filedialog.askdirectory(title="스캔 이미지 폴더 선택")
        if p:
            v["src"].set(p)

    r = nrow()
    ttk.Label(frm, text="소스 (폴더/PDF)").grid(row=r, column=0, sticky="w")
    ttk.Entry(frm, textvariable=v["src"]).grid(row=r, column=1,
                                               sticky="ew", padx=4)
    bf = ttk.Frame(frm)
    bf.grid(row=r, column=2)
    ttk.Button(bf, text="폴더", width=5, command=browse_dir).pack(
        side="left")
    ttk.Button(bf, text="PDF", width=5, command=browse_src).pack(
        side="left")

    add_entry("책 제목 (출력 파일명)", "title")
    add_combo("원서 언어", "source_lang", LANGS)
    add_combo("전사 방식", "ocr", OCR_MODES)
    add_combo("번역 엔진", "backend", BACKENDS)
    r = nrow()
    ttk.Label(frm, foreground="#666",
              text="※ 전사·번역은 따로 조합 — 가성비: Gemini 비전 전사 "
                   "+ Claude 번역 (끝나면 파트별 예상 요금 표시)").grid(
        row=r, column=1, columnspan=2, sticky="w", padx=4)
    r = nrow()
    ttk.Label(frm, text="Claude 모델").grid(row=r, column=0, sticky="w")
    ttk.Combobox(frm, textvariable=v["claude_model"],
                 values=["claude-sonnet-4-5", "claude-haiku-4-5"],
                 width=22).grid(row=r, column=1, sticky="w", padx=4)
    r = nrow()
    ttk.Label(frm, text="Gemini 모델").grid(row=r, column=0, sticky="w")
    ttk.Combobox(frm, textvariable=v["gemini_model"],
                 values=["gemini-3.1-flash-lite", "gemini-2.5-flash-lite",
                         "gemini-3.5-flash"],
                 width=22).grid(row=r, column=1, sticky="w", padx=4)
    add_entry("Gemini API 키 (선택)", "gemini_key")
    add_entry("DeepSeek API 키 (선택)", "deepseek_key")
    add_entry("Ollama 모델", "ollama_model")
    add_entry("용어집(선택)", "glossary", browse=lambda: v["glossary"].set(
        filedialog.askopenfilename(filetypes=[("텍스트", "*.txt"),
                                              ("모든 파일", "*.*")]) or
        v["glossary"].get()))
    add_entry("페이지 범위 (예: 5-20)", "page_range")
    add_entry("API 키 (코믹스 앱과 공유)", "api_key")

    r = nrow()
    btns = ttk.Frame(frm)
    btns.grid(row=r, column=0, columnspan=3, pady=6, sticky="ew")
    run_btn = ttk.Button(btns, text="▶ 번역 시작")
    run_btn.pack(side="left")
    cancel_btn = ttk.Button(btns, text="■ 취소", state="disabled")
    cancel_btn.pack(side="left", padx=6)
    edit_btn = ttk.Button(btns, text="📝 편집 페이지")
    edit_btn.pack(side="left", padx=6)
    ttk.Label(btns, foreground="#666",
              text="같은 출력 폴더로 다시 실행하면 이어서 합니다"
              ).pack(side="left", padx=8)

    r = nrow()
    logbox = tk.Text(frm, height=16, state="disabled", wrap="word")
    logbox.grid(row=r, column=0, columnspan=3, sticky="nsew")
    frm.rowconfigure(r, weight=1)

    q: queue.Queue = queue.Queue()
    state = {"running": False, "cancel": False, "edit_url": None}

    def log(msg):
        q.put(str(msg))

    def pump():
        try:
            while True:
                m = q.get_nowait()
                logbox.configure(state="normal")
                logbox.insert("end", m + "\n")
                logbox.see("end")
                logbox.configure(state="disabled")
        except queue.Empty:
            pass
        root.after(150, pump)

    def collect() -> dict:
        cfg = {k: sv.get().strip() if isinstance(sv.get(), str) else sv.get()
               for k, sv in v.items()}
        for k, pairs in (("source_lang", LANGS), ("ocr", OCR_MODES),
                         ("backend", BACKENDS)):
            cfg[k] = next((key for lb, key in pairs if lb == cfg[k]),
                          pairs[0][1])
        cfg["ollama_url"] = retype.OLLAMA_URL
        return cfg

    def worker(cfg):
        try:
            run_book(cfg, log, lambda: state["cancel"])
            log("=== 작업 완료 ===")
        except Cancelled:
            log("=== 취소됨 (재실행하면 이어서 합니다) ===")
        except Exception as e:
            log(f"!! 오류: {e}")
        finally:
            u = retype.usage_summary()  # 취소·오류 시에도 요금 표시
            if u:
                log(u)
            state["running"] = False
            q.put("__DONE__")

    def poll_done():
        if not state["running"]:
            run_btn.configure(state="normal")
            cancel_btn.configure(state="disabled")
            return
        root.after(300, poll_done)

    def start():
        if state["running"]:
            return
        cfg = collect()
        if not cfg["src"]:
            messagebox.showerror("오류", "소스 폴더 또는 PDF를 지정하세요")
            return
        try:
            prev = {}
            try:                        # 웹앱 전용 키(gas_* 등) 보존
                prev = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            except Exception:
                prev = {}
            prev.update({k: cfg[k] for k in
                         ("src", "title", "source_lang", "ocr", "backend",
                          "claude_model", "gemini_model", "gemini_key",
                          "deepseek_model", "deepseek_key",
                          "deepseek_url", "ollama_model", "glossary",
                          "api_key")})
            CONFIG_PATH.write_text(
                json.dumps(prev, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except OSError:
            pass
        state.update(running=True, cancel=False)
        run_btn.configure(state="disabled")
        cancel_btn.configure(state="normal")
        threading.Thread(target=worker, args=(cfg,), daemon=True).start()
        poll_done()

    def open_edit():
        import webbrowser
        cfg = collect()
        if not cfg["src"]:
            messagebox.showerror("오류", "소스 폴더 또는 PDF를 지정하세요")
            return
        try:
            if state["edit_url"]:           # 서버 재사용 — 최신 데이터로 재생성
                write_edit_html(resolve_out(cfg)[0])
            else:
                state["edit_url"] = run_edit_server(
                    cfg, log, is_busy=lambda: state["running"])
        except Exception as e:
            log(f"!! 편집 페이지 열기 실패: {e}")
            return
        if not state["edit_url"]:
            messagebox.showinfo(
                "확인", "편집할 데이터가 아직 없습니다.\n"
                "[▶ 번역 시작]으로 전사·병합이 끝난 뒤 다시 눌러보세요.\n"
                "(실행 중이라면 '본문 재구성' 로그가 나온 뒤부터 가능)")
            return
        webbrowser.open(state["edit_url"] + "/edit.html")

    run_btn.configure(command=start)
    edit_btn.configure(command=open_edit)
    cancel_btn.configure(command=lambda: state.update(cancel=True))
    pump()
    root.mainloop()


if __name__ == "__main__":
    if len(sys.argv) > 1:
        raise SystemExit(_cli())
    _gui()
