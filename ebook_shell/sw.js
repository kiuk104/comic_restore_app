/* 설치 가능성(installability)용 최소 서비스 워커 — fetch 개입 없음 */
self.addEventListener("install", function (e) { self.skipWaiting(); });
self.addEventListener("activate", function (e) {
  e.waitUntil(self.clients.claim());
});
