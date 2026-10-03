# -*- coding: utf-8 -*-
"""ebook_kindle.py — Fokus Viewer → 킨들(USB 드라이브형: 오아시스 등) 갱신 + My Clippings 회수.

  push   : Fokus EPUB → Calibre ebook-convert → AZW3 → <Kindle>/documents/Fokus/<제목>.azw3 덮어쓰기
           (같은 파일명 유지 → 킨들의 <제목>.sdr 읽은 위치 보존)
  clips  : <Kindle>/documents/My Clippings.txt 파싱 → 책 제목으로 거르고 → 메모 달린 하이라이트를
           book.json 문단(i, off)에 매칭 → 교정 대기 목록. 처리한 항목 키는 호출 측이 book["kindle_seen"]에 보관.

CLI:
  python ebook_kindle.py status
  python ebook_kindle.py push  <book.epub> [--title 제목] [--kindle E:\\] [--profile kindle_oasis]
  python ebook_kindle.py clips <book.json 또는 _book 폴더> [--kindle E:\\] [--all]
"""
import os, re, io, sys, json, shutil, struct, zipfile, hashlib, posixpath, subprocess, tempfile, \
    unicodedata, string

SUBDIR = "Fokus"                 # documents 아래 Fokus 전용 폴더
MIN_CALIBRE = (5, 0)
FIX_WORDS = None                 # None = 메모가 있으면 전부 교정 후보. 예: ("오타", "합치", "나누", "?")

# ───────── Calibre ─────────

def find_ebook_convert(cfg=None):
    """cfg['calibre_convert'] → PATH → 기본 설치 폴더 순."""
    cand = []
    if cfg and cfg.get("calibre_convert"):
        cand.append(cfg["calibre_convert"])
    w = shutil.which("ebook-convert")
    if w:
        cand.append(w)
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
        for d in ("Calibre2", "Calibre", "calibre"):
            cand.append(os.path.join(base, d, "ebook-convert.exe"))
    cand.append(os.path.expanduser(r"~\AppData\Local\Programs\Calibre Portable\Calibre\ebook-convert.exe"))
    for c in cand:
        if c and os.path.isfile(c):
            return c
    return None


def calibre_version(exe):
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True,
                             timeout=60, **_nowin()).stdout
    except Exception:
        return None
    m = re.search(r"calibre\s+(\d+)\.(\d+)(?:\.(\d+))?", out or "")
    return tuple(int(x or 0) for x in m.groups()) if m else None


def _nowin():
    # Windows에서 콘솔 창이 번쩍이지 않게
    if os.name == "nt":
        return {"creationflags": 0x08000000}
    return {}

# ───────── 킨들 드라이브 ─────────

def _vol_label(root):
    if os.name != "nt":
        return ""
    import ctypes
    buf = ctypes.create_unicode_buffer(261)
    ok = ctypes.windll.kernel32.GetVolumeInformationW(
        ctypes.c_wchar_p(root), buf, 261, None, None, None, None, 0)
    return buf.value if ok else ""


def find_kindle(cfg=None):
    """USB 드라이브형 킨들의 루트(E:\\ 등). 볼륨 이름 'Kindle' 또는 documents+system 폴더로 판별."""
    if cfg and cfg.get("kindle_root") and os.path.isdir(os.path.join(cfg["kindle_root"], "documents")):
        return cfg["kindle_root"]
    roots = []
    if os.name == "nt":
        roots = [f"{d}:\\" for d in string.ascii_uppercase[3:]]
    else:  # 테스트/맥·리눅스
        for base in ("/Volumes", "/media", f"/media/{os.environ.get('USER', '')}", "/run/media"):
            if os.path.isdir(base):
                roots += [os.path.join(base, n) for n in os.listdir(base)]
    for r in roots:
        try:
            if not os.path.isdir(os.path.join(r, "documents")):
                continue
            if _vol_label(r).lower() == "kindle" or os.path.isdir(os.path.join(r, "system")) \
                    or os.path.basename(r.rstrip("\\/")).lower() == "kindle":
                return r
        except OSError:
            continue
    return None

# ───────── 파일명 ─────────

def safe_name(title):
    t = unicodedata.normalize("NFC", (title or "book").strip())
    t = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", t)
    t = re.sub(r"\s+", " ", t).strip(" .") or "book"
    return t[:80]

# ───────── 제목·저자·폰트·표지 ─────────

APP_DIR = os.path.dirname(os.path.abspath(sys.argv[0] if getattr(sys, "frozen", False) else __file__))

FONT_CHOICES = (   # Fokus Viewer 보기 설정과 같은 이름 (킨들은 TTF/OTF만 — 바탕·돋움 TTC는 제외)
    ("ridi", "리디바탕 (명조)", ("리디바탕.ttf", "RIDIBatang.ttf", "RIDIBatang.otf")),
    ("scd3", "에스코어드림 Light", ("에스코어 드림 3 Light.ttf", "SCDream3.otf", "S-CoreDream-3Light.ttf")),
    ("scd6", "에스코어드림 Bold", ("에스코어 드림 6 Bold.ttf", "SCDream6.otf", "S-CoreDream-6Bold.ttf")),
    ("nmj", "나눔명조", ("NanumMyeongjo.ttf", "나눔명조.ttf", "NanumMyeongjoRegular.ttf")),
    ("ngo", "나눔고딕", ("NanumGothic.ttf", "나눔고딕.ttf", "NanumGothicRegular.ttf")),
)


def _font_dirs():
    d = [os.path.join(APP_DIR, "fonts")]
    if os.environ.get("WINDIR"):
        d.append(os.path.join(os.environ["WINDIR"], "Fonts"))
    if os.environ.get("LOCALAPPDATA"):
        d.append(os.path.join(os.environ["LOCALAPPDATA"], "Microsoft", "Windows", "Fonts"))
    d += ["/usr/share/fonts/truetype/nanum", os.path.expanduser("~/.fonts")]
    return [x for x in d if os.path.isdir(x)]


def find_font(key):
    """FONT_CHOICES 키 → TTF/OTF 경로 (없으면 None). 'none'/'' = 킨들 기본 글꼴."""
    if not key or key == "none":
        return None
    if os.path.isfile(key):
        return key
    for k, _lbl, names in FONT_CHOICES:
        if k != key:
            continue
        for d in _font_dirs():
            for n in names:
                fp = os.path.join(d, n)
                if os.path.isfile(fp):
                    return fp
    return None


def font_list():
    return [{"key": k, "label": lbl, "ok": bool(find_font(k))} for k, lbl, _n in FONT_CHOICES]


def split_title(t):
    """'앵무새 죽이기 [하퍼 리]' / '… (하퍼 리)' → ('앵무새 죽이기', '하퍼 리')."""
    t = (t or "").strip()
    m = re.match(r"^(.*?)\s*[\[(（]([^\[\]()（）]{1,40})[\])）]\s*$", t)
    return (m.group(1).strip(), m.group(2).strip()) if m and m.group(1).strip() else (t, "")


def _wrap(draw, text, font, maxw):
    """단어 단위 줄바꿈 — 한 단어가 너무 길면 글자 단위로 쪼갠다."""
    fits = lambda t: draw.textlength(t, font=font) <= maxw
    lines, cur = [], ""
    for tok in re.findall(r"\S+", text or ""):
        test = (cur + " " + tok) if cur else tok
        if fits(test):
            cur = test
            continue
        if cur:
            lines.append(cur); cur = ""
        while tok and not fits(tok):          # 긴 단어 쪼개기
            k = len(tok)
            while k > 1 and not fits(tok[:k]):
                k -= 1
            lines.append(tok[:k]); tok = tok[k:]
        cur = tok
    if cur:
        lines.append(cur)
    return lines


def _glyph_safe(text, font):
    """폰트에 없는 글자(빈 칸으로 찍히는 — 등)를 비슷한 글자로 바꾼다."""
    def has(ch):
        try:
            return ch.isspace() or font.getmask(ch).getbbox() is not None
        except Exception:
            return True
    alt = {"—": "―-", "–": "-", "…": ".", "·": "・.", "「": "\"", "」": "\"", "『": "\"", "』": "\""}
    out = []
    for ch in text or "":
        if not has(ch):
            ch = next((c for c in alt.get(ch, "") if has(c)), "")
        out.append(ch)
    return "".join(out)


def make_cover(title, author="", font_path=None, size=(1264, 1680)):
    """글자 표지 JPEG (e-ink 흑백 기준). 스캔 표지가 없는 평문 책용."""
    from PIL import Image, ImageDraw, ImageFont
    W, H = size
    img = Image.new("L", size, 246)
    d = ImageDraw.Draw(img)
    tf = find_font("scd6") or font_path
    bf = font_path or find_font("ridi") or tf

    def F(path, px):
        try:
            return ImageFont.truetype(path, px) if path else ImageFont.load_default()
        except Exception:
            return ImageFont.load_default()
    m = int(W * 0.12)
    d.rectangle([m // 2, m // 2, W - m // 2, H - m // 2], outline=40, width=4)
    d.rectangle([m // 2 + 14, m // 2 + 14, W - m // 2 - 14, H - m // 2 - 14], outline=120, width=1)
    px = 150
    while px > 60:
        f = F(tf, px)
        lines = _wrap(d, _glyph_safe(title or "제목 없음", f), f, W - 2 * m)
        if len(lines) <= 4:
            break
        px -= 10
    lh = int(px * 1.28)
    y = max(m + 60, int(H * 0.30) - (len(lines) * lh) // 2)
    for ln in lines:
        d.text(((W - d.textlength(ln, font=f)) / 2, y), ln, font=f, fill=20)
        y += lh
    y += int(px * 0.5)
    d.line([(W / 2 - 90, y), (W / 2 + 90, y)], fill=60, width=4)
    if author:
        fa = F(bf, 64)
        author = _glyph_safe(author, fa)
        y += 60
        d.text(((W - d.textlength(author, font=fa)) / 2, y), author, font=fa, fill=50)
    fs = F(bf, 34)
    lab = "Fokus Viewer"
    d.text(((W - d.textlength(lab, font=fs)) / 2, H - m - 40), lab, font=fs, fill=130)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    return buf.getvalue()


def fit_cover(data, size=(1264, 1680)):
    """사용자 이미지/스캔 표지 → 킨들 비율 JPEG(흑백, 남는 곳은 흰 여백)."""
    from PIL import Image, ImageOps
    im = Image.open(io.BytesIO(data))
    im = ImageOps.exif_transpose(im).convert("L")
    im = ImageOps.contain(im, size)
    bg = Image.new("L", size, 255)
    bg.paste(im, ((size[0] - im.width) // 2, (size[1] - im.height) // 2))
    buf = io.BytesIO()
    bg.save(buf, "JPEG", quality=90)
    return buf.getvalue()


def _opf_path(z):
    c = z.read("META-INF/container.xml").decode("utf-8", "replace")
    return re.search(r'full-path="([^"]+)"', c).group(1)


def embed_font(epub_in, epub_out, font_path, family="FokusFont"):
    """EPUB 사본에 폰트+CSS를 넣어 본문 전체에 적용 (킨들 Aa → '출판사 글꼴'에서 보임)."""
    ext = os.path.splitext(font_path)[1].lower()
    mt = "application/vnd.ms-opentype" if ext == ".otf" else "application/x-font-truetype"
    with zipfile.ZipFile(epub_in) as zi:
        opf = _opf_path(zi)
        base = posixpath.dirname(opf)
        J = lambda x: posixpath.join(base, x) if base else x
        odata = zi.read(opf).decode("utf-8")
        css = ('@font-face{font-family:"%s";src:url(fokus_font%s);}\n'
               'body,p,div,span,li,blockquote,h1,h2,h3,h4{font-family:"%s",serif;}\n') % (family, ext, family)
        items = re.findall(r'<item\b[^>]*>', odata)
        xhtml = []
        for it in items:
            if "application/xhtml+xml" in it:
                h = re.search(r'href="([^"]+)"', it)
                if h:
                    xhtml.append(J(h.group(1)))
        odata = odata.replace("</manifest>",
            f'<item id="fokusfont" href="fokus_font{ext}" media-type="{mt}"/>\n'
            '<item id="fokuscss" href="fokus.css" media-type="text/css"/>\n</manifest>', 1)
        with zipfile.ZipFile(epub_out, "w") as zo:
            zo.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
            for info in zi.infolist():
                n = info.filename
                if n == "mimetype":
                    continue
                data = zi.read(n)
                if n == opf:
                    data = odata.encode("utf-8")
                elif n in xhtml:
                    rel = posixpath.relpath(J("fokus.css"), posixpath.dirname(n) or ".")
                    t = data.decode("utf-8", "replace")
                    t = re.sub(r"</head>", f'<link rel="stylesheet" type="text/css" href="{rel}"/></head>', t, 1)
                    data = t.encode("utf-8")
                zo.writestr(n, data, compress_type=zipfile.ZIP_DEFLATED)
            zo.writestr(J("fokus.css"), css, compress_type=zipfile.ZIP_DEFLATED)
            with open(font_path, "rb") as f:
                zo.writestr(J("fokus_font" + ext), f.read(), compress_type=zipfile.ZIP_DEFLATED)
    return epub_out

# ───────── AZW3 메타(EXTH) 읽기 — 썸네일 이름용 ─────────

def read_exth(path):
    with open(path, "rb") as f:
        data = f.read(1 << 16)
    off0 = struct.unpack(">I", data[78:82])[0]
    r0 = data[off0:]
    if r0[16:20] != b"MOBI":
        return {}
    hlen = struct.unpack(">I", r0[20:24])[0]
    if not (struct.unpack(">I", r0[0x80:0x84])[0] & 0x40):
        return {}
    e = 16 + hlen
    if r0[e:e + 4] != b"EXTH":
        return {}
    cnt = struct.unpack(">I", r0[e + 8:e + 12])[0]
    p, out = e + 12, {}
    for _ in range(cnt):
        t, ln = struct.unpack(">II", r0[p:p + 8])
        out.setdefault(t, r0[p + 8:p + ln].decode("utf-8", "replace"))
        p += ln
    return out


def write_thumbnail(root, azw, cover_jpg, log=print):
    """킨들 홈 화면 표지 — system/thumbnails/thumbnail_<ASIN>_<cdetype>_portrait.jpg.
    (USB로 넣은 책은 이게 없으면 표지가 빈 칸으로 보이는 경우가 있음)"""
    try:
        from PIL import Image
        ex = read_exth(azw)
        asin, cdt = ex.get(113) or ex.get(504), ex.get(501) or "EBOK"
        if not asin:
            return None
        td = os.path.join(root, "system", "thumbnails")
        if not os.path.isdir(td):
            return None
        im = Image.open(io.BytesIO(cover_jpg)).convert("L")
        im.thumbnail((330, 470))
        fp = os.path.join(td, f"thumbnail_{asin}_{cdt}_portrait.jpg")
        im.save(fp, "JPEG", quality=85)
        return fp
    except Exception as e:
        log(f"(썸네일 생략: {e})")
        return None


def stable_asin(name):
    """파일명 기준 고정 UUID — Calibre가 매번 무작위 UUID를 ASIN으로 쓰므로 교체용."""
    import uuid
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "fokus-viewer:" + safe_name(name)))


def set_asin(path, new):
    """AZW3 EXTH 113/504의 ASIN을 같은 길이 문자열로 제자리 교체 (오프셋 불변)."""
    ex = read_exth(path)
    old = ex.get(113) or ex.get(504)
    if not old or old == new or len(old.encode()) != len(new.encode()):
        return old == new
    with open(path, "r+b") as f:
        head = f.read(1 << 16)
        n = head.count(old.encode())
        f.seek(0)
        f.write(head.replace(old.encode(), new.encode()))
    return n > 0

# ───────── push ─────────

def push(epub, title=None, cfg=None, log=print, profile=None,
         author=None, cover=None, font=None, name=None):
    """EPUB → (폰트 주입) → AZW3(제목·저자·표지) → 킨들 덮어쓰기. 반환: 킨들 안 대상 경로.

    name  : 킨들 파일명(확장자 제외) — 바꾸지 말 것(읽은 위치 .sdr이 파일명 기준)
    title : 킨들에 보일 제목, author: 저자, cover: JPEG bytes, font: TTF/OTF 경로"""
    cfg = cfg or {}
    if not os.path.isfile(epub):
        raise FileNotFoundError(f"EPUB 없음: {epub}")
    exe = find_ebook_convert(cfg)
    if not exe:
        raise RuntimeError("Calibre ebook-convert.exe를 찾지 못했습니다 (설정 calibre_convert에 경로 지정 가능).")
    ver = calibre_version(exe)
    if ver and ver[:2] < MIN_CALIBRE:
        log(f"⚠ Calibre {'.'.join(map(str, ver))} — 오래된 버전이라 한글·EPUB3 변환이 깨질 수 있습니다. 업데이트 권장.")
    root = find_kindle(cfg)
    if not root:
        raise RuntimeError("킨들을 찾지 못했습니다 — USB로 연결하고 'Kindle' 드라이브가 보이는지 확인하세요.")

    name = safe_name(name or title or os.path.splitext(os.path.basename(epub))[0])
    dst_dir = os.path.join(root, "documents", SUBDIR)
    os.makedirs(dst_dir, exist_ok=True)
    dst = os.path.join(dst_dir, name + ".azw3")

    tmpd = tempfile.mkdtemp(prefix="fv_kindle_")
    try:
        src = epub
        if font:
            src = embed_font(epub, os.path.join(tmpd, "in.epub"), font)
            log(f"🔤 폰트 넣음: {os.path.basename(font)}")
        out = os.path.join(tmpd, "book.azw3")
        args = [exe, src, out,
                "--output-profile", profile or cfg.get("kindle_profile") or "kindle_oasis",
                "--no-inline-toc"]
        if title:
            args += ["--title", title]
        if author:
            args += ["--authors", author]
        if cover:
            cp = os.path.join(tmpd, "cover.jpg")
            with open(cp, "wb") as f:
                f.write(cover)
            args += ["--cover", cp]
        if font:
            args += ["--subset-embedded-fonts"]       # 쓰인 글자만 남겨 용량 축소
        log(f"📲 AZW3 변환 중… ({os.path.basename(exe)} {'.'.join(map(str, ver or ()))})")
        p = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=900, **_nowin())
        if p.returncode != 0 or not os.path.isfile(out):
            tail = "\n".join(((p.stderr or "") + (p.stdout or "")).strip().splitlines()[-12:])
            raise RuntimeError("ebook-convert 실패:\n" + tail)
        # ASIN 고정 — 다시 보내도 같은 책으로 인식(썸네일·위치 안정). 실패해도 진행.
        try:
            set_asin(out, stable_asin(name))
        except Exception as e:
            log(f"(ASIN 고정 생략: {e})")
        # 같은 이름으로 원자적 교체 (복사 중 뽑혀도 기존 파일은 살아 있게)
        part = dst + ".part"
        shutil.copyfile(out, part)
        os.replace(part, dst)
        if cover:
            write_thumbnail(root, dst, cover, log)
        log(f"✅ 킨들 갱신: documents/{SUBDIR}/{name}.azw3 ({os.path.getsize(dst)//1024} KB) — 읽은 위치는 유지됩니다.")
        return dst
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)


# ───────── My Clippings ─────────

_SEP = "=========="
_KIND = (  # 킨들 UI 언어별 표기 (영·독·한·일·불·서)
    ("note", ("note", "notiz", "메모", "メモ", "remarque", "nota")),
    ("bookmark", ("bookmark", "lesezeichen", "북마크", "ブックマーク", "signet", "marcador")),
    ("highlight", ("highlight", "markierung", "하이라이트", "ハイライト", "surlignement", "subrayado")),
)
_LOC_WORDS = r"(?:location|position|위치|位置No\.|emplacement|posición|pos\.)"


def _kind_of(meta):
    m = meta.lower()
    for k, words in _KIND:
        if any(w in m for w in words):
            return k
    return "highlight"


def _loc_of(meta):
    """'Location 123-125' / 'Position 123-125' / '위치 123-125' → (123,125). 없으면 페이지 숫자라도."""
    m = re.search(_LOC_WORDS + r"\s*(\d+)(?:\s*-\s*(\d+))?", meta, re.I) \
        or re.search(r"(\d+)(?:\s*-\s*(\d+))?\s*(?:위치|位置)", meta)
    if not m:
        nums = re.findall(r"\d+", meta.split("|")[0])
        if not nums:
            return (0, 0)
        a = int(nums[-1]); return (a, a)
    a = int(m.group(1)); b = int(m.group(2) or a)
    if m.group(2) and len(m.group(2)) < len(m.group(1)):     # '1234-36' 축약
        b = int(m.group(1)[:-len(m.group(2))] + m.group(2))
    return (a, b)


def parse_clippings(path):
    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        raw = f.read()
    out = []
    for blk in raw.split(_SEP):
        lines = [l.rstrip("\r") for l in blk.strip("\r\n").split("\n")]
        while lines and not lines[0].strip():
            lines.pop(0)
        if len(lines) < 2:
            continue
        book = lines[0].strip().lstrip("\ufeff")
        meta = lines[1].strip()
        text = "\n".join(lines[2:]).strip()
        out.append({"book": book, "kind": _kind_of(meta), "loc": _loc_of(meta), "text": text,
                    "key": hashlib.sha1((book + "\n" + meta + "\n" + text).encode("utf-8")).hexdigest()[:16]})
    return out


def pair_notes(clips):
    """메모를 같은 책의 하이라이트(메모 위치가 하이라이트 범위 끝 근처)에 붙인다."""
    res = []
    hls = [c for c in clips if c["kind"] == "highlight"]
    used = set()
    for n in (c for c in clips if c["kind"] == "note"):
        best = None
        for h in hls:
            if h["book"] != n["book"] or id(h) in used:
                continue
            a, b = h["loc"]
            if a - 1 <= n["loc"][0] <= b + 1:
                best = h
                if n["loc"][0] == b:
                    break
        if best:
            used.add(id(best))
        res.append({"book": n["book"], "note": n["text"], "text": best["text"] if best else "",
                    "loc": (best or n)["loc"], "key": n["key"]})
    return res

# ───────── 문단 매칭 ─────────

_MARK = re.compile(r"^#{1,6}\s+|\*\*|~~|\+\+|>>|(?<!\*)\*(?!\*)")


def _norm(s):
    s = unicodedata.normalize("NFC", _MARK.sub("", s or ""))
    return re.sub(r"\s+", " ", s).strip()


def match_para(text, paras):
    """하이라이트 글 → (문단 i, 표시 글 안 off). 못 찾으면 (None, None)."""
    t = _norm(text)
    if not t:
        return (None, None)
    disp = [_norm(p) for p in paras]
    for probe in (t, t[:40], t[-40:], t[:20]):
        if len(probe) < 6:
            continue
        for i, d in enumerate(disp):
            k = d.find(probe)
            if k >= 0:
                if probe is t[-40:]:
                    k = max(0, k - (len(t) - len(probe)))
                return (i, k)
    # 하이라이트가 문단 경계를 넘은 경우: 앞 문단 끝 + 다음 문단 시작
    for i in range(len(disp) - 1):
        j = disp[i] + " " + disp[i + 1]
        k = j.find(t[:30])
        if k >= 0:
            return (i, min(k, len(disp[i])))
    return (None, None)


def _load_book(p):
    if os.path.isdir(p):
        cand = os.path.join(p, "_work", "book.json")
        p = cand if os.path.isfile(cand) else os.path.join(p, "book.json")
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f), p


def book_texts(book, done=None):
    """읽기 화면 글 = 번역||원문 (평문 책은 src)."""
    done = done or {}
    out = []
    for i, para in enumerate(book.get("paras", [])):
        x = done.get(str(i)) or done.get(i)
        out.append(x if (x and book.get("kind") != "plain") else para.get("src", ""))
    return out


def collect_fixes(book, clippings_path, title=None, done=None, include_seen=False):
    """이 책의 메모 달린 하이라이트 → 교정 후보 [{key,i,off,text,note,loc}]."""
    titles = title if isinstance(title, (list, tuple)) else [title or book.get("title") or ""]
    want = set()
    for t in titles:
        if t:
            want |= {_norm(t).lower(), _norm(safe_name(t)).lower()}
    clips = parse_clippings(clippings_path)
    seen = set(book.get("kindle_seen") or [])
    texts = book_texts(book, done)
    out = []
    for n in pair_notes(clips):
        bt = _norm(re.sub(r"\s*\([^()]*\)\s*$", "", n["book"])).lower()   # '제목 (저자)' → 제목
        if bt not in want:
            continue
        if not include_seen and n["key"] in seen:
            continue
        if FIX_WORDS and not any(w in n["note"] for w in FIX_WORDS):
            continue
        i, off = match_para(n["text"], texts) if n["text"] else (None, None)
        out.append({"key": n["key"], "i": i, "off": off, "text": n["text"],
                    "note": n["note"], "loc": list(n["loc"])})
    out.sort(key=lambda x: (x["i"] is None, x["i"] or 0, x["off"] or 0))
    return out


def mark_seen(book, keys):
    s = list(dict.fromkeys(list(book.get("kindle_seen") or []) + list(keys)))
    book["kindle_seen"] = s[-2000:]
    return book

# ───────── CLI ─────────

def _arg(argv, name, default=None):
    if name in argv:
        k = argv.index(name)
        if k + 1 < len(argv):
            return argv[k + 1]
    return default


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__); return 0
    cmd, cfg = argv[0], {}
    if _arg(argv, "--kindle"):
        cfg["kindle_root"] = _arg(argv, "--kindle")
    if cmd == "status":
        exe = find_ebook_convert(cfg)
        print("Calibre :", exe or "없음", calibre_version(exe) if exe else "")
        print("Kindle  :", find_kindle(cfg) or "연결 안 됨")
        return 0
    if cmd == "push" and len(argv) > 1:
        push(argv[1], _arg(argv, "--title"), cfg, profile=_arg(argv, "--profile"))
        return 0
    if cmd == "clips" and len(argv) > 1:
        book, bp = _load_book(argv[1])
        root = find_kindle(cfg)
        cp = os.path.join(root, "documents", "My Clippings.txt") if root else _arg(argv, "--file")
        if not cp or not os.path.isfile(cp):
            print("My Clippings.txt 없음 (킨들 연결 또는 --file 경로)"); return 1
        done = {}
        xp = os.path.join(os.path.dirname(bp), "xlat.json")
        if os.path.isfile(xp):
            with open(xp, "r", encoding="utf-8") as f:
                done = json.load(f)
        fx = collect_fixes(book, cp, done=done, include_seen="--all" in argv)
        for x in fx:
            where = f"#{x['i']}" if x["i"] is not None else "?? (못 찾음)"
            print(f"{where:>8}  [{x['note']}]  {x['text'][:60]}")
        print(f"— 교정 후보 {len(fx)}개")
        return 0
    print(__doc__); return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]) or 0)
