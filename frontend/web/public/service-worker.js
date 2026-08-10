/**
 * Offline trips (spec EXP-005).
 *
 * The problem this solves is specific: somebody is standing on a street in
 * Addis with a plan they made at home and no data. They need the stops, the
 * times, the addresses and how to get between them. They do not need the
 * discovery feed.
 *
 * **Almost nothing is cached, and that is the point.** An offline-first shell
 * that caches everything is how a product shows a sold-out event as available,
 * a cancelled date as on, and last Tuesday's "tonight" rail as tonight. Stale
 * discovery data is worse than no discovery data, because the reader cannot
 * tell. So the only API responses kept are kept itineraries and their routes -
 * things the explorer deliberately saved, which describe a fixed evening, and
 * which are useless the moment they need them if they are not here.
 *
 * **Written by hand rather than generated.** `vite-plugin-pwa` and Workbox are
 * good and would be a reasonable choice; they are not used because the policy
 * above is the whole feature, and it is easier to read as forty lines of fetch
 * handler than as a configuration object whose defaults cache more than
 * intended. The risk with a generated service worker is precisely that it
 * quietly caches the thing you did not want cached.
 *
 * **The shell is cached because otherwise none of this works.** With no
 * network and no cached HTML, a browser shows its own error page and the saved
 * itinerary inside the app is unreachable. The shell is versioned so a deploy
 * replaces it rather than serving last month's JavaScript forever.
 */

// Bumped on any change to this file or to what the shell needs. Old caches are
// deleted on activate, so a stale shell cannot outlive a deploy.
const VERSION = 'mado-v1'
const SHELL_CACHE = `${VERSION}-shell`
const PLANS_CACHE = `${VERSION}-plans`

// Enough to boot the app and render a stored plan. Hashed asset URLs are not
// listed: they change every build, and they are picked up on first visit by the
// runtime handler below.
const SHELL = ['/', '/index.html', '/favicon.svg']

/** Only these API paths are ever stored. Everything else goes to the network. */
function isSavedPlan(url) {
  return (
    url.pathname.startsWith('/api/v1/itineraries/') ||
    url.pathname === '/api/v1/itineraries'
  )
}

function isAsset(url) {
  return (
    url.origin === self.location.origin &&
    (url.pathname.startsWith('/assets/') || url.pathname.endsWith('.svg'))
  )
}

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches
      .open(SHELL_CACHE)
      // `addAll` rejects the whole install if any one URL fails, which would
      // leave no service worker at all. Individually, a missing favicon costs
      // a favicon.
      .then((cache) => Promise.allSettled(SHELL.map((path) => cache.add(path))))
      .then(() => self.skipWaiting()),
  )
})

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((names) =>
        Promise.all(
          names.filter((name) => !name.startsWith(VERSION)).map((name) => caches.delete(name)),
        ),
      )
      .then(() => self.clients.claim()),
  )
})

self.addEventListener('fetch', (event) => {
  const request = event.request
  if (request.method !== 'GET') return

  const url = new URL(request.url)

  // Navigation: try the network, fall back to the cached shell. Network first
  // rather than cache first, so a deploy is picked up on the next visit online
  // rather than whenever the cache happens to be evicted.
  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request).catch(() =>
        caches.match('/index.html', { ignoreSearch: true }).then(
          (cached) => cached ?? new Response('Offline', { status: 503 }),
        ),
      ),
    )
    return
  }

  // A saved plan. Network first so the times are current whenever they can be,
  // and the last good copy kept so they are there when they cannot.
  if (isSavedPlan(url)) {
    event.respondWith(
      fetch(request)
        .then((response) => {
          if (response.ok) {
            const copy = response.clone()
            caches.open(PLANS_CACHE).then((cache) => cache.put(request, copy))
          }
          return response
        })
        .catch(() =>
          caches.match(request).then((cached) => {
            if (!cached) throw new Error('offline and not stored')
            // Marked so the interface can say the plan is from storage rather
            // than presenting an hour-old copy as current. An offline plan that
            // looks live is how somebody turns up to a cancelled event.
            const headers = new Headers(cached.headers)
            headers.set('X-Mado-From-Cache', '1')
            return cached.blob().then(
              (body) =>
                new Response(body, {
                  status: cached.status,
                  statusText: cached.statusText,
                  headers,
                }),
            )
          }),
        ),
    )
    return
  }

  // Build assets are content-hashed, so a cached copy can never be the wrong
  // one - cache first is safe and makes a second visit instant.
  if (isAsset(url)) {
    event.respondWith(
      caches.match(request).then(
        (cached) =>
          cached ??
          fetch(request).then((response) => {
            if (response.ok) {
              const copy = response.clone()
              caches.open(SHELL_CACHE).then((cache) => cache.put(request, copy))
            }
            return response
          }),
      ),
    )
    return
  }

  // Everything else - discovery, search, the concierge - is left alone. It is
  // time-sensitive, and a stale answer that looks current is worse than an
  // honest failure.
})
