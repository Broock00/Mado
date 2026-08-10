/**
 * Registering the service worker, and knowing when the network is gone
 * (spec EXP-005).
 *
 * **Only in a production build.** A service worker in front of the Vite dev
 * server intercepts module requests and serves yesterday's code, which presents
 * as an edit that does nothing - the single most confusing bug this feature can
 * cause, and it costs a developer an afternoon before they think to look here.
 *
 * **`navigator.onLine` is not trusted on its own.** It reports whether there is
 * a network interface, not whether anything is reachable: a captive portal, a
 * hotel wifi that has not been paid for, and a dead uplink all report `true`.
 * It is used only to notice the transition, and the interface says "we cannot
 * reach Mado" rather than "you are offline", because the second is a claim
 * about the reader's connection that this cannot actually make.
 */

import { useSyncExternalStore } from 'react'

export function registerServiceWorker(): void {
  if (!import.meta.env.PROD) return
  if (!('serviceWorker' in navigator)) return

  // After load, so registering never competes with the first paint for
  // bandwidth on the connection this is meant to help with.
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/service-worker.js').catch((error) => {
      // Not fatal. Everything works online without it; only the offline plan
      // is lost, and failing loudly here would break the app for a feature
      // that is a convenience.
      console.warn('[offline] service worker did not register', error)
    })
  })
}

function subscribe(callback: () => void): () => void {
  window.addEventListener('online', callback)
  window.addEventListener('offline', callback)
  return () => {
    window.removeEventListener('online', callback)
    window.removeEventListener('offline', callback)
  }
}

/**
 * Whether the browser currently believes it has a network.
 *
 * `useSyncExternalStore` rather than state plus an effect: the value is read
 * during render from the browser rather than mirrored into React, so there is
 * no window where the component and the world disagree.
 */
export function useIsOnline(): boolean {
  return useSyncExternalStore(
    subscribe,
    () => navigator.onLine,
    // Server snapshot. Nothing here is server-rendered today, and assuming
    // online is the right default for the case where it becomes so.
    () => true,
  )
}
