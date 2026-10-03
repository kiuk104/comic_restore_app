# -*- coding: utf-8 -*-
"""PDF 문서 레이아웃 보존 번역 v0.1.0
=====================================

텍스트 레이어가 있는 PDF 문서(잡지·논문·매뉴얼)를 레이아웃(사진·표·단
구성) 그대로 유지하며 한글 PDF로 번역한다. 책 흐름 번역(ebook_translate,
TXT/EPUB)과 상보적인 "문서 모드" — 번역 엔진·용어집·언어 감지·사용량
집계는 ebook_translate/comic_retype_pipeline 것을 그대로 재사용한다.

흐름 (2026-08-08 Spiegel p11 프로토타입에서 검증):
  1. 페이지별 텍스트 블록 추출 (PyMuPDF, bbox+대표 폰트·크기·색)
  2. 블록 체인 — 열 판정(x0 클러스터) 후 읽기 순서로 정렬, 종결부호로
     끝나지 않은 블록을 다음 블록과 병합 (사진 컷아웃·단 넘김으로 쪼개진
     문단 복원). 러닝헤드·쪽번호·장식 낱글자는 스킵.
  3. 디하이픈 (soft hyphen + 줄끝 분철) 후 체인 단위 배치 번역
     (translate_chunk 재사용, 문서용 프롬프트). 체인 해시로 resume.
  4. redaction(fill 없음 — 사진·컬러 밴드 보존)으로 원문 글리프만 제거,
     같은 bbox에 한글 재삽입 (insert_htmlbox, word-break:keep-all,
     굵기·색 매핑). 1차 스크래치 문서에서 자동 축소 배율을 측정해
     2차에 체인별 균일 크기로 적용.
  5. <제목>_ko.pdf 저장 (한글 폰트 서브셋 임베딩)

알려진 한계(v0.1): 캡션 뒤 컬러 밴드가 원본 줄 폭 기준이라 새 줄바꿈과
어긋날 수 있음 · 원형 컷아웃 주변 랩 텍스트가 이미지에 일부 겹칠 수
있음 · 체인 내 분배가 면적 비례라 블록별 과소/과잉 가능. 스캔 PDF는
미지원(텍스트 레이어 필수).

실행:
  python ebook_pdf_translate.py 문서.pdf [--range 5-20] [--backend claude]
  (웹앱 ebook_translate_web.py 의 "PDF 문서" 모드가 공식 UI)
"""
from __future__ import annotations

__version__ = "0.1.3"   # 번역 캐시 재사용·누락 문단 로그 (재실행 요금 진단)

import argparse
import hashlib
import html
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import ebook_translate as core            # noqa: E402  (엔진·설정 재사용)
import comic_retype_pipeline as retype    # noqa: E402

APP_DIR = core.APP_DIR
FONT_DIR = APP_DIR / "fonts"

# 재삽입 폰트 — 원본 스타일 → fonts/ 한글 폰트 (별칭: css @font-face용)
FONT_SERIF = ("ridi.ttf", "리디바탕.ttf")
FONT_BOLD = ("dream6.ttf", "에스코어 드림 6 Bold.ttf")
FONT_SANS = ("dream3.ttf", "에스코어 드림 3 Light.ttf")

SIZE_FACTOR = 0.94        # 한글 글리프 시각 밀도 보정 (원본 크기 대비)
COL_TOL = 60              # 열 클러스터 허용 오차(pt)
MIN_CHAIN_CHARS = 2       # 이 이하 낱글자 블록은 장식으로 보고 스킵
EDGE_FRAC = 0.07          # 페이지 상하 이 비율 안의 짧은 숫자 블록 = 러닝헤드 후보

TERMINAL_RE = re.compile(r'[.!?«»:"“”]\s*$')   # 문장 종료로 보는 끝
NUMS_RE = re.compile(r"\d")

PROMPT_XLAT_DOC = """PDF 문서({lang})의 문단 목록입니다 (읽는 순서).

- 자연스러운 한국어 문어체 — 기사·문서 번역체, 직역투 지양
- 문단 수와 순서 유지 — 문단을 합치거나 쪼개지 말 것
- 숫자·단위·고유명사·URL은 정확히 유지
- 인용부호 « » “ ” 는 원문 스타일대로 유지 — JSON을 깨뜨리는 일반
  큰따옴표(")는 쓰지 말고, 부득이하면 반드시 \\" 로 이스케이프
- 번역할 수 없는 문단(수식·코드 등)은 text에 null
{gloss}{ctx}
JSON 배열만 출력 (설명 금지):
[{{"id": 1, "text": "..."}}, ...]"""

CSS = """
@font-face {font-family: ridi;   src: url(ridi.ttf);}
@font-face {font-family: dream6; src: url(dream6.ttf);}
@font-face {font-family: dream3; src: url(dream3.ttf);}
p {margin: 0; text-align: justify; line-height: 1.32; word-break: keep-all;}
"""


def _pymupdf():
    try:
        import pymupdf
        return pymupdf
    except ImportError:
        raise RuntimeError("PDF 문서 모드에는 PyMuPDF가 필요합니다: "
                           "pip install pymupdf")


# ---------------------------------------------------------------------------
# 1. 블록 추출
# ---------------------------------------------------------------------------
def dehyph(t: str, lang: str = "de") -> str:
    """soft hyphen 제거 + 줄끝 분철 하이픈 결합 + 공백 정리."""
    t = t.replace("­ ", "").replace("­", "")
    t = re.sub(r"(\w)-\s+(?=[a-zäöüß])", r"\1", t)
    return re.sub(r"\s+", " ", t).strip()


def extract_blocks(pymupdf, page) -> list[dict]:
    """텍스트 블록 목록 — bbox·대표 폰트/크기/색·텍스트(줄은 공백 결합)."""
    out = []
    for b in page.get_text("dict")["blocks"]:
        if b["type"] != 0:
            continue
        sizes, fonts, colors, parts = [], [], [], []
        for ln in b["lines"]:
            for s in ln["spans"]:
                sizes.append(round(s["size"], 1))
                fonts.append(s["font"])
                colors.append(s["color"])
            parts.append("".join(s["text"] for s in ln["spans"]))
        txt = " ".join(parts)
        if not txt.strip():
            continue
        out.append(dict(
            rect=pymupdf.Rect(b["bbox"]),
            size=max(set(sizes), key=sizes.count),
            font=max(set(fonts), key=fonts.count),
            color=max(set(colors), key=colors.count),
            text=txt))
    return out


# ---------------------------------------------------------------------------
# 2. 스킵 판정 + 블록 체인
# ---------------------------------------------------------------------------
def _skip(b: dict, page_h: float) -> bool:
    t = b["text"].strip()
    if len(t) <= MIN_CHAIN_CHARS:                 # 장식 낱글자·불릿
        return True
    if re.fullmatch(r"[\d\s|·.–-]+", t):          # 쪽번호 등 숫자 뿐
        return True
    edge = page_h * EDGE_FRAC
    near_edge = b["rect"].y0 < edge or b["rect"].y1 > page_h - edge
    if near_edge and len(t) < 60 and NUMS_RE.search(t):
        return True                               # 러닝헤드/꼬리글 휴리스틱
    return False


def _col_centers(blocks: list[dict]) -> list[float]:
    """블록 x0 클러스터 중심 — 다단 판정용 (허용 오차 COL_TOL)."""
    centers: list[list[float]] = []                # [합, 개수]씩 [sum, n]
    for b in sorted(blocks, key=lambda b: b["rect"].x0):
        x = b["rect"].x0
        for c in centers:
            if abs(x - c[0] / c[1]) < COL_TOL:
                c[0] += x
                c[1] += 1
                break
        else:
            centers.append([x, 1])
    return [c[0] / c[1] for c in centers]


def chain_blocks(blocks: list[dict], page_h: float) -> list[list[int]]:
    """읽기 순서(열→y) 정렬 후 문장이 이어지는 블록을 체인으로 묶는다."""
    cols = _col_centers(blocks)

    def col_of(x0):
        return min(range(len(cols)), key=lambda i: abs(x0 - cols[i]))

    order = sorted(range(len(blocks)),
                   key=lambda i: (col_of(blocks[i]["rect"].x0),
                                  blocks[i]["rect"].y0))
    chains: list[list[int]] = []
    cur: list[int] = []
    for i in order:
        b = blocks[i]
        if _skip(b, page_h):
            if cur:
                chains.append(cur)
                cur = []
            continue
        if cur:
            prev = blocks[cur[-1]]
            same_style = abs(prev["size"] - b["size"]) < 3   # 굵기 교차 허용
            cont = not TERMINAL_RE.search(prev["text"].rstrip())
            if not (cont and same_style):
                chains.append(cur)
                cur = []
        cur.append(i)
    if cur:
        chains.append(cur)
    return chains


# ---------------------------------------------------------------------------
# 3. 번역 (translate_chunk 재사용 + 해시 resume)
# ---------------------------------------------------------------------------
def _chain_key(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def translate_chains(texts: list[str], cfg: dict, gloss: str, cache: dict,
                     log, is_cancelled) -> dict[int, str | None]:
    """전 페이지 체인 원문 목록 → {체인 전역 인덱스: 한국어}. cache 갱신."""
    todo = [(i, t) for i, t in enumerate(texts)
            if _chain_key(t) not in cache]
    done_n = len(texts) - len(todo)
    # 요금이 왜 또 나갔는지 바로 알 수 있게 캐시 상황을 항상 남긴다
    log(f"번역 캐시 {done_n}/{len(texts)}문단 재사용 — "
        f"{len(todo)}문단 새로 번역 (캐시 파일: _work/pdfxlat.json)")
    batch: list[tuple[int, str]] = []
    chars = 0
    missing = 0                      # 응답에 안 담겨 온 문단 수(다음 실행 재시도)

    def flush():
        nonlocal batch, chars, missing
        if not batch:
            return
        got = core.translate_chunk(batch, cfg, gloss, "",
                                   tmpl=PROMPT_XLAT_DOC)
        ok = 0
        for gi, ko in got.items():
            if ko is not None:
                cache[_chain_key(texts[gi])] = ko
                ok += 1
        missing += len(batch) - ok
        batch, chars = [], 0

    for n, (i, t) in enumerate(todo, 1):
        if is_cancelled():
            raise core.Cancelled()
        batch.append((i, t))
        chars += len(t)
        if chars >= core.CHUNK_CHARS:
            flush()
            log(f"번역 {done_n + n}/{len(texts)}문단")
    flush()
    if todo:
        log(f"번역 {len(texts)}/{len(texts)}문단")
    if missing:
        log(f"!! {missing}문단이 응답에 없어 캐시에 저장되지 않았습니다 — "
            "원문이 그대로 남고, 다시 실행하면 이 문단만 재번역합니다 "
            "(그래서 재실행에도 요금이 조금 나옵니다)")
    out: dict[int, str | None] = {}
    for i, t in enumerate(texts):
        out[i] = cache.get(_chain_key(t))
    return out


# ---------------------------------------------------------------------------
# 4. 재삽입
# ---------------------------------------------------------------------------
def _font_family(fontname: str) -> str:
    if "Serif" in fontname:
        return "ridi"
    if "Bold" in fontname or "Black" in fontname or "Heavy" in fontname:
        return "dream6"
    return "dream3"


def _font_archive(pymupdf):
    arch = pymupdf.Archive()
    for alias, fname in (FONT_SERIF, FONT_BOLD, FONT_SANS):
        p = FONT_DIR / fname
        if not p.exists():
            raise RuntimeError(f"폰트가 없습니다: {p}")
        arch.add(p.read_bytes(), alias)
    return arch


def split_by_weight(text: str, weights: list[float]) -> list[str]:
    """번역문을 블록 면적 비례로 어절 경계에서 분할."""
    total = sum(weights) or 1.0
    words = text.split(" ")
    parts: list[str] = []
    wi = 0
    target = 0.0
    for k, wt in enumerate(weights):
        target += wt / total * len(words)
        if k == len(weights) - 1:
            parts.append(" ".join(words[wi:]))
            break
        cut = max(wi + 1, min(len(words), round(target)))
        parts.append(" ".join(words[wi:cut]))
        wi = cut
    return parts


def _render_page(pymupdf, page, jobs, arch, scale_of) -> dict:
    """한 페이지 재조판 — jobs=[(체인id, 블록, 한글조각)]. 배율 기록 반환."""
    for _, b, _t in jobs:
        page.add_redact_annot(b["rect"], fill=False)
    page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                          graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)
    scales: dict = {}
    for ci, b, part in jobs:
        fam = _font_family(b["font"])
        size = b["size"] * SIZE_FACTOR * scale_of(ci)
        htm = (f'<p style="font-family:{fam}; font-size:{size:.1f}px; '
               f'color:#{b["color"]:06x};">{html.escape(part)}</p>')
        try:
            _, sc = page.insert_htmlbox(b["rect"], htm, css=CSS, archive=arch)
        except Exception:
            sc = 1.0
        scales[ci] = min(sc, scales.get(ci, 1.0))
    return scales


# ---------------------------------------------------------------------------
# 실행 진입점
# ---------------------------------------------------------------------------
def resolve_out(cfg: dict) -> tuple[Path, str]:
    return core.resolve_out(cfg)          # <제목>_한글번역/ 폴더 규칙 공유


# ---------------------------------------------------------------- 저장 보호
# 공용 헬퍼는 comic_retype_pipeline(retype)에 있다 — 코믹스 ZIP·이북
# TXT/EPUB과 같은 규칙을 쓴다. 여기서는 PDF 사정만 덧붙인다.
def check_output_free(cfg: dict, log=None) -> None:
    """번역을 시작하기 전에 출력 PDF가 열려 있는지 확인.

    조판까지 다 끝낸 뒤 저장에서 막히면 API 요금만 날아간다
    (2026-08-08 Spiegel 8호 68페이지 — 'cannot remove file … Permission
    denied'로 $0.22어치 결과를 잃을 뻔함). 그래서 돈 쓰기 전에 본다.
    """
    try:
        out, title = resolve_out(cfg)
        dst = out / f"{title}_ko.pdf"
    except Exception:
        return                                # 경로를 못 정하면 검사 생략
    if retype.is_locked(dst):
        raise RuntimeError(
            f"출력 PDF가 다른 프로그램에서 열려 있습니다:\n    {dst}\n"
            "PDF 뷰어(Acrobat·Edge·크롬 등)에서 닫은 뒤 다시 실행하세요. "
            "번역 결과는 캐시에 남아 있어 다시 돌려도 요금은 들지 않습니다.")


def _save_pdf(final, dst: Path, log) -> Path:
    """서브셋 임베딩 후 임시 파일 → 교체 (막히면 '이름 (2).pdf')."""
    try:
        final.subset_fonts()
    except Exception:
        pass

    def _write(target):
        final.ez_save(str(target))
        final.close()
    return retype.safe_produce(dst, _write, log)


def run_pdf_doc(cfg: dict, log, is_cancelled) -> dict:
    """문서 모드 전체 파이프라인. cfg 키는 run_book과 동일 서브셋:
    src(.pdf), out, title, source_lang, backend(+모델·키), glossary,
    page_range, api_key."""
    pymupdf = _pymupdf()
    src = Path(cfg["src"])
    if src.suffix.lower() != ".pdf":
        raise RuntimeError("문서 모드 소스는 PDF 파일이어야 합니다")
    kind, total = core.probe_source(src)
    if kind != "pdf-text":
        raise RuntimeError(
            "텍스트 레이어가 없는 스캔 PDF입니다 — 문서 모드는 텍스트 PDF "
            "전용입니다. 스캔 문서는 '책' 모드(TXT/EPUB)를 쓰세요.")
    out, title = resolve_out(cfg)
    out.mkdir(parents=True, exist_ok=True)
    check_output_free(cfg, log)      # 요금 쓰기 전에 출력 파일 잠김 확인
    work = out / "_work"
    work.mkdir(exist_ok=True)

    core._apply_keys(cfg)
    if cfg.get("backend") == "claude" and not os.environ.get(
            "ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY 가 없습니다 — 키를 입력하거나 "
                           "번역 엔진 Ollama(완전 로컬)를 쓰세요.")

    pages = core.list_source_pages(src, kind)
    if (cfg.get("page_range") or "").strip():
        pages = retype.apply_page_range(pages, cfg["page_range"])
        log(f"페이지 범위 {cfg['page_range'].strip()} → {len(pages)}페이지")
    log(f"소스: 텍스트 PDF {total}페이지 중 {len(pages)}페이지 — "
        f"문서 모드(레이아웃 유지), 제목 '{title}'")

    # ---- 추출·체인 ----
    doc = pymupdf.open(str(src))
    lang = cfg.get("source_lang") or "auto"
    page_jobs: list[tuple[int, list, list]] = []   # (페이지, 블록들, 체인들)
    chain_texts: list[str] = []
    sample = ""
    for p in pages:
        if is_cancelled():
            raise core.Cancelled()
        page = doc[p]
        blocks = extract_blocks(pymupdf, page)
        chains = chain_blocks(blocks, page.rect.height)
        page_jobs.append((p, blocks, chains))
        for ch in chains:
            t = dehyph(" ".join(blocks[i]["text"] for i in ch), lang)
            chain_texts.append(t)
            if len(sample) < 2000:
                sample += t + "\n"
    if lang == "auto":
        lang, scores = core.detect_lang(sample)
        cfg["source_lang"] = lang
        log(f"언어 자동 감지: {core.LANG_NAMES.get(lang, lang)} {scores}")
    log(f"블록 체인 {len(chain_texts)}문단 추출")

    # ---- 번역 (해시 resume: _work/pdfxlat.json) ----
    cache_path = work / "pdfxlat.json"
    cache = core._read_json(cache_path) or {}
    gloss = core._load_glossary(cfg.get("glossary"), out)
    try:
        kos = translate_chains(chain_texts, cfg, gloss, cache,
                               log, is_cancelled)
    finally:
        core._atomic_json(cache_path, cache)
    failed = sum(1 for v in kos.values() if v is None)
    if failed:
        log(f"!! {failed}문단 번역 실패 — 원문 유지 (재실행하면 재시도)")

    # ---- 재조판 (2패스: 스크래치에서 배율 측정 → 균일 적용) ----
    arch = _font_archive(pymupdf)

    def build_jobs(blocks, chains, base_ci):
        jobs = []
        for k, ch in enumerate(chains):
            ko = kos.get(base_ci + k)
            if ko is None:
                continue                        # 실패 문단은 원문 그대로
            weights = [blocks[i]["rect"].get_area() for i in ch]
            parts = (split_by_weight(ko, weights) if len(ch) > 1 else [ko])
            for i, part in zip(ch, parts):
                jobs.append((base_ci + k, blocks[i], part))
        return jobs

    all_scales: dict = {}
    for pass_no, d in ((1, pymupdf.open(str(src))), (2, doc)):
        ci = 0
        for n, (p, blocks, chains) in enumerate(page_jobs, 1):
            if is_cancelled():
                raise core.Cancelled()
            jobs = build_jobs(blocks, chains, ci)
            ci += len(chains)
            got = _render_page(
                pymupdf, d[p], jobs, arch,
                (lambda _ci: 1.0) if pass_no == 1
                else (lambda _ci: all_scales.get(_ci, 1.0)))
            if pass_no == 1:
                for k, v in got.items():
                    all_scales[k] = min(v, all_scales.get(k, 1.0))
            else:
                log(f"조판 {n}/{len(page_jobs)}페이지")
        if pass_no == 1:
            d.close()

    # ---- 저장 ----
    dst = out / f"{title}_ko.pdf"
    final = pymupdf.open()
    for p in pages:
        final.insert_pdf(doc, from_page=p, to_page=p)
    doc.close()
    dst = _save_pdf(final, dst, log)
    log(f"저장: {dst}")
    return {"out": str(out), "pdf": str(dst),
            "chains": len(chain_texts), "failed": failed}


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        description="텍스트 PDF 문서 → 레이아웃 유지 한글 PDF 번역")
    ap.add_argument("src", help="PDF 파일")
    ap.add_argument("--range", dest="page_range", default="",
                    help="페이지 범위 (예: 5-20)")
    ap.add_argument("--backend", default="",
                    help="번역 엔진 claude/gemini/kimi/ollama "
                         "(비면 저장된 설정)")
    ap.add_argument("--title", default="", help="제목 (비면 파일명)")
    a = ap.parse_args(argv)
    cfg = core.load_defaults()
    cfg.update({k: v for k, v in
                dict(src=a.src, page_range=a.page_range,
                     backend=a.backend, title=a.title).items() if v})
    cfg.setdefault("backend", "claude")
    try:
        run_pdf_doc(cfg, print, lambda: False)
    finally:
        u = retype.usage_summary()
        if u:
            print(u)


if __name__ == "__main__":
    main()
