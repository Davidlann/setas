const CACHE_NAME = 'mapa-setero-v6';
const DATA_PATH = /\/public\/data\/experimental\/boletus_[a-z]+\.json$/;
const STATIC_ASSET_PATH = /\.(?:webmanifest|svg|png)$/;

self.addEventListener('install', event => {
  event.waitUntil(self.skipWaiting());
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(
        keys.filter(key => key.startsWith('mapa-setero-') && key !== CACHE_NAME)
          .map(key => caches.delete(key))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', event => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== 'GET' || url.origin !== self.location.origin) return;

  if (DATA_PATH.test(url.pathname)) {
    event.respondWith((async () => {
      try {
        const response = await fetch(request);
        if (response.ok) {
          const cache = await caches.open(CACHE_NAME);
          await cache.put(request, response.clone());
        }
        return response;
      } catch (error) {
        const cached = await caches.match(request);
        if (cached) return cached;
        return new Response(JSON.stringify({
          error: 'No hay datos guardados para esta especie y no se puede conectar.',
        }), {
          status: 503,
          headers: { 'Content-Type': 'application/json; charset=utf-8' },
        });
      }
    })());
    return;
  }

  if (request.mode === 'navigate') {
    event.respondWith((async () => {
      try {
        const response = await fetch(request);
        if (response.ok) {
          const cache = await caches.open(CACHE_NAME);
          await cache.put(request, response.clone());
        }
        return response;
      } catch (error) {
        const cached = await caches.match(request) ||
          await caches.match(new URL('./', self.registration.scope).href);
        if (cached) return cached;
        throw error;
      }
    })());
    return;
  }

  if (STATIC_ASSET_PATH.test(url.pathname)) {
    event.respondWith((async () => {
      try {
        const response = await fetch(request);
        if (response.ok) {
          const cache = await caches.open(CACHE_NAME);
          await cache.put(request, response.clone());
        }
        return response;
      } catch (error) {
        const cached = await caches.match(request);
        if (cached) return cached;
        throw error;
      }
    })());
  }
});
