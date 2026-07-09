# 만화 한글 복원 (Comic Restore) v0.9.0

스캔 화질이 열화된 만화의 한글 말풍선을 복원하는 도구.
업스케일(Upscayl) → Claude 비전 전사 → 재조판 → 브라우저 검수 에디터로 이어지는
전체 파이프라인을 GUI 앱 하나로 제공한다.

> 0.x 버전은 개발 단계로, 동작 방식과 데이터 구조가 바뀔 수 있음.

## 구성

| 파일 | 역할 |
|---|---|
| `comic_restore_app.py` | Tkinter GUI 앱 — 업스케일→재조판 일괄 실행, 검수 서버 내장 |
| `comic_retype_pipeline.py` | 핵심 파이프라인 (v3) — 감지·전사·재조판·검수 페이지 |
| `comic_restore_pipeline.py` | v2 비파괴 톤 보정 (감지 전처리로 사용) |
| `run_app.bat` | 앱 실행 |
| `create_shortcut.bat` | 바탕화면·시작메뉴 바로가기 생성 (작업표시줄 고정용) |

## 주요 기능

- 말풍선 감지 후 Claude 비전으로 열화 한글 전사 — 이중 전사 + 불일치 3차 검증
- Batch API 지원(비용 50% 절감), 이어하기(중단 지점 재개), 로컬 OCR 대체 엔진
- 원본 화질 100% 보존 합성 — 재조판 말풍선 내부만 종이색으로 정밀 수정
- 페이지별 PSD 출력 (원본/지움/말풍선별 텍스트 레이어)
- 브라우저 검수 에디터: 클릭 마킹, 텍스트 수동 교정, 트랜스폼(이동·크기·장평),
  자간·줄간격·점간격·정렬, 폰트 지정·서식 복사·기본 서식, 브러시 칠/복원,
  영역 편집, 수동 영역 추가, 잠금, 적용 되돌리기, 즉시 적용(내장 서버)

## 설치

```
pip install opencv-python numpy pillow psd-tools anthropic
```

- [Upscayl](https://upscayl.org) 설치 (자동 감지)
- 나눔명조 Bold 등 한글 폰트 (프로젝트 `fonts/` 폴더 또는 OS 설치)
- ANTHROPIC_API_KEY (앱에 입력) — 또는 로컬 OCR 엔진 선택

## 사용

1. `run_app.bat` 실행
2. 원본 폴더 지정 → [전체 시작]
3. [검수 페이지]에서 결과 확인·교정 → [✔ 이 페이지 적용]

주의: `app_config.json`에 API 키가 저장될 수 있으므로 커밋 금지(.gitignore 처리됨).
