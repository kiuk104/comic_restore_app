"""평문 책 자동 교정 — 규칙(확실한 것) + AI(애매한 곳만, 지시 목록 방식).

가져오기(ebook_import) 직후 또는 이미 가져온 책(편집 서버 /api/autofix)에서
문단 구조를 자동으로 고친다.

  1) 규칙: 확실한 것만 적용
     - 잘린 문단 합치기: 앞 문단이 문장 중간에서 끝났고
         · 뒤 문단이 조사·어미 조각으로 시작("걱정" + "을 완전히") → 붙여 씀
         · 앞 문단이 대사 따옴표를 연 채 끝남 → 띄어 씀
     - 제목: [ 여행] · <제목> · ■ 제목 · 1. 제목 · (7) 제목 · 숫자만 있는 줄
       (앞뒤가 본문인 외톨이 줄만 — 목차 목록은 제외)
     - 잡음: PC통신 게시물 머리(#3705 이름 (아이디), … 288 line, 보낸이:…조회:)
       — 지우되 기록에 원문을 남겨 되돌릴 수 있게. '차 례: X'는 장 제목 X로.
     - 덜 나뉜 책(평균 문단이 아주 긴 책): 서술→대사, 대사→서술 경계에서 나눔
  2) AI: 규칙이 애매하다고 표시한 곳만 묻는다 (본문 전체를 보내지 않음)
     - J 문단 경계(앞 끝 ‖ 뒤 첫머리) → merge / merge0(붙여 씀) / keep
     - H 짧은 외톨이 줄 → head / sub / body
     - S 그래도 너무 긴 문단 → 새 문단이 시작되는 문장 번호
     AI는 글을 다시 쓰지 않고 '지시'만 돌려준다 → 프로그램이 적용하므로 본문
     글자가 바뀌거나 빠질 일이 없고, 엉뚱한 지시는 검증에서 버린다.

모든 변경은 book["af"]["items"]에 기록 — 편집 페이지 「자동 교정 기록」에서
하나씩 되돌릴 수 있다(구조 변경은 PC). 문단 번호가 바뀌어도 기록은
restructure_book이 함께 옮긴다.
"""
from __future__ import annotations

import re
import time
from typing import Callable, Optional

import ebook_import as imp
import ebook_translate as core

# ---------------------------------------------------------------------------
# 판정 도우미
# ---------------------------------------------------------------------------
_H2 = "## "
_H3 = "### "
_BULLET = re.compile(r"^[-—–*·•■□◆◇●○▶▷★☆※◎▣◈]")
_LISTNUM = re.compile(r"^\(?\d{1,3}[.)]")


def head_lv(s: str) -> int:
    s = s or ""
    return 3 if s.startswith(_H3) else 2 if s.startswith(_H2) else 0


def body(s: str) -> str:
    return re.sub(r"^#{2,3}\s*", "", s or "").strip()


def quote_open(s: str) -> bool:
    """문단이 큰따옴표·꺾쇠 대사를 연 채로 끝났나."""
    s = s or ""
    if s.count('"') % 2 == 1:
        return True
    if s.count("“") > s.count("”"):
        return True
    if s.count("[") > s.count("]") and s.lstrip().startswith("["):
        return True                                  # 무협지 [대사]
    if s.count("「") > s.count("」") or s.count("『") > s.count("』"):
        return True
    return False


# 제목 꼴 (외톨이 짧은 줄일 때만) — (이름, 정규식)
_HEAD_PATS = [
    ("bracket", re.compile(r"^\[\s*[^\[\]]{1,30}\]?$")),
    ("angle", re.compile(r"^[<〈《【][^<>〈〉《》【】]{1,30}[>〉》】]$")),
    ("bullet", re.compile(r"^[■□◆◇●○▶▷★☆※◎▣◈]\s*\S.{0,30}$")),
    ("num", re.compile(r"^(\d{1,3}|[IVXⅠ-Ⅻ]{1,6}\.?|#\s*\d{1,3})$")),
    ("numtitle", re.compile(r"^(\(\d{1,3}\)|\d{1,3}[.)])\s*\S.{0,38}$")),
    ("chapter", re.compile(r"^제\s*\d+\s*[장부편권화절막과판]\b.{0,30}$")),
]
# PC통신 게시물 머리 — 이 신호가 있는 줄 근처의 메타 줄만 지운다
_BBS_SIG = [re.compile(r"^#\d{2,6}\s+\S.*\(\s*[^()]{1,20}\s*\)\s*$"),
            re.compile(r"\d{1,2}/\d{1,2}\s+\d{1,2}:\d{2}\s+\d+\s*line\s*$", re.I),
            re.compile(r"^보낸이\s*:.*(조회\s*:?\s*\d+|\d{4}-\d\d-\d\d)")]
_BBS_META = re.compile(r"^(제\s*목|지은이|옮긴이|올린이|글쓴이|번\s*역|작\s*가)\s*:\s*\S")
_BBS_TOC = re.compile(r"^차\s*례\s*:\s*(\S.*)$")


def _is_bbs_sig(s: str) -> bool:
    return any(r.search(s) for r in _BBS_SIG)


def _sentences(p: str) -> list[int]:
    """문장 시작 오프셋 목록 (첫 문장 0 포함)."""
    offs = [0]
    for m in re.finditer(r"[.!?…]+[\"”’'」』\]]?\s+(?=\S)", p):
        offs.append(m.end())
    return offs


def _dlg_splits(p: str) -> list[int]:
    """덜 나뉜 문단에서 확실한 대사 경계 — 서술 끝 → 여는 따옴표, 닫는 대사 → 서술."""
    out = []
    for m in re.finditer(r"[.!?…]\s+(?=[\"“「『\[])", p):           # 서술 → 대사
        out.append(m.end())
    for m in re.finditer(r"[.!?…~][\"”」』\]]\s+(?=[가-힣])", p):   # 대사 → 서술
        rest = p[m.end():m.end() + 6]
        if not imp._QUOTATIVE.match(rest):
            out.append(m.end())
    return sorted(set(o for o in out if 0 < o < len(p)))


# ---------------------------------------------------------------------------
# 1) 규칙 분석 — 확실한 ops + 애매한 asks
# ---------------------------------------------------------------------------
def analyze(paras: list[str], p95: Optional[int] = None) -> dict:
    """paras(원문 문자열 목록) → {"ops": [...], "asks": [...], "stats": {...}}.

    ops (원래 문단 번호 기준):
      {"t":"merge","i":i,"sep":" "|""}   i와 i+1 합치기
      {"t":"head","i":i,"lv":2|3,"text":새 원문?}
      {"t":"drop","i":i}
      {"t":"split","i":i,"offs":[...]}    i를 원문 오프셋들에서 나누기
    asks: {"k":"J","i":i,...} / {"k":"H","i":i,...} / {"k":"S","i":i,"offs":[...]}"""
    n = len(paras)
    P = [p or "" for p in paras]
    if not p95:
        ls = sorted(len(x) for x in P if x)
        p95 = ls[int(len(ls) * 0.95) - 1] if len(ls) > 1 else 40
    ops, asks = [], []
    has_h2 = any(head_lv(x) == 2 for x in P)
    drop = set()
    headset = {}

    # ── 잡음: PC통신 머리 블록 ──
    sig = [k for k in range(n) if _is_bbs_sig(P[k].strip())]
    near = set()
    for k in sig:
        for d in range(-4, 5):
            if 0 <= k + d < n:
                near.add(k + d)
    for k in sorted(near):
        t = P[k].strip()
        if head_lv(t):
            continue
        if _is_bbs_sig(t) or _BBS_META.match(t):
            drop.add(k)
        else:
            m = _BBS_TOC.match(t)
            if m:
                headset[k] = (2, _H2 + m.group(1).strip())
    for k in sorted(drop):
        ops.append({"t": "drop", "i": k, "why": "PC통신 게시물 머리"})

    # ── 제목 꼴 (외톨이 짧은 줄) ──
    def short(k):
        return 0 <= k < n and k not in drop and len(P[k].strip()) <= 40 \
            and not imp.is_term(P[k].strip()) and not head_lv(P[k])

    cls_hits = {}
    # [대사] 문체(무협지 등) — [ ]로 감싼 줄이 흔하면 괄호 제목 꼴은 쓰지 않는다
    # (닫는 괄호 없는 '[ 여행' 꼴만 인정)
    br_dlg = sum(1 for x in P if x.strip().startswith("[") and
                 x.strip().endswith("]")) >= 20
    for k in range(n):
        t = P[k].strip()
        if k in drop or k in headset or head_lv(t) or not t or "\n" in t:
            continue
        if len(t) > 42:
            continue
        inner = re.sub(r"^[\[<〈《【(]\s*|\s*[\]>〉》】)]$", "", t)
        if imp.is_term(inner) or inner.endswith((",", "，")):
            continue                     # 문장처럼 끝남 — 대사·본문
        for name, rx in _HEAD_PATS:
            if name == "bracket" and br_dlg and t.endswith("]"):
                continue
            if rx.match(t):
                # 목차처럼 짧은 줄이 이어지는 곳은 제외
                if short(k - 1) and short(k + 1):
                    break
                cls_hits.setdefault(name, []).append(k)
                break
    for name, ks in cls_hits.items():
        lv = 2 if (not has_h2 and len(ks) >= 3 and name in
                   ("num", "chapter", "bracket", "numtitle")) else 3
        for k in ks:
            headset[k] = (lv, None)
    for k, (lv, txt) in sorted(headset.items()):
        op = {"t": "head", "i": k, "lv": lv, "why": "제목 꼴"}
        if txt:
            op["text"] = txt
            op["why"] = "게시물 차례 줄 → 장 제목"
        ops.append(op)

    # ── 문단 경계 ──
    for k in range(n - 1):
        if k in drop or k + 1 in drop or k in headset or k + 1 in headset:
            continue
        a, b = P[k].strip(), P[k + 1].strip()
        if not a or not b or head_lv(a) or head_lv(b):
            continue
        if imp.is_term(a):
            continue
        if b.startswith(imp._OPEN_Q) or _BULLET.match(b) or _LISTNUM.match(b):
            continue
        if not re.match(r"[가-힣A-Za-z0-9぀-ヿ一-鿿(]", b):
            continue
        if imp.ko_nospace(a, b):
            ops.append({"t": "merge", "i": k, "sep": "", "why": "낱말 중간에서 잘림"})
        elif quote_open(a) and not b.startswith(imp._OPEN_Q):
            ops.append({"t": "merge", "i": k, "sep": " ", "why": "대사 도중 끊김"})
            if re.match(r"[가-힣]", a[-1]) and re.match(r"[가-힣]", b[0]):
                # 합치는 건 확실, 띄어쓰기만 애매 ("질" + "서에 대해") → AI가 고름
                asks.append({"k": "J", "i": k, "a": a[-70:], "b": b[:70], "sp": 1})
        elif len(a) <= 30 and "\n" not in a:
            # 짧은 미종결 줄 — 제목·날짜·서명일 수도, 잘린 줄일 수도
            if not short(k - 1) or k == 0:
                asks.append({"k": "H", "i": k, "t": a,
                             "p": P[k - 1].strip()[-60:] if k else "",
                             "n": b[:60]})
        else:
            asks.append({"k": "J", "i": k, "a": a[-70:], "b": b[:70]})

    # ── 덜 나뉜 책: 문단이 지나치게 길면 대사 경계에서 나눔 ──
    tot = sum(len(x) for x in P)
    avg = tot / max(1, n)
    long_cut = 1500 if avg < 400 else 600
    for k in range(n):
        if k in drop or head_lv(P[k]) or len(P[k]) < long_cut:
            continue
        offs = _dlg_splits(P[k]) if avg >= 400 else []
        if offs:
            ops.append({"t": "split", "i": k, "offs": offs, "why": "대사 경계"})
        # 대사 경계로 나눈 뒤에도 긴 조각이 남으면 AI에 문장 단위로 묻는다
        cuts = [0] + offs + [len(P[k])]
        for a0, b0 in zip(cuts, cuts[1:]):
            if b0 - a0 >= 1200:
                so = [a0 + o for o in _sentences(P[k][a0:b0])]
                if len(so) >= 4:
                    asks.append({"k": "S", "i": k, "offs": so, "lo": a0, "hi": b0})
    st = {"merge": sum(1 for o in ops if o["t"] == "merge"),
          "head": sum(1 for o in ops if o["t"] == "head"),
          "drop": sum(1 for o in ops if o["t"] == "drop"),
          "split": sum(len(o["offs"]) for o in ops if o["t"] == "split"),
          "ask_J": sum(1 for a in asks if a["k"] == "J"),
          "ask_H": sum(1 for a in asks if a["k"] == "H"),
          "ask_S": sum(1 for a in asks if a["k"] == "S"),
          "paras": n, "avg": round(avg)}
    st["ask_chars"] = _ask_chars(P, asks)
    st["ask_tokens"] = int(st["ask_chars"] * 0.9) + 600 * _n_batches(P, asks)
    return {"ops": ops, "asks": asks, "stats": st}


# ---------------------------------------------------------------------------
# 2) AI 판정
# ---------------------------------------------------------------------------
PROMPT_AF = """다음은 txt 전자책을 문단으로 재구성할 때 판단이 애매했던 곳들이다.
본문을 고쳐 쓰지 말고, 각 항목에 대한 판정만 JSON 배열로 출력하라.
형식: [{{"id": 번호, "a": 판정}}, ...]  — 설명·코드블록 없이 JSON만.

항목 유형
- [J] 문단 경계: 『앞 문단 끝』‖『다음 문단 첫머리』.
  한 문장·한 문단이 줄바꿈 때문에 잘못 나뉜 것이면 "merge",
  그중 낱말 한가운데서 잘린 것(띄어쓰기 없이 붙여야 함)이면 "merge0",
  원래 서로 다른 문단(새 문장·새 화제·시·목록·제목 등)이면 "keep".
- [H] 짧은 외톨이 줄: (앞 문맥) ▶줄◀ (뒤 문맥).
  책의 장 제목이면 "head", 장 안의 소제목이면 "sub",
  본문의 일부(짧은 대사·문장·날짜·서명·인용 등)이면 "body",
  다음 문단과 이어지는 잘린 줄이면 "merge".
- [S] 긴 문단: 번호 붙은 문장들. 원래 책이라면 새 문단이 시작됐을 문장
  번호 목록 (예: [3, 7]). 나눌 곳이 없으면 []. 1번은 넣지 말 것.
  대사(따옴표)가 새로 시작되는 곳, 장면·화제가 바뀌는 곳이 기준.

{items}
"""


def _ask_text(P: list[str], a: dict, num: int) -> str:
    if a["k"] == "J":
        return f"{num}. [J] 『…{a['a']}』‖『{a['b']}…』"
    if a["k"] == "H":
        return f"{num}. [H] (…{a['p']}) ▶{a['t']}◀ ({a['n']}…)"
    p = P[a["i"]]
    offs = a["offs"] + [a["hi"]]
    sents = [p[offs[j]:offs[j + 1]].strip() for j in range(len(a["offs"]))]
    body_ = "\n".join(f"  ({j + 1}) {s}" for j, s in enumerate(sents))
    return f"{num}. [S]\n{body_}"


def _batches(P, asks, budget=6000):
    cur, size = [], 0
    for a in asks:
        t = _ask_text(P, a, 0)
        if cur and size + len(t) > budget:
            yield cur
            cur, size = [], 0
        cur.append(a)
        size += len(t)
    if cur:
        yield cur


def _n_batches(P, asks) -> int:
    return sum(1 for _ in _batches(P, asks))


def _ask_chars(P, asks) -> int:
    return sum(len(_ask_text(P, a, 0)) for a in asks)


def ai_engine_label(cfg: dict) -> str:
    be = (cfg or {}).get("backend") or "claude"
    if be == "gemini":
        return "Gemini " + (cfg.get("gemini_model") or core.GEMINI_MODEL)
    if be == "kimi":
        return "Kimi " + core._norm_kimi_model(cfg.get("kimi_model"))
    if be == "ollama":
        return "Ollama " + (cfg.get("ollama_model") or "")
    return "Claude " + af_claude_model(cfg)


def af_claude_model(cfg: dict) -> str:
    # 분류만 하므로 번역용(sonnet)보다 가벼운 모델로 충분 — 설정으로 바꿀 수 있음
    return (cfg or {}).get("af_claude_model") or "claude-haiku-4-5"


def ai_call(cfg: dict, body_: str) -> list:
    """번역 엔진 설정(backend)을 그대로 써서 JSON 배열 응답을 받는다."""
    be = cfg.get("backend") or "claude"
    if be == "ollama":
        r = core.retype._call_ollama(
            {"ollama_model": cfg.get("ollama_model"),
             "ollama_url": cfg.get("ollama_url")}, body_)
        return r if isinstance(r, list) else []
    if be == "gemini":
        raw = core._call_gemini([{"role": "user", "content": body_}],
                                cfg.get("gemini_model") or core.GEMINI_MODEL,
                                cfg.get("gemini_key") or "", max_tokens=4000,
                                part="교정")
    elif be == "kimi":
        raw = core._call_kimi([{"role": "user", "content": body_}],
                              core._norm_kimi_model(cfg.get("kimi_model")),
                              cfg.get("kimi_key") or "", max_tokens=8000,
                              part="교정")
    else:
        import anthropic
        model = af_claude_model(cfg)
        msg = anthropic.Anthropic().messages.create(
            model=model, max_tokens=4000, temperature=0.0,
            messages=[{"role": "user", "content": body_}])
        u = getattr(msg, "usage", None)
        core.retype.track_usage("교정", model, getattr(u, "input_tokens", 0),
                                getattr(u, "output_tokens", 0))
        raw = msg.content[0].text
    return core._parse_loose(raw)


def ai_decide(P: list[str], asks: list, cfg: dict,
              log: Callable[[str], None] = print,
              call=None) -> list:
    """asks → 추가 ops. call(cfg, prompt) 주입 가능(테스트)."""
    call = call or ai_call
    ops = []
    bs = list(_batches(P, asks))
    for bi, batch in enumerate(bs, 1):
        items = "\n".join(_ask_text(P, a, j) for j, a in enumerate(batch, 1))
        try:
            res = call(cfg, PROMPT_AF.format(items=items))
        except Exception as e:
            log(f"  AI 판정 실패 (묶음 {bi}/{len(bs)}): {e} — 이 묶음은 그대로 둠")
            continue
        by = {}
        for r in res or []:
            if isinstance(r, dict):
                try:
                    by[int(r.get("id"))] = r.get("a")
                except (TypeError, ValueError):
                    pass
        for j, a in enumerate(batch, 1):
            v = by.get(j)
            if a["k"] == "J" and v in ("merge", "merge0"):
                ops.append({"t": "merge", "i": a["i"],
                            "sep": "" if v == "merge0" else " ",
                            "why": "AI: 잘린 문단", "by": "ai"})
            elif a["k"] == "H" and v in ("head", "sub"):
                ops.append({"t": "head", "i": a["i"], "lv": 2 if v == "head" else 3,
                            "why": "AI: " + ("장 제목" if v == "head" else "소제목"),
                            "by": "ai"})
            elif a["k"] == "H" and v == "merge":
                ops.append({"t": "merge", "i": a["i"],
                            "sep": "" if imp.ko_nospace(P[a["i"]], P[a["i"] + 1]) else " ",
                            "why": "AI: 잘린 줄", "by": "ai"})
            elif a["k"] == "S" and isinstance(v, list):
                offs = sorted({a["offs"][x - 1] for x in v
                               if isinstance(x, int) and 2 <= x <= len(a["offs"])})
                if offs:
                    ops.append({"t": "split", "i": a["i"], "offs": offs,
                                "why": "AI: 문단 나눔", "by": "ai"})
        log(f"  AI 판정 {bi}/{len(bs)} 묶음 — 지금까지 {len(ops)}건 적용 예정")
    return ops


# ---------------------------------------------------------------------------
# 3) 적용 — 뒤에서부터 (앞쪽 번호가 안 밀리게), 기록 남김
# ---------------------------------------------------------------------------
def apply_ops(book: dict, done: dict, ops: list, by_default: str = "rule") -> dict:
    """ops를 book(book.json 형식)에 적용. 북마크·하이라이트·위치·번역 키는
    restructure_book이 함께 옮긴다. 반환: 종류별 적용 건수."""
    af = book.setdefault("af", {})
    items = af.setdefault("items", [])
    af["ts"] = int(time.time() * 1000)
    paras = book["paras"]
    cnt = {"merge": 0, "head": 0, "drop": 0, "split": 0}
    # 같은 문단에 여러 op — 순서: merge(i 끝과 i+1) → head → split(문단 안) → drop.
    # (merge를 먼저 해야 split 오프셋이 앞부분 그대로 유효). 문단 번호
    # 내림차순으로 적용하면 앞 번호는 그대로 유효하다.
    rank = {"merge": 0, "head": 1, "split": 2, "drop": 3}
    # 같은 (종류, 문단) — AI 판정이 규칙보다 우선 (예: 대사 합치기의 띄어쓰기)
    best = {}
    uniq = []
    for o in ops:
        if o["t"] == "split":
            uniq.append(o)
            continue
        key = (o["t"], o["i"])
        if key not in best or o.get("by") == "ai":
            best[key] = o
    uniq += list(best.values())
    # split이 같은 문단에 둘(규칙+AI)이면 오프셋 합침
    sp = {}
    rest = []
    for o in uniq:
        if o["t"] == "split":
            d = sp.setdefault(o["i"], dict(o, offs=set()))
            d["offs"] |= set(o["offs"])
            if o.get("by") == "ai":
                d["by"] = "ai"
        else:
            rest.append(o)
    uniq = rest + [dict(o, offs=sorted(o["offs"])) for o in sp.values()]
    uniq.sort(key=lambda o: (-o["i"], rank[o["t"]]))
    for o in uniq:
        i = o["i"]
        by = o.get("by") or by_default
        if not 0 <= i < len(paras):
            continue
        if o["t"] == "split":
            src = paras[i].get("src") or ""
            done_any = False
            for off in sorted(o["offs"], reverse=True):
                a, b = src[:off].strip(), src[off:].strip()
                if not a or not b:
                    continue
                core.restructure_book(book, done, "split", i, off)
                items.append({"t": "split", "i": i + 1, "by": by,
                              "why": o.get("why", ""), "a": a[-20:], "b": b[:20]})
                done_any = True
            if done_any:
                cnt["split"] += 1
        elif o["t"] == "head":
            old = paras[i].get("src") or ""
            new = o.get("text") or ((_H2 if o["lv"] == 2 else _H3) + body(old))
            if new != old:
                paras[i] = dict(paras[i], src=new)
                items.append({"t": "head", "i": i, "lv": o["lv"], "prev": old,
                              "new": body(new)[:60],
                              "by": by, "why": o.get("why", "")})
                cnt["head"] += 1
        elif o["t"] == "merge":
            if i >= len(paras) - 1:
                continue
            if head_lv(paras[i].get("src")) or head_lv(paras[i + 1].get("src")):
                continue                # 제목으로 판정된 줄은 합치지 않음
            a = (paras[i].get("src") or "").rstrip()
            bb = (paras[i + 1].get("src") or "").lstrip()
            r = core.restructure_book(book, done, "merge", i, sep=o.get("sep", " "))
            items.append({"t": "merge", "i": i, "off": r["caret"],
                          "sep": o.get("sep", " "), "by": by,
                          "why": o.get("why", ""), "a": a[-20:], "b": bb[:20]})
            cnt["merge"] += 1
        elif o["t"] == "drop":
            if len(paras) < 2:
                continue
            txt = paras[i].get("src") or ""
            core.restructure_book(book, done, "drop", i)
            items.append({"t": "drop", "i": i, "text": txt, "by": by,
                          "why": o.get("why", "")})
            cnt["drop"] += 1
    # 기록은 책 순서대로
    items.sort(key=lambda x: (x["i"], x.get("off") or 0))
    return cnt


def _sig_merge(a: str, b: str) -> str:
    return "m:" + re.sub(r"\s+", "", (a or "")[-12:]) + "|" + \
        re.sub(r"\s+", "", (b or "")[:12])


def _sig_text(t: str, s: str) -> str:
    return t + ":" + re.sub(r"\s+", "", body(s or ""))[:40]


def _remember_no(book: dict, sig: str) -> None:
    """되돌린 곳 — 다시 검사해도 같은 교정을 하지 않게 기억."""
    no = book.setdefault("af", {}).setdefault("no", [])
    if sig not in no:
        no.append(sig)
        del no[:-2000]


def _filter_no(book: dict, P: list, ops: list, asks: list):
    no = set((book.get("af") or {}).get("no") or [])
    if not no:
        return ops, asks

    def bad(k, i):
        if k in ("merge", "J"):
            return i + 1 < len(P) and _sig_merge(P[i], P[i + 1]) in no
        if k in ("head", "H"):
            return _sig_text("h", P[i]) in no
        if k == "drop":
            return _sig_text("d", P[i]) in no
        return False
    ops = [o for o in ops if not bad(o["t"], o["i"])]
    asks = [a for a in asks if not bad(a["k"], a["i"])]
    return ops, asks


def undo_item(book: dict, done: dict, k: int) -> dict:
    """기록 k번째 되돌리기 (PC 편집 서버). 반환: 바뀐 문단 번호."""
    items = (book.get("af") or {}).get("items") or []
    if not 0 <= k < len(items):
        raise ValueError("이미 되돌렸거나 없는 기록입니다")
    it = items[k]
    paras = book["paras"]
    i = int(it["i"])
    t = it["t"]
    items.pop(k)                    # 먼저 빼야 restructure의 기록 이동에서 제외
    if t == "head":
        if 0 <= i < len(paras):
            paras[i] = dict(paras[i], src=it.get("prev") or body(paras[i]["src"]))
            _remember_no(book, _sig_text("h", paras[i]["src"]))
        return {"i": i}
    if t == "merge":
        src = paras[i].get("src") or ""
        off = int(it.get("off") or 0)
        sep = it.get("sep", " ")
        cut = off - len(sep)
        if not 0 < cut < len(src):
            raise ValueError("문단이 그 사이 바뀌어 자동으로 되돌릴 수 없습니다 — "
                             "✂ 나누기로 직접 나누세요")
        core.restructure_book(book, done, "split", i, cut)
        _remember_no(book, _sig_merge(paras[i]["src"], paras[i + 1]["src"]))
        return {"i": i}
    if t == "split":
        if not 1 <= i < len(paras):
            raise ValueError("문단을 찾을 수 없습니다")
        core.restructure_book(book, done, "merge", i - 1)
        return {"i": i - 1}
    if t == "drop":
        core.restructure_book(book, done, "insert", min(i, len(paras)),
                              text=it.get("text") or "")
        _remember_no(book, _sig_text("d", it.get("text") or ""))
        return {"i": i}
    raise ValueError("알 수 없는 기록")


# ---------------------------------------------------------------------------
# 진입점
# ---------------------------------------------------------------------------
def estimate(book: dict) -> dict:
    """적용 없이 예상치 — 되돌린 곳 제외 (편집 페이지 「다시 검사」)."""
    P = [e.get("src") or "" for e in book["paras"]]
    p95 = (book.get("origin") or {}).get("p95")
    an = analyze(P, p95 if isinstance(p95, int) else None)
    ops, asks = _filter_no(book, P, an["ops"], an["asks"])
    st = dict(an["stats"])
    st.update(merge=sum(1 for o in ops if o["t"] == "merge"),
              head=sum(1 for o in ops if o["t"] == "head"),
              drop=sum(1 for o in ops if o["t"] == "drop"),
              split=sum(len(o["offs"]) for o in ops if o["t"] == "split"),
              ask_J=sum(1 for a in asks if a["k"] == "J"),
              ask_H=sum(1 for a in asks if a["k"] == "H"),
              ask_S=sum(1 for a in asks if a["k"] == "S"))
    st["ask_chars"] = _ask_chars(P, asks)
    st["ask_tokens"] = int(st["ask_chars"] * 0.9) + 600 * _n_batches(P, asks) if asks else 0
    return st


def run(book: dict, done: dict, mode: str = "rule", cfg: Optional[dict] = None,
        log: Callable[[str], None] = print, call=None) -> dict:
    """book을 제자리 교정. mode: rule | ai(규칙+AI). 반환: 통계."""
    P = [e.get("src") or "" for e in book["paras"]]
    p95 = (book.get("origin") or {}).get("p95")
    an = analyze(P, p95 if isinstance(p95, int) else None)
    ops, asks = _filter_no(book, P, an["ops"], an["asks"])
    an["asks"] = asks
    st = dict(an["stats"])
    if mode == "ai" and an["asks"]:
        cfg = dict(cfg or {})
        log(f"자동 교정 — AI 판정 {len(an['asks'])}곳 "
            f"(약 {st['ask_tokens']:,} 토큰, {ai_engine_label(cfg)})")
        ops += ai_decide(P, an["asks"], cfg, log, call=call)
    cnt = apply_ops(book, done, ops)
    st["applied"] = cnt
    st["mode"] = mode
    book["af"]["mode"] = mode
    log("자동 교정 적용: 합치기 {merge} · 나누기 {split} · 제목 {head} · "
        "잡음 줄 제거 {drop}".format(**cnt)
        + (f" (AI 미사용 — 애매한 곳 {st['ask_J'] + st['ask_H'] + st['ask_S']}곳은 그대로)"
           if mode != "ai" else ""))
    return st
