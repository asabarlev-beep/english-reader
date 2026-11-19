// Service worker: keeps the app, the dictionary and downloaded books on the phone.
const VERSION = '__BUILD__';
const SHELL = 'shell-' + VERSION;
const BOOKS = 'books-v1';            // stays the same across updates, so downloaded books survive
const SHELL_FILES = ['./', 'index.html', 'manifest.json', 'icon-192.png', 'icon-512.png',
  'data/books.json', 'data/dict.json'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(SHELL).then(c => c.addAll(SHELL_FILES)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys()
    .then(keys => Promise.all(keys.filter(k => k.startsWith('shell-') && k !== SHELL).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;           // online dictionary lookups go straight to the network

  if (url.pathname.includes('/books/')) {               // books: from the phone first, save when read online
    e.respondWith(caches.open(BOOKS).then(async c => {
      const hit = await c.match(req, { ignoreSearch: true });
      if (hit) return hit;
      const res = await fetch(req);
      if (res.ok) c.put(req, res.clone());
      return res;
    }));
    return;
  }

  e.respondWith(caches.match(req, { ignoreSearch: true }).then(hit => {
    if (hit) return hit;
    return fetch(req).catch(() => req.mode === 'navigate' ? caches.match('index.html') : Response.error());
  }));
});
