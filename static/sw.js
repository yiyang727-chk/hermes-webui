/**
 * Hermes WebUI Service Worker
 * Minimal PWA service worker — enables "Add to Home Screen".
 * No offline caching of API responses (the UI requires a live backend).
 * Caches only static shell assets so the app shell loads fast on repeat visits.
 */

// Cache version is injected by the server at request time (routes.py /sw.js handler).
// Bumps automatically whenever the git commit changes — no manual edits needed.
const CACHE_NAME = 'hermes-shell-__CACHE_VERSION__';

// Static assets that form the app shell
const SHELL_ASSETS = [
  './',
  './static/style.css',
  './static/boot.js',
  './static/ui.js',
  './static/messages.js',
  './static/sessions.js',
  './static/panels.js',
  './static/commands.js',
  './static/icons.js',
  './static/i18n.js',
  './static/workspace.js',
  './static/terminal.js',
  './static/onboarding.js',
  './static/favicon.svg',
  './static/favicon-32.png',
  './manifest.json',
];

const SHELL_PATHS = new Set(
  SHELL_ASSETS.map((asset) => {
    if (asset === './') return '/';
    return asset.startsWith('./') ? asset.slice(1) : asset;
  })
);

// Install: pre-cache the app shell
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      return cache.addAll(SHELL_ASSETS).catch((err) => {
        // Non-fatal: if any asset fails, still activate
        console.warn('[sw] Shell pre-cache partial failure:', err);
      });
    })
  );
  self.skipWaiting();
});

// Activate: clean up old caches
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k))
      )
    )
  );
  self.clients.claim();
});

// Fetch strategy:
// - API calls (/api/*, /stream) → always network (never cache)
// - Shell assets → cache-first with network fallback
// - Everything else → network-first, fall back to offline page
self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // Never intercept cross-origin requests
  if (url.origin !== self.location.origin) return;

  // Never intercept the service worker script itself. Returning a cached sw.js
  // prevents the browser from seeing a new cache version after local patches.
  if (url.pathname.endsWith('/sw.js')) return;

  // API and streaming endpoints — always go to network.
  // The WebUI may be mounted under a subpath such as /hermes/, so API
  // requests can look like /hermes/api/sessions rather than /api/sessions.
  if (
    url.pathname.startsWith('/api/') ||
    url.pathname.includes('/api/') ||
    url.pathname.includes('/stream') ||
    url.pathname.startsWith('/health') ||
    url.pathname.includes('/health')
  ) {
    return; // let browser handle normally
  }

  const isShellAsset = event.request.method === 'GET' && SHELL_PATHS.has(url.pathname);

  // Navigation requests should prefer the network so auth/login/profile UI
  // updates do not get stuck behind an older cached document shell.
  if (event.request.mode === 'navigate') {
    event.respondWith(
      fetch(event.request).catch(() =>
        caches.match('./').then((cached) => cached || new Response(
          '<html><body style="font-family:sans-serif;padding:2rem;background:#1a1a1a;color:#ccc">' +
          '<h2>You are offline</h2>' +
          '<p>Hermes requires a server connection. Please check your network and try again.</p>' +
          '</body></html>',
          { headers: { 'Content-Type': 'text/html' } }
        ))
      )
    );
    return;
  }

  // Explicit shell assets: cache-first.
  if (isShellAsset) {
    event.respondWith(
      caches.match(event.request).then((cached) => {
        if (cached) return cached;
        return fetch(event.request).then((response) => {
          if (response.status === 200) {
            const clone = response.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone));
          }
          return response;
        });
      })
    );
    return;
  }

  // Everything else: network only.
  event.respondWith(fetch(event.request));
});
