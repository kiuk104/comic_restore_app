# ebook_shell — 이북 검수 PWA 셸

GAS 검수 페이지를 주소창 없는 standalone 앱으로 여는 얇은 껍데기.
서재(책 목록)를 자체 렌더하고, 책을 열면 GAS 페이지를 전체화면 iframe으로 품는다.
비밀키는 이 저장소에 없음 — 폰 localStorage에만 저장.

## 배포 (1회, GitHub Pages)

1. github.com 에서 **공개** 저장소 생성 (예: `ebook-shell`)
2. 이 폴더의 5개 파일 업로드 (웹에서 드래그&드롭 가능):
   index.html · manifest.json · sw.js · icon-192.png · icon-512.png
3. 저장소 Settings → Pages → Source: "Deploy from a branch",
   Branch: main / (root) → Save
4. 1~2분 후 `https://<계정>.github.io/ebook-shell/` 접속 가능

## 폰 설정

- 최초 1회: 셸 접속 → 설정 화면에 GAS 배포 URL(…/exec)과 동기화 키 입력
  (또는 `…/ebook-shell/?gas=<exec URL>&key=<키>` 로 열면 자동 저장)
- Chrome 메뉴 → "홈 화면에 추가"(설치) → 이후 아이콘 실행 시 주소창 없이 열림
- ⌂ 버튼: 서재로 / ⚙: 설정 재입력 / 마지막 책 자동 재진입

## 전제

- ebook_gas_sync.gs 에 op:list + XFrameOptions ALLOWALL 반영본 배포 필요
- 목록 API(fetch)가 CORS로 막히면 GAS 내장 목록 페이지 iframe으로 자동 폴백
