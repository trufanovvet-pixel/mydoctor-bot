'use strict';
const CACHE = 'mydoctor-public-app-v2';
const OFFLINE = '/static/offline.html';
const PUBLIC_ASSETS = new Set([OFFLINE, '/app-icon/192.png', '/app-icon/512.png', '/app-icon/96.png']);
self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll([...PUBLIC_ASSETS])).then(() => self.skipWaiting()));
});
self.addEventListener('activate', event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(key => key.startsWith('mydoctor-public-app-') && key !== CACHE).map(key => caches.delete(key)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', event => {
  const req = event.request;
  const url = new URL(req.url);
  if (req.method !== 'GET' || url.origin !== self.location.origin) return;
  if (req.mode === 'navigate') {
    // Never store authenticated pages, messages, documents or failed form submissions.
    event.respondWith(fetch(req).catch(() => caches.match(OFFLINE)));
  } else if (PUBLIC_ASSETS.has(url.pathname) && !url.search) {
    event.respondWith(caches.match(req).then(cached => cached || fetch(req)));
  }
});
self.addEventListener('push', event => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (_) { /* Generic fallback. */ }
  const english = data.language === 'en';
  // The server and worker both use generic wording; no clinical message previews.
  event.waitUntil(self.registration.showNotification(english ? 'MyDoctor' : 'МойДоктор', {
    body: english ? 'You have a new message. Open your conversation.' : 'У вас новое сообщение. Откройте переписку.',
    icon: '/app-icon/192.png', badge: '/app-icon/96.png', tag: data.tag || 'mydoctor-message',
    data: {url: data.url || '/messages'}
  }));
});
self.addEventListener('notificationclick', event => {
  event.notification.close();
  const path = event.notification.data && event.notification.data.url;
  const safe = typeof path === 'string' && /^\/(?:doctor|consultation)\/requests\/[1-9]\d*#conversation$/.test(path);
  const target = new URL(safe ? path : '/messages', self.location.origin).href;
  event.waitUntil(self.clients.matchAll({type: 'window', includeUncontrolled: true}).then(async windows => {
    // Do not navigate an unrelated open form and discard its unsent draft.
    const same = windows.find(client => client.url.split('#')[0] === target.split('#')[0]);
    if (same) return same.focus();
    return self.clients.openWindow(target);
  }));
});
