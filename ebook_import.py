#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ebook_import.py — 평문 텍스트(txt/md) → Fokus Viewer 책(book.json) 가져오기.

라이브러리 서고(_Ebook_Library 등)의 txt는 1995~2006년 PC통신 시절 파일이라
① 한 줄이 ~40자에서 강제로 잘린 '하드 줄바꿈'(대부분의 한국 책)과
② 한 줄이 한 문단인 파일(번역 라이트노벨·중국어 소설·青空文庫)이 섞여 있다.
이 모듈은 파일마다 구조를 자동 판별해 문단 배열로 재구성하고,
번역 파이프라인과 같은 book.json 형식(원문 src 슬롯, 번역 비움, kind="plain")
으로 저장한다. 이후 읽기·교정·북마크·하이라이트·PC↔폰 동기화는 번역책과
완전히 같은 경로를 탄다.

하드 줄바꿈 재구성 규칙(실측 — Jude1·우동 한그릇·거울의길·돈의 역사):
  · 새 문단  = 들여쓰기가 '본문 이어쓰기 들여쓰기'(최빈값)보다 깊은 줄,
               빈 줄 다음 줄, 또는 직전 줄이 짧은 줄(p95의 60% 미만)의 다음 줄
  · 줄 잇기  = 직전 줄이 공백으로 끝나면 단어 경계 → 띄어 잇고,
               공백 없이 끝나면 단어가 잘린 것("혹평했다. 하" + "지만") → 붙여 잇는다.
               (줄 끝 공백이 보존된 파일에서만; 아니면 한글·라틴은 띄우고 CJK는 붙임)
  · 정렬용 이중 공백은 하나로

사용:  python ebook_import.py <파일.txt> [--title 제목] [--out 폴더] [--split auto|wrap|lines]
       → <파일 폴더>/<제목>_book/_work/book.json (+ xlat.json, <제목>_src.txt)
"""
from __future__ import annotations

import collections
import html as _html
import json
import os
import re
import sys
from pathlib import Path
from typing import Callable, Optional

import comic_retype_pipeline as retype

HEAD_MARK = "## "          # ebook_translate._HEAD_MARK 과 동일 (출력 시 제거)
SUB_MARK = "### "          # 소제목 (ebook_translate._SUB_MARK) — 장 분할 안 함
_MD_HEAD = re.compile(r"^(#{1,6})[ \t]+(\S.*)$")   # 마크다운 제목 줄
SCHEMA_VER = 1

# ---------------------------------------------------------------------------
# 1. 읽기 — 인코딩 판별
# ---------------------------------------------------------------------------
_GOOD_CH = re.compile(
    r"[가-힣぀-ヿ一-鿿　-〿！-｠"
    r"A-Za-z0-9 \n\r\t.,!?'\"()\-:;…·「」『』‘’“”、。]")


def _score(t: str) -> float:
    n = max(1, len(t))
    good = sum(1 for c in t if _GOOD_CH.match(c))
    return good / n - t.count("�") * 0.01


def read_text_any(path: Path) -> tuple[str, str]:
    """BOM → utf-8 → 후보 인코딩 중 문자 분포 점수 최대. (text, 인코딩명)"""
    b = Path(path).read_bytes()
    if b[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return b.decode("utf-16"), "utf-16"
    if b[:3] == b"\xef\xbb\xbf":
        return b[3:].decode("utf-8", "replace"), "utf-8-sig"
    try:
        return b.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    best = None
    for enc in ("cp949", "shift_jis", "euc-jp", "big5", "gb18030", "cp1252"):
        try:
            t = b.decode(enc, "replace")
        except LookupError:
            continue
        s = _score(t[:80000])
        if best is None or s > best[0]:
            best = (s, t, enc)
    return best[1], best[2]


# ---------------------------------------------------------------------------
# 2. 전처리 — HTML 껍데기, 青空文庫 주석
# ---------------------------------------------------------------------------
_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(t: str) -> str:
    """txt 안에 HTML이 들어 있는 파일(357.txt 류) — 블록 태그를 개행으로."""
    t = re.sub(r"(?is)<(script|style|head)\b.*?</\1>", "", t)
    t = re.sub(r"(?i)<br\s*/?>", "\n", t)
    t = re.sub(r"(?i)</(p|div|h[1-6]|li|tr)>", "\n\n", t)
    t = re.sub(r"(?i)<(p|div|h[1-6]|li|tr)\b[^>]*>", "\n", t)
    t = _TAG_RE.sub("", t)
    return _html.unescape(t)


def looks_html(t: str) -> bool:
    head = t[:4000].lower()
    return ("<html" in head or "<body" in head
            or len(re.findall(r"<(p|br|div)\b", head)) >= 3)


_AOZ_RUBY_BAR = re.compile(r"｜([^《｜]+)《([^》]+)》")
_AOZ_RUBY_AUTO = re.compile(r"([一-鿿々〆ヶ]+)《([^》]+)》")
_AOZ_NOTE = re.compile(r"［＃[^］]*］")
_AOZ_HEAD = re.compile(r"［＃「([^」]+)」は([大中小])見出し］")


def aozora_clean(t: str, ruby: str = "strip") -> str:
    """青空文庫 기호 정리. ruby: strip(읽기 제거) | paren(漢字(かな)) | keep."""
    if "《" not in t and "［＃" not in t:
        return t
    # 머리 설명부(【テキスト中に現れる記号について】…) 제거
    t = re.sub(r"-{20,}\n【テキスト中に現れる記号について】.*?-{20,}\n", "",
               t, flags=re.S)
    if ruby == "strip":
        t = _AOZ_RUBY_BAR.sub(r"\1", t)
        t = _AOZ_RUBY_AUTO.sub(r"\1", t)
    elif ruby == "paren":
        t = _AOZ_RUBY_BAR.sub(r"\1(\2)", t)
        t = _AOZ_RUBY_AUTO.sub(r"\1(\2)", t)
    # 見出し 주석 → 제목 마커 (주석이 해당 줄 뒤에 붙는 형식)
    def _head(m):
        return ""
    # 大·中見出し → 장 제목, 小見出し → 소제목
    heads = {m.group(1): (SUB_MARK if m.group(2) == "小" else HEAD_MARK)
             for m in _AOZ_HEAD.finditer(t)}
    t = _AOZ_HEAD.sub(_head, t)
    t = t.replace("［＃改ページ］", "\n\n").replace("［＃改頁］", "\n\n")
    t = _AOZ_NOTE.sub("", t)
    if heads:
        lines = t.split("\n")
        for i, ln in enumerate(lines):
            s = ln.strip("　 \t")
            if s in heads and not s.startswith(HEAD_MARK):
                lines[i] = heads[s] + s
        t = "\n".join(lines)
    return t


# ---------------------------------------------------------------------------
# 3. 구조 분석
# ---------------------------------------------------------------------------
_TERM_RE = re.compile(r"[.!?。！？…\"”」』』\)\]]\s*$")
_CJK_NOSPACE = re.compile(r"[぀-ヿ一-鿿㐀-䶿"
                          r"！-｠　-〿]")


def _lead(line: str) -> int:
    n = 0
    for c in line:
        if c == " ":
            n += 1
        elif c == "　":
            n += 2
        elif c == "\t":
            n += 4
        else:
            break
    return n


def analyze(lines: list[str]) -> dict:
    nb = [l for l in lines if l.strip()]
    if not nb:
        return {"mode": "lines", "p95": 0, "term": 0, "trail": 0,
                "indent_mode": 0, "blank": 0}
    lens = sorted(len(l.strip()) for l in nb)
    p95 = lens[int(len(lens) * 0.95) - 1] if len(lens) > 1 else lens[0]
    term = sum(1 for l in nb if _TERM_RE.search(l)) / len(nb)
    trail = sum(1 for l in nb if l != l.rstrip(" \t")) / len(nb)
    # 본문 '이어쓰기' 들여쓰기 = 8% 이상 나타나는 들여쓰기 중 가장 얕은 것.
    # (최빈값을 쓰면 '모든 새 문단이 2칸, 이어쓰기는 0칸'인 파일에서
    #  2칸이 기준이 돼 문단이 전부 붙어 버린다 — 우동 한그릇 사례)
    leads = collections.Counter(_lead(l) for l in nb)
    common = [k for k, c in leads.items() if c / len(nb) >= 0.08]
    indent_mode = min(common) if common else leads.most_common(1)[0][0]
    blank = 1 - len(nb) / max(1, len(lines))
    # 한 줄=한 문단: 문장부호로 끝나는 줄이 많거나, 긴 줄(p95>120)이 흔하면
    mode = "lines" if (term >= 0.72 or p95 > 120) else "wrap"
    return {"mode": mode, "p95": p95, "term": round(term, 2),
            "trail": round(trail, 2), "indent_mode": indent_mode,
            "blank": round(blank, 2)}


def detect_lang(t: str) -> str:
    s = t[:20000]
    ko = len(re.findall(r"[가-힣]", s))
    ja = len(re.findall(r"[぀-ヿ]", s))
    zh = len(re.findall(r"[一-鿿]", s))
    la = len(re.findall(r"[A-Za-z]", s))
    best = max((ko, "ko"), (ja, "ja"), (zh, "zh"), (la * 0.5, "en"))
    return best[1] if best[0] > 20 else "ko"


# ---------------------------------------------------------------------------
# 4. 문단 재구성
# ---------------------------------------------------------------------------
_MULTI_WS = re.compile(r"[ \t　]{2,}")


# ── 한국어 줄 잇기 단서 (재구성·자동 교정 공용) ─────────────────────────
# 줄 첫머리가 '홀로 설 수 없는 조각'(조사·어미)이면 앞 줄 끝 낱말이 잘린 것 →
# 띄어쓰기 없이 붙인다. ('이'·'가'·'다'·'아'·'어'는 지시어·동사·감탄사로도
# 쓰여 제외 — 애매한 것은 AI 판정 단계로)
KO_FRAG = frozenset(
    "은 는 을 를 의 에 와 과 로 도 만 며 면 서 던 게 니 고 랑 요 죠 지만 "
    "에서 에게 으로 까지 부터 처럼 보다 마저 조차 하고는 이나 이며 이고 이란 "
    "이라 이라고 이라는 이었다 였다 었다 았다 했다 한다 합니다 습니다 니다 "
    "었던 았던 겠다 겠지 는데 은데 는지 으며 으면 어서 아서 어도 아도 "
    "어요 아요 세요 는다 ㄴ다 다고 다는 다며 라고 라는 라며 지요 네요 군요".split())
# 이 글자로 끝난 줄은 낱말 중간에서 잘린 것 (어절 끝에 올 수 없는 꼴)
KO_CUT_END = ("습니", "합니", "됩니", "십니", "입니", "옵니", "갑니", "봅니",
              "었", "았", "였", "겠", "했")
_OPEN_Q = tuple('"“\'‘「『[<〈《(')
_CLOSE_DLG = re.compile(r"[.!?…~]\s*[\"”’'」』\]]\s*$")
_SENT_TERM = re.compile(r"[.!?。！？…~]\s*[\"”’'」』\])]*\s*$|[\"”’'」』\]]\s*$")
_QUOTATIVE = re.compile(r"^(하고|라고|라며|하며|하면서|하자|하니|하는|고\s|며\s|라는)")


# ── 원본 손상(깨진 글자) 감지 ─────────────────────────────────────────
# 2006년 txt 아카이브 일부는 파일 자체가 손상돼 한글 사이에 엉뚱한 한자·기호가
# 박혀 있다("크리스마만찬銖?때맨毬ち?"). 바이트를 다시 맞춰도 복구되지 않으므로
# (글자 일부가 이미 사라짐) 경고만 하고, 자동 교정·AI 판정 대상에서 뺀다.
_GJUNK = re.compile(r"[\ue000-\uf8ff\u2500-\u257f\u3200-\u33ff\u2460-\u24ff"
                    r"\u2190-\u21ff\u2200-\u22ff\u3040-\u30ff]")
_GHAN = re.compile(r"(?<=[가-힣?])[\u4e00-\u9fff\uf900-\ufaff](?=[가-힣?])")


def is_garbled(s: str, lang: str = "ko") -> bool:
    """깨진 글자가 6% 넘게 섞인 줄·문단인가 (한국어 책 기준 — 괄호 안 한자
    병기·「」 인용은 제외). 일본어·중국어 책은 사용자 정의 영역 문자만 본다."""
    t = re.sub(r"\([^()]{0,30}\)", "", s or "")
    t = re.sub(r"[「『][^」』]{0,40}[」』]", "", t)
    n = len(t.strip())
    if n < 6:
        return False
    if lang not in ("ko", "", None):
        return len(re.findall(r"[\ue000-\uf8ff]", t)) / n > 0.06
    junk = len(_GJUNK.findall(t)) + len(_GHAN.findall(t))
    junk += 0.5 * len(re.findall(r"[가-힣]\?[가-힣]", t))
    return junk / n > 0.06


def damage(paras: list, lang: str = "ko") -> dict:
    """문단 목록의 손상 정도 — {ratio, n, first(첫 손상 문단 번호, 연속 구간 기준)}."""
    bad = [is_garbled(p, lang) for p in paras]
    n = sum(bad)
    first = None
    for i in range(len(bad)):
        w = bad[i:i + 10]
        if w and w[0] and sum(w) >= min(6, len(w)):
            first = i
            break
    return {"ratio": round(n / max(1, len(paras)), 3), "n": n, "first": first}


def first_tok(s: str) -> str:
    m = re.match(r"[가-힣]+", s or "")
    return m.group(0) if m else ""


def ko_nospace(a: str, b: str) -> bool:
    """a 끝과 b 첫머리 사이를 띄어 쓰지 않아야 하는가 (잘린 낱말)."""
    if not a or not b or not re.match(r"[가-힣]", a[-1]):
        return False
    t = first_tok(b)
    if t and t in KO_FRAG and (len(b) == len(t) or not re.match(r"[가-힣]", b[len(t)])):
        return True
    return a.endswith(KO_CUT_END) and bool(t)


def is_term(s: str) -> bool:
    """문장이 끝났나 (종결부호, 닫는 따옴표·괄호 포함)."""
    return bool(_SENT_TERM.search(s or ""))


def _norm(s: str) -> str:
    return _MULTI_WS.sub(" ", s.strip(" \t　"))


def _join(prev: str, prev_trail: bool, nxt: str, use_trail: bool) -> str:
    """두 줄 잇기 — 줄 끝 공백 신호가 있으면 그것을, 없으면 문자 종류로."""
    if not prev:
        return nxt
    if not nxt:
        return prev
    if use_trail:
        sep = " " if prev_trail else ""
    else:
        a, b = prev[-1], nxt[0]
        sep = "" if (_CJK_NOSPACE.match(a) and _CJK_NOSPACE.match(b)) else " "
        if sep and ko_nospace(prev, nxt):      # "걱정" + "을 완전히" → 걱정을
            sep = ""
    return prev + sep + nxt


def reflow(text: str, split: str = "auto") -> tuple[list[str], dict]:
    """텍스트 → 문단 리스트. split: auto | wrap(하드 줄바꿈 재구성) | lines | blank(빈 줄 기준)."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    info = analyze(lines)
    mode = info["mode"] if split == "auto" else split
    info["used"] = mode
    paras: list[str] = []

    if mode == "lines":
        for ln in lines:
            s = _norm(ln)
            if s:
                paras.append(s)
        return paras, info

    if mode == "blank":
        # 빈 줄 = 문단 경계, 문단 안 줄바꿈은 그대로 — `_src.txt` 왕복용
        # (텍스트 편집기에서 빈 줄을 넣고 빼서 문단을 나누고 합친 뒤 재가져오기)
        buf: list[str] = []
        for ln in lines:
            if ln.strip():
                buf.append(ln.rstrip())
            elif buf:
                paras.append("\n".join(buf).strip()); buf = []
        if buf:
            paras.append("\n".join(buf).strip())
        return paras, info

    p95 = max(1, info["p95"])
    short_cut = p95 * 0.6
    long_cut = p95 * 0.75
    frag_cut = max(3, p95 * 0.12)
    base = info["indent_mode"]
    use_trail = info["trail"] >= 0.15
    cur, cur_trail, prev_len, prev_head = "", False, None, False
    prev_s, soft = "", False
    # 이어쓰기 들여쓰기 기준을 '최근 60줄'에서 다시 잡는다 — 여러 게시물을 이어
    # 붙인 파일은 구간마다 형식이 다르다(새 문단 4칸/이어쓰기 2칸 ↔ 2칸/1칸)
    win = collections.deque(maxlen=60)
    for ln in lines:
        s = _norm(ln)
        if not s:                                   # 빈 줄 → 문단 끝
            # 단, 꽉 찬 줄이 문장 중간에서 끝났으면 빈 줄은 줄 간격(더블
            # 스페이스·페이지 경계) — 다음 줄이 이어지는지 보고 결정 (soft)
            if cur and prev_len is not None and prev_len >= long_cut \
                    and not is_term(prev_s) and not prev_head:
                soft = True
                continue
            if cur:
                paras.append(cur)
            cur, cur_trail, prev_len, prev_head = "", False, None, False
            prev_s, soft = "", False
            continue
        lead = _lead(ln)
        win.append(lead)
        if len(win) >= 20:
            cnt = collections.Counter(win)
            com = [k for k, c in cnt.items() if c >= 0.15 * len(win)]
            base = min(com) if com else info["indent_mode"]
        is_head = bool(_MD_HEAD.match(s))
        if soft and (lead > base or s.startswith(_OPEN_Q) or is_head):
            paras.append(cur)                       # 이어지지 않음 → 진짜 문단 끝
            cur, cur_trail, prev_len, prev_head, prev_s = "", False, None, False, ""
        soft = False
        # 이중 줄바꿈 조각("서비" / "스" / "(용역)다.") — 꽉 찬 줄 뒤 아주 짧은
        # 미종결 줄은 이어쓰기, 그 짧음은 문단 끝 신호로 쓰지 않는다
        frag = (cur and prev_len is not None and prev_len >= long_cut
                and len(s) <= frag_cut and not is_term(prev_s) and not is_term(s)
                and lead <= base)
        dlg = (cur and prev_s and (
            (is_term(prev_s) and s.startswith(_OPEN_Q))          # 서술 → 대사
            or (_CLOSE_DLG.search(prev_s) and re.match(r"[가-힣A-Za-z]", s)
                and not _QUOTATIVE.match(s))))                    # 대사 → 서술
        new_para = (not frag) and (
            not cur or lead > base
            or (prev_len is not None and prev_len < short_cut)
            or is_head or prev_head or dlg)
        if new_para:
            if cur:
                paras.append(cur)
            cur = s
        else:
            cur = _join(cur, cur_trail, s, use_trail)
        cur_trail = ln != ln.rstrip(" \t　")
        prev_len = (max(prev_len or 0, int(long_cut)) if frag
                    else len(ln.strip()))
        prev_head = is_head                  # 제목 줄 다음은 늘 새 문단
        prev_s = s
    if cur:
        paras.append(cur)
    return paras, info


# ---------------------------------------------------------------------------
# 5. 제목 표시
# ---------------------------------------------------------------------------
_HEAD_KO = re.compile(
    r"^(제\s*[\d０-９一二三四五六七八九十百]+\s*[장부편회화권절막]|"
    r"프롤로그|에필로그|서장|종장|서문|서시|후기|작가의\s*말|목차|들어가(며|는 글)|"
    r"나오(며|는 글)|맺음말|머리말|\d{1,3}\s*[.장화]\s*\S)")
_HEAD_JA = re.compile(
    r"^(第[〇一二三四五六七八九十百千万0-9０-９]+[章話部巻節幕]"
    r"|序章|終章|序|プロローグ|エピローグ|まえがき|あとがき|目次)")
_HEAD_ZH = re.compile(r"^(第[〇一二三四五六七八九十百千万0-9０-９]+[章回節卷集部]|楔子|序章|尾聲|後記|番外)")
_HEAD_EN = re.compile(
    r"^(kapitel|chapter|teil|part|prolog|prologue|epilog|epilogue|buch|book)"
    r"\b[\s\d.:IVXLC-]*", re.I)
_SENT_END = tuple('.!?"»«“”’:;' + '。」』？！…')


def mark_headings(paras: list[str]) -> tuple[list[str], int]:
    """장 제목 → '## ', 마크다운 제목은 단계 유지(#·## → '## ', ###~ → '### ' 소제목).
    소제목 자동 판별은 하지 않는다 — 짧은 대사 오인이 많아 뷰어의
    「소제목 후보 찾기」에서 사람이 확정."""
    out, n = [], 0
    for p in paras:
        s = p.strip()
        m = _MD_HEAD.match(s) if "\n" not in s else None
        if m:
            out.append((SUB_MARK if len(m.group(1)) >= 3 else HEAD_MARK)
                       + m.group(2).strip()); n += 1; continue
        if (len(s) <= 40 and not s.endswith(_SENT_END)
                and (_HEAD_KO.match(s) or _HEAD_JA.match(s)
                     or _HEAD_ZH.match(s) or _HEAD_EN.match(s))):
            out.append(HEAD_MARK + s); n += 1
        else:
            out.append(s)
    return out, n


# ---------------------------------------------------------------------------
# 6. 저장 — book.json (번역 파이프라인과 동일 형식)
# ---------------------------------------------------------------------------
def _atomic_json(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    os.replace(tmp, path)


def default_out(src: Path, title: str) -> Path:
    """_book 폴더 위치 — 규칙은 core.plain_book_dir 한 곳 (서고 안이면 _books/)."""
    import ebook_translate as core      # 지연 import (core가 무거움)
    return core.plain_book_dir(src, title)


TEXT_EXTS = (".txt", ".md", ".markdown")
LIB_DIRNAME = "_Ebook_Library"


def natural_key(s: str):
    """k2 < k10 · 1권 < 10권 — 숫자는 수로 비교."""
    return [(0, int(t), "") if t.isdigit() else (1, 0, t.lower())
            for t in re.split(r"(\d+)", s) if t]


def list_texts(d: Path, recursive: bool = True) -> list[Path]:
    """폴더 안 텍스트 파일(자연 정렬, 하위 폴더 포함). _book 폴더는 제외."""
    d = Path(d)
    it = d.rglob("*") if recursive else d.iterdir()
    fs = [p for p in it if p.is_file() and p.suffix.lower() in TEXT_EXTS
          and not any(x.endswith("_book") or x == "_books"
                      for x in p.relative_to(d).parts[:-1])
          and not p.name.endswith("_src.txt")]
    return sorted(fs, key=lambda p: natural_key(str(p.relative_to(d))))


def default_title(src) -> str:
    """기본 책 제목 — 폴더는 폴더명, 파일은 '폴더에 텍스트가 그 하나뿐'이면
    폴더명(서고엔 k1216.txt·LANT1127.TXT 같은 이름이 많다), 아니면 파일명."""
    src = Path(src)
    if src.is_dir():
        return src.name
    try:
        sib = [p for p in src.parent.iterdir()
               if p.is_file() and p.suffix.lower() in TEXT_EXTS]
    except OSError:
        sib = [src]
    pn = src.parent.name.strip()
    # 분류 폴더는 '[소설]'·'[ 기   타 ]'처럼 이름 전체가 괄호 — 책 폴더는
    # '[저자]제목' 형태라 괄호 뒤에 글이 있다
    if len(sib) == 1 and pn and not re.fullmatch(r"\[[^\]]*\]", pn) \
            and pn != LIB_DIRNAME:
        return src.parent.name
    return src.stem


def _text_to_paras(path: Path, split: str, ruby: str, log):
    text, enc = read_text_any(path)
    if looks_html(text):
        text = strip_html(text)
        log("HTML 껍데기 제거")
    text = aozora_clean(text, ruby=ruby)
    paras, info = reflow(text, split=split)
    paras, nhead = mark_headings(paras)
    return text, enc, paras, info, nhead


def _autofix(paras: list, p95, mode: str, cfg, log,
             lang: str = "ko") -> tuple[list, Optional[dict], dict]:
    """가져오기 직후 자동 교정 (ebook_autofix). mode: off | rule | ai.
    반환 (교정된 문단, af 기록 or None, 통계)."""
    if mode not in ("rule", "ai") or not paras:
        return paras, None, {}
    import ebook_autofix as af
    book = {"paras": [{"src": p, "page": ""} for p in paras],
            "origin": {"p95": p95}, "source_lang": lang or "ko"}
    st = af.run(book, {}, mode, cfg or {}, log)
    return [e["src"] for e in book["paras"]], book.get("af"), st


def _save_book(out: Path, title: str, lang: str, paras: list, origin: dict,
               log, resplit_src=None, af_log: Optional[dict] = None) -> None:
    work = out / "_work"
    work.mkdir(parents=True, exist_ok=True)
    book = {"title": title, "source_lang": lang, "kind": "plain",
            "page_labels": [],
            "paras": [{"src": p, "page": ""} for p in paras],
            "origin": origin}
    if af_log:
        book["af"] = af_log
    bp = work / "book.json"
    if bp.exists():     # 재가져오기 — 북마크·위치·표지 등 사용자 상태는 보존
        # 편집 페이지에서 고친 본문·제목 지정은 새 재구성으로 덮이므로 사본을 남긴다
        try:
            import datetime
            import shutil
            bak = work / ("reimport_" +
                          datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
            bak.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(bp), str(bak / "book.json"))
            log(f"이전 book.json 사본 → _work/{bak.name}/")
        except Exception as e:
            log(f"(이전 book.json 사본 실패: {e})")
        try:
            old = json.loads(bp.read_text(encoding="utf-8"))
            for k in ("bmks", "pos", "off", "pos_ts", "cover", "hi", "hi_ts"):
                if k in old:
                    book[k] = old[k]
            if resplit_src and isinstance(old.get("origin"), dict):
                book["origin"] = dict(old["origin"], resplit=str(resplit_src))
                book["source_lang"] = old.get("source_lang") or lang
        except Exception:
            pass
    _atomic_json(bp, book)
    xp = work / "xlat.json"
    if not xp.exists():
        _atomic_json(xp, {})
    retype.safe_write_text(out / f"{title}_src.txt",
                           "\n\n".join(paras) + "\n", log)


def import_text(src, out=None, title: Optional[str] = None,
                lang: str = "auto", split: str = "auto", ruby: str = "strip",
                log: Callable[[str], None] = print,
                autofix: str = "off", cfg: Optional[dict] = None) -> dict:
    src = Path(src)
    if src.is_dir():
        return import_folder(src, out, title, lang, split, ruby, log,
                             autofix=autofix, cfg=cfg)
    if not src.is_file():
        raise FileNotFoundError(str(src))
    from_src = src.name.endswith("_src.txt")
    if from_src:                       # <제목>_src.txt 재가져오기 — 문단 구조 왕복
        title = (title or "").strip() or src.name[:-len("_src.txt")]
        out = Path(out) if out else src.parent        # 이미 _book 폴더 안
        if split == "auto":
            split = "blank"
    else:
        title = (title or "").strip() or default_title(src)
        out = Path(out) if out else default_out(src, title)
    text, enc, paras, info, nhead = _text_to_paras(src, split, ruby, log)
    if lang == "auto":
        lang = detect_lang(text)
    if not paras:
        raise RuntimeError("문단을 하나도 만들지 못했습니다 (빈 파일?)")
    # _src.txt 왕복(사람이 고친 문단 구조)은 자동 교정하지 않는다
    paras, af_log, _st = _autofix(paras, info["p95"],
                                  "off" if from_src else autofix, cfg, log, lang)
    if af_log:
        nhead = sum(1 for p in paras if p.startswith((HEAD_MARK, SUB_MARK)))
    dm = damage(paras, lang)
    if dm["ratio"] >= 0.02:
        log(f"⚠ 원본 손상: 문단 {dm['n']}개({dm['ratio']:.0%})에 깨진 글자"
            + (f" — {dm['first'] + 1}번째 문단 부근부터" if dm["first"] is not None else "")
            + ". 원본 파일 자체가 손상돼 복구할 수 없습니다 (다른 판본 권장). "
              "깨진 문단은 자동 교정·AI 판정에서 제외했습니다")
    _save_book(out, title, lang, paras,
               {"file": str(src), "encoding": enc,
                "split": info["used"], "p95": info["p95"],
                "term": info["term"], "trail": info["trail"],
                # 다시 적용(옵션 바꾸기)용 — 사용자가 고른 옵션 그대로
                "opt": {"split": split, "ruby": ruby,
                        "autofix": "off" if from_src else autofix},
                "damage": dm["ratio"]},
               log, resplit_src=src if from_src else None, af_log=af_log)
    log(f"가져오기 완료: {title} — {len(paras)}문단, 제목 {nhead}개, "
        f"{enc}, 모드 {info['used']} (p95 {info['p95']}자, "
        f"문장종결 {info['term']:.0%}, 줄끝공백 {info['trail']:.0%}) → {out}")
    return {"out": str(out), "title": title, "count": len(paras),
            "headings": nhead, "lang": lang, "encoding": enc, "info": info}


def _file_label(ps: list, f: Path, root: Path):
    """폴더 가져오기 — 파일 첫머리에 제목이 없으면 `## 파일명` 장 제목 삽입."""
    if any(p.startswith(HEAD_MARK) for p in ps[:3]):
        return ps, 0
    rel = f.relative_to(root)
    label = " / ".join(list(rel.parts[:-1]) + [f.stem])
    return [HEAD_MARK + label] + ps, 1


def preview(src, split: str = "auto", ruby: str = "strip", n: int = 40,
            title: Optional[str] = None, autofix: str = "off",
            cfg: Optional[dict] = None) -> dict:
    """가져오기 전 미리보기 — 저장하지 않고 앞 n문단과 판별 정보만.

    웹앱 가져오기 창에서 재구성 방식(auto/wrap/lines/blank)을 바꿔 가며
    확인하는 용도. 폴더는 앞 파일부터 n문단이 찰 때까지만 처리한다."""
    src = Path(src)
    from_src = src.is_file() and src.name.endswith("_src.txt")
    if from_src and split == "auto":
        split = "blank"
    if from_src:
        t = (title or "").strip() or src.name[:-len("_src.txt")]
        out = src.parent
    else:
        t = (title or "").strip() or default_title(src)
        out = default_out(src, t)
    r = {"src": str(src), "title": t, "out": str(out), "folder": src.is_dir()}
    if src.is_dir():
        files = list_texts(src)
        if not files:
            raise RuntimeError(f"텍스트 파일이 없습니다: {src}")
        r["files"] = len(files)
        ps_all, info0, enc0, used = [], None, "", set()
        for f in files:
            _t, enc, ps, info, _nh = _text_to_paras(f, split, ruby, lambda m: None)
            if not ps:
                continue
            if len(files) > 1:
                ps, _a = _file_label(ps, f, src)
            info0 = info0 or info
            enc0 = enc0 or enc
            used.add(info["used"])
            ps_all += ps
            if len(ps_all) >= n:
                break
        paras, info, enc = ps_all, (info0 or {}), enc0
        info = dict(info, used=",".join(sorted(used)) or info.get("used", ""))
        r["count"] = None                   # 폴더 전체는 가져올 때 계산
    else:
        if not src.is_file():
            raise FileNotFoundError(str(src))
        _t, enc, paras, info, nh = _text_to_paras(src, split, ruby, lambda m: None)
        if autofix in ("rule", "ai") and not from_src:
            # 미리보기는 규칙 교정까지 적용해 보여 주고, AI는 예상치만
            import ebook_autofix as af
            an = af.analyze(paras, info["p95"])
            paras, _l, st = _autofix(paras, info["p95"], "rule", None,
                                     lambda m: None, detect_lang(_t))
            r["af"] = dict(st, ask=an["stats"]["ask_J"] + an["stats"]["ask_H"]
                           + an["stats"]["ask_S"], engine=af.ai_engine_label(cfg or {}))
            nh = sum(1 for p in paras if p.startswith(HEAD_MARK))
        r["count"] = len(paras)
        r["headings"] = nh
        r["subheads"] = sum(1 for p in paras if p.startswith(SUB_MARK))
        r["damage"] = damage(paras, detect_lang(_t))
    r.update(encoding=enc, mode=info.get("used", ""), p95=info.get("p95"),
             term=info.get("term"), trail=info.get("trail"),
             paras=[p if len(p) <= 400 else p[:400] + "…" for p in paras[:n]])
    bp = out / "_work" / "book.json"
    if bp.exists():                         # 이미 가져온 책 — 덮어쓰기 경고용
        try:
            old = json.loads(bp.read_text(encoding="utf-8"))
            r["existing"] = {"count": len(old.get("paras") or []),
                             "mode": (old.get("origin") or {}).get("split", ""),
                             "opt": (old.get("origin") or {}).get("opt") or {},
                             "title": old.get("title") or "",
                             "hi": sum(len(v) for k, v in (old.get("hi") or {}).items()
                                       if not str(k).startswith("_")),
                             "bmks": len(old.get("bmks") or [])}
        except Exception:
            r["existing"] = {"count": 0}
    return r


def import_folder(src, out=None, title: Optional[str] = None,
                  lang: str = "auto", split: str = "auto", ruby: str = "strip",
                  log: Callable[[str], None] = print,
                  max_files: int = 3000, autofix: str = "off",
                  cfg: Optional[dict] = None) -> dict:
    """폴더 안 텍스트 여러 개(권·장 분할본) → 한 권. 파일마다 따로 재구성
    (줄 구조가 파일마다 다를 수 있음)하고 자연 정렬로 잇는다. 파일 첫머리에
    제목이 감지되지 않으면 `## 파일명`을 장 제목으로 넣는다(목차·이동용)."""
    src = Path(src)
    files = list_texts(src)
    if not files:
        raise RuntimeError(f"텍스트 파일이 없습니다: {src}")
    if len(files) > max_files:
        raise RuntimeError(f"파일이 너무 많습니다({len(files)}개) — 하위 폴더를 고르세요")
    title = (title or "").strip() or default_title(src)
    out = Path(out) if out else default_out(src, title)
    paras, encs, modes, nhead, sample, p95s = [], set(), set(), 0, [], []
    for f in files:
        try:
            text, enc, ps, info, nh = _text_to_paras(f, split, ruby, lambda m: None)
        except Exception as e:
            log(f"  ! {f.name}: {e}")
            continue
        if not ps:
            continue
        encs.add(enc)
        modes.add(info["used"])
        p95s.append(info["p95"])
        if len(sample) < 20000:
            sample.append(text[:4000])
        if len(files) > 1:
            ps, add = _file_label(ps, f, src)
            nh += add
        paras += ps
        nhead += nh
    if not paras:
        raise RuntimeError("문단을 하나도 만들지 못했습니다 (빈 파일들?)")
    if lang == "auto":
        lang = detect_lang("\n".join(sample))
    p95 = sorted(p95s)[len(p95s) // 2] if p95s else None
    paras, af_log, _st = _autofix(paras, p95, autofix, cfg, log, lang)
    if af_log:
        nhead = sum(1 for p in paras if p.startswith((HEAD_MARK, SUB_MARK)))
    _save_book(out, title, lang, paras,
               {"file": str(src), "folder": True, "files": len(files),
                "encoding": ",".join(sorted(encs)), "p95": p95,
                "split": ",".join(sorted(modes)),
                "opt": {"split": split, "ruby": ruby, "autofix": autofix},
                "damage": damage(paras, lang)["ratio"]}, log, af_log=af_log)
    log(f"폴더 가져오기 완료: {title} — 파일 {len(files)}개 → {len(paras)}문단, "
        f"제목 {nhead}개, 모드 {','.join(sorted(modes))} → {out}")
    return {"out": str(out), "title": title, "count": len(paras),
            "headings": nhead, "lang": lang, "files": len(files)}


# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="평문 txt/md → Fokus Viewer 책")
    ap.add_argument("src", help="텍스트 파일 (.txt/.md) 또는 폴더(여러 파일 → 한 권)")
    ap.add_argument("--out", default=None, help="출력 폴더 (기본 <파일 폴더>/<제목>_book)")
    ap.add_argument("--title", default=None, help="책 제목 (기본: 폴더명 또는 파일명)")
    ap.add_argument("--lang", default="auto", help="ko|ja|zh|en|de (기본 자동)")
    ap.add_argument("--split", default="auto", choices=["auto", "wrap", "lines", "blank"],
                    help="문단 분리: auto(자동 판별) wrap(하드 줄바꿈 재구성) lines(한 줄=한 문단) blank(빈 줄 기준 — _src.txt 재가져오기용)")
    ap.add_argument("--ruby", default="strip", choices=["strip", "paren", "keep"],
                    help="青空文庫 루비 처리")
    ap.add_argument("--autofix", default="off", choices=["off", "rule", "ai"],
                    help="자동 교정: off | rule(규칙) | ai(규칙+AI, ebook_config.json의 번역 엔진)")
    ap.add_argument("--preview", type=int, default=0, metavar="N",
                    help="저장하지 않고 앞 N문단만 출력")
    a = ap.parse_args(argv)
    if a.preview and Path(a.src).is_dir():
        fs = list_texts(Path(a.src))
        print(f"폴더: 텍스트 {len(fs)}개 — 기본 제목 {default_title(a.src)!r}")
        for f in fs[:a.preview]:
            print("  ", f.relative_to(a.src))
        return 0
    if a.preview:
        text, enc = read_text_any(Path(a.src))
        if looks_html(text):
            text = strip_html(text)
        text = aozora_clean(text, ruby=a.ruby)
        paras, info = reflow(text, split=a.split)
        paras, nh = mark_headings(paras)
        print(f"[{enc}] {info}  문단 {len(paras)} 제목 {nh}")
        for i, p in enumerate(paras[:a.preview]):
            print(f"--- #{i+1} ({len(p)}자)\n{p}")
        return 0
    cfg = None
    if a.autofix == "ai":
        try:
            import ebook_translate as core
            cp = Path(__file__).with_name("ebook_config.json")
            cfg = json.loads(cp.read_text(encoding="utf-8")) if cp.exists() else {}
            core._apply_keys(cfg)
        except Exception as e:
            print(f"설정 읽기 실패: {e}")
    import_text(a.src, a.out, a.title, a.lang, a.split, a.ruby,
                autofix=a.autofix, cfg=cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
