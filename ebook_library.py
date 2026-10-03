"""
ebook_library.py — Fokus Viewer 서고(Drive _Ebook_Library) 카탈로그
=====================================================================
PC가 서고 폴더를 한 번 훑어 **카탈로그(책 목록 JSON)** 를 만들고 GAS에 올린다.
폰(셸)은 카탈로그를 받아 폴더 탐색·검색을 **기기 안에서 즉시** 한다 —
GAS가 매번 Drive를 뒤지면 12,000권 서고에서 느리기 때문(2026-10-02 결정:
Drive 접근은 OAuth 대신 GAS 확장, txt는 PC 가져오기 큐).

카탈로그 형식 (v2, 압축형):
  {"v":2, "gen":ms, "root":"_Ebook_Library", "n":파일수,
   "d":[[-1,""], [0,"[무분류]"], [1,"하위"], …],  # 폴더 = [부모idx, 이름] (0=루트)
   "f":[[폴더idx, "이름.txt", 바이트], …],         # 텍스트 파일만 (txt/md)
   "imp":{"상대경로(파일 또는 폴더)": "책제목", …}}  # 이미 가져온 책

CLI:  python ebook_library.py [서고경로] [--out catalog.json] [--stats]
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Callable, Optional

LIB_NAME = "_Ebook_Library"
BOOKS_DIR = "_books"
TEXT_EXTS = (".txt", ".md", ".markdown")
CAT_FILE = "catalog.json"          # <서고>/_books/catalog.json (로컬 사본)


def find_lib_root(cfg: Optional[dict] = None) -> Optional[Path]:
    """서고 루트 — 설정(lib_root) 우선, 없으면 Drive 데스크톱 기본 위치 추정."""
    cand = []
    if cfg and cfg.get("lib_root"):
        cand.append(Path(cfg["lib_root"]))
    home = Path.home()
    for drv in ("내 드라이브", "My Drive"):
        cand.append(home / drv / LIB_NAME)
        for letter in "GHIJ":
            cand.append(Path(f"{letter}:/") / drv / LIB_NAME)
    for c in cand:
        try:
            if c.is_dir():
                return c
        except OSError:
            pass
    return None


def _skip_dir(name: str) -> bool:
    return (name == BOOKS_DIR or name.endswith("_book")
            or name.endswith("_한글번역") or name.startswith("."))


def _imported(root: Path) -> dict:
    """가져온 책 → {원본 상대경로: 제목}. _books/ 모음 + 옛 위치(원본 옆) 둘 다."""
    imp = {}
    books = []
    bd = root / BOOKS_DIR
    if bd.is_dir():
        books += [p for p in bd.iterdir() if p.is_dir() and p.name.endswith("_book")]
    return imp if not books else _read_origins(root, books, imp)


def _read_origins(root: Path, books: list, imp: dict) -> dict:
    for b in books:
        try:
            bk = json.loads((b / "_work" / "book.json").read_text(encoding="utf-8"))
            f = Path((bk.get("origin") or {}).get("file") or "")
            rel = f.relative_to(root).as_posix() if f.is_absolute() else ""
        except Exception:
            continue
        if rel:
            imp[rel] = bk.get("title") or b.name[:-5]
    return imp


def scan_catalog(root, log: Callable[[str], None] = print) -> dict:
    root = Path(root)
    t0 = time.time()
    dirs, files, legacy = [""], [], []
    idx = {"": 0}
    for cur, dnames, fnames in os.walk(root):
        rel = Path(cur).relative_to(root).as_posix()
        rel = "" if rel == "." else rel
        keep = []
        for d in sorted(dnames):
            if d.endswith("_book"):
                legacy.append(Path(cur) / d)
            if not _skip_dir(d):
                keep.append(d)
        dnames[:] = keep                         # os.walk 가지치기
        di = idx.get(rel)
        if di is None:
            continue
        for d in keep:
            r = f"{rel}/{d}" if rel else d
            idx[r] = len(dirs)
            dirs.append(r)
        for fn in sorted(fnames):
            if fn.lower().endswith(TEXT_EXTS) and not fn.endswith("_src.txt"):
                try:
                    sz = (Path(cur) / fn).stat().st_size
                except OSError:
                    sz = -1
                files.append([di, fn, sz])
    imp = _imported(root)
    _read_origins(root, legacy, imp)
    # 텍스트가 하나도 없는 가지(빈 폴더)는 빼서 폰 목록을 가볍게
    has = [False] * len(dirs)
    for di, _, _ in files:
        p = dirs[di]
        while True:
            has[idx[p]] = True
            if not p:
                break
            p = p.rsplit("/", 1)[0] if "/" in p else ""
    remap, nd = {}, []
    for i, d in enumerate(dirs):
        if has[i]:
            remap[i] = len(nd)
            if not d:
                nd.append([-1, ""])
            else:                                 # 부모는 항상 먼저 나온다(os.walk)
                par = d.rsplit("/", 1)[0] if "/" in d else ""
                nd.append([remap[idx[par]], d.rsplit("/", 1)[-1]])
    files = [[remap[di], fn, sz] for di, fn, sz in files]
    cat = {"v": 2, "gen": int(time.time() * 1000), "root": root.name,
           "n": len(files), "d": nd, "f": files, "imp": imp}
    log(f"서고 카탈로그: 텍스트 {len(files):,}개 · 폴더 {len(nd):,}개 · "
        f"가져온 책 {len(imp)}권 ({time.time() - t0:.1f}초)")
    return cat


def catalog_json(cat: dict) -> tuple[str, str]:
    """(직렬화 문자열, 버전) — 버전은 내용 해시(생성 시각 제외)라 서고가
    그대로면 같은 값 → 폰이 다시 받지 않는다."""
    body = {k: v for k, v in cat.items() if k != "gen"}
    ver = hashlib.sha1(json.dumps(body, ensure_ascii=False, sort_keys=True)
                       .encode("utf-8")).hexdigest()[:12]
    s = json.dumps(dict(cat, ver=ver), ensure_ascii=False, separators=(",", ":"))
    return s, ver


def catalog_gz_b64(s: str) -> str:
    """GAS 전송용 — gzip+base64 (672KB → 약 310KB). GAS는 풀지 않고 그대로
    보관·전달하고, 폰이 DecompressionStream('gzip')으로 푼다."""
    import base64
    import gzip
    return base64.b64encode(gzip.compress(s.encode("utf-8"), 9)).decode("ascii")


def cat_path(cat: dict, di: int) -> str:
    """폴더 idx → 상대경로 (테스트·PC용)."""
    parts = []
    while di > 0:
        pi, name = cat["d"][di]
        parts.append(name)
        di = pi
    return "/".join(reversed(parts))


def save_local(root: Path, s: str) -> Path:
    p = Path(root) / BOOKS_DIR / CAT_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(s, encoding="utf-8")
    os.replace(tmp, p)
    return p


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Fokus Viewer 서고 카탈로그 생성")
    ap.add_argument("root", nargs="?", help="서고 폴더 (기본: Drive 자동 탐색)")
    ap.add_argument("--out", help="카탈로그 저장 경로 (기본 <서고>/_books/catalog.json)")
    ap.add_argument("--stats", action="store_true", help="저장하지 않고 통계만")
    a = ap.parse_args(argv)
    root = Path(a.root) if a.root else find_lib_root()
    if not root or not root.is_dir():
        print("서고 폴더를 찾지 못했습니다 — 경로를 인자로 주세요")
        return 1
    cat = scan_catalog(root)
    s, ver = catalog_json(cat)
    print(f"버전 {ver} · {len(s.encode('utf-8')) / 1024:.0f}KB "
          f"(전송 gzip+b64 {len(catalog_gz_b64(s)) / 1024:.0f}KB)")
    if a.stats:
        return 0
    if a.out:
        Path(a.out).write_text(s, encoding="utf-8")
        print("저장:", a.out)
    else:
        print("저장:", save_local(root, s))
    return 0


if __name__ == "__main__":
    sys.exit(main())
