// Mavis Dashboard Service Worker
const CACHE_NAME = 'mavis-dashboard-v1';
const ESSENTIAL = [
  '/',
  '/index.html',
  '/login',
  '/manifest.json'
];

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(ESSENTIAL))
  );
  self.skipWaiting();
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);

  // 跳过 SSE（长连接）
  if (url.pathname === '/events') {
    return;
  }

  // 跳过非 GET
  if (e.request.method !== 'GET') {
    return;
  }

  // 网络优先，失败时用缓存
  e.respondWith(
    fetch(e.request)
      .then((resp) => {
        // 成功：缓存 GET 请求
        if (resp.ok && url.pathname.startsWith('/state/')) {
          const clone = resp.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(e.request, clone));
        }
        return resp;
      })
      .catch(() => caches.match(e.request))
  );
});