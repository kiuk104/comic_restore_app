# -*- coding: utf-8 -*-
"""성경 전사 진행 상황 보기 — 구약·신약 폴더의 엔진별 진행률과 남은 시간 (30초마다 새로 고침).
전사 창과 따로 실행. 읽기만 하므로 언제 켜고 꺼도 전사에 영향 없음."""
import os, sys, time, glob, re
from pathlib import Path

ROOT = Path(r"E:\Coding\capture_tool")
FOLDERS = sys.argv[1:] or [str(ROOT / "02_구약"), str(ROOT / "03_신약")]
ENGINES = ["winocr", "tesseract", "deepseek", "gemini"]
NAMES = {"winocr": "Windows OCR", "tesseract": "Tesseract", "deepseek": "DeepSeek", "gemini": "Gemini"}
DEFAULT_RATE = {"winocr": 60.0, "tesseract": 20.0, "deepseek": 13.0, "gemini": 20.0}   # 장/분, 측정 전 어림값
WINDOW = 600  # 최근 10분 동안 저장된 페이지로 속도 측정


def fmt_min(m):
    if m is None:
        return "-"
    m = int(round(m))
    return f"{m // 60}시간 {m % 60:02d}분" if m >= 60 else f"{m}분"


def bar(p, w=24):
    n = int(round(p * w))
    return "█" * n + "░" * (w - n)


def engine_state(folder, e, total, now):
    log = Path(folder) / f"log_{e}.txt"
    pages = Path(folder) / f"out_{e}" / "_work" / "pages"
    files = list(pages.glob("page_*.txt")) if pages.exists() else []
    done = len(files)
    txt = ""
    if log.exists():
        try:
            txt = log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
    fails = len(set(re.findall(r"!! (page_\d+)\.png 전사 실패", txt)))
    ts = sorted(t for t in (f.stat().st_mtime for f in files) if now - t < WINDOW)
    recent = len(ts)
    age = now - log.stat().st_mtime if log.exists() else None
    if not log.exists():
        st = "대기"
    elif "완료:" in txt or "Traceback" in txt:
        st = "끝남" if "완료:" in txt else "오류로 멈춤"
    elif age is not None and age > 900 and recent == 0:
        st = "멈춘 듯함"
    else:
        st = "진행 중"
    # 속도: 최근 10분 안에 저장된 페이지들의 첫~마지막 간격으로 계산 (막 시작했어도 정확)
    span = (max(ts[-1], now - 60) - ts[0]) / 60 if len(ts) >= 3 else 0
    rate = (len(ts) - 1) / span if span >= 1 else None
    return dict(done=done, fails=fails, st=st, rate=rate)


def show():
    now = time.time()
    out = [f"성경 전사 진행 상황  ({time.strftime('%H:%M:%S')}, 30초마다 새로 고침 · 닫아도 전사는 계속됨)", ""]
    for folder in FOLDERS:
        imgs = glob.glob(os.path.join(folder, "page_*.png"))
        total = len(imgs)
        out.append(f"■ {Path(folder).name}  (캡처 {total}장)")
        if not total:
            out.append("   캡처 파일이 없습니다"); out.append(""); continue
        left_total = 0.0
        busy = False
        for e in ENGINES:
            s = engine_state(folder, e, total, now)
            if busy and s["st"] != "진행 중":   # 앞 엔진이 도는 중 = 이 엔진은 차례를 기다림 (이전 실행 기록은 무시)
                s["st"], s["fails"] = "대기", 0
            if s["st"] == "진행 중":
                busy = True
            p = min(1.0, s["done"] / total)
            remain = max(0, total - s["done"])
            line = f"   {NAMES[e]:<12} {bar(p)} {p*100:5.1f}%  {s['done']:>5}/{total}  {s['st']}"
            if s["fails"]:
                line += f"  · 실패 {s['fails']}장"
            if s["st"] == "진행 중":
                r = s["rate"] or DEFAULT_RATE[e]
                eta = remain / r if r else None
                line += f"  · {r:.0f}장/분 · 남은 시간 약 {fmt_min(eta)}" + ("" if s["rate"] else " (어림)")
                left_total += eta or 0
            elif s["st"] == "대기":
                left_total += remain / DEFAULT_RATE[e]
            out.append(line)
        if left_total:
            out.append(f"   → 이 폴더 전체 남은 시간 대략 {fmt_min(left_total)} (끝나는 시각 약 "
                       f"{time.strftime('%H:%M', time.localtime(now + left_total * 60))})")
        elif all(engine_state(folder, e, total, now)['st'] == '끝남' for e in ENGINES):
            out.append("   → 전사 끝")
        out.append("")
    out.append("실패한 페이지는 전사가 모두 끝난 뒤 같은 bat 파일을 다시 실행하면 그 페이지만 다시 합니다.")
    out.append("Gemini 실패(RECITATION)는 저작권 필터라 다시 해도 대부분 그대로입니다 — OCR 결과로 채워집니다.")
    os.system("cls" if os.name == "nt" else "clear")
    print("\n".join(out), flush=True)


if __name__ == "__main__":
    try:
        while True:
            show()
            time.sleep(30)
    except KeyboardInterrupt:
        pass
