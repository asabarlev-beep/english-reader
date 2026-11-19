// Service worker: keeps the app, the dictionary and downloaded books on the phone.
const VERSION = '__BUILD__';
const SHELL = 'shell-' + VERSION;
const BOOKS = 'books-v2';            // stays the same across updates, so downloaded books survive
const SHELL_FILES = ['./', 'index.html', 'manifest.json', 'icon-192.png', 'icon-512.png',
  'data/books.json', 'data/dict.json'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(SHELL).then(c => c.addAll(SHELL_FILES)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys()
    .then(keys => Promise.all(keys.filter(k => (k.startsWith('shell-') && k !== SHELL) || (k.startsWith('books-') && k !== BOOKS)).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;           // online dictionary lookups go straight to the network

  if (url.pathname.includes('/books/')) {               // books: from the phone first, save when read online
    e.respondWith(caches.open(BOOKS).then(async c => {
      const hit = await c.match(req);                    // exact address incl. ?v= (a new edition gets a new address)
      if (hit) return hit;
      const res = await fetch(req);
      if (res.ok) {
        await c.put(req, res.clone());
        for (const k of await c.keys()) {                // drop older editions of the same book
          const u = new URL(k.url);
          if (u.pathname === url.pathname && u.search !== url.search) c.delete(k);
        }
      }
      return res;
    }));
    return;
  }

  if (req.mode === 'navigate') {                         // the app page: newest version when there is reception
    e.respondWith((async () => {
      try {
        const res = await Promise.race([fetch(req), new Promise((_, r) => setTimeout(() => r('slow'), 3500))]);
        if (res.ok) (await caches.open(SHELL)).put('index.html', res.clone());
        return res;
      } catch (err) {
        return (await caches.match('index.html')) || (await caches.match('./')) || Response.error();
      }
    })());
    return;
  }

  e.respondWith(caches.match(req, { ignoreSearch: true }).then(hit => hit || fetch(req)));
});
