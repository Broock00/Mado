/**
 * Loading Google Maps, once, on demand.
 *
 * Spec 82.01 s10 treats maps as infrastructure: the vendor draws tiles, and
 * Mado supplies the pins and decides their order. Which vendor draws them is
 * therefore a setting, and this module is the switch. With
 * `VITE_GOOGLE_MAPS_API_KEY` set the maps render through Google; without it they
 * fall back to MapLibre over OpenStreetMap tiles, so development, self-hosting
 * and a deployment with no billing account all still work.
 *
 * **The script tag is deliberate.** The Maps JavaScript API cannot be bundled -
 * it is served per-key, per-version, and it registers itself on `window`. So it
 * is injected at first use and never at page load: most sessions never open a
 * map, and the library plus its libraries is several hundred kilobytes and a
 * billable map load.
 *
 * **The key is public and that is fine.** Anything the browser sends, a reader
 * can read; there is no arrangement in which a browser calls Google without the
 * key being visible. What stops it being abused is a restriction on the key
 * itself - HTTP referrer, and only the Maps JavaScript API enabled - which is
 * why this must be a *different* key from the server's, and why the server's
 * must never be given to the browser. See `.env.example` in this directory.
 */

const KEY = import.meta.env.VITE_GOOGLE_MAPS_API_KEY as string | undefined

/**
 * Whether maps should render through Google.
 *
 * Read at module scope, so it is constant for the life of the page: a component
 * that switched vendors mid-session would have to tear down and rebuild a live
 * map, and there is no reason for that to ever happen.
 */
export const usingGoogleMaps = Boolean(KEY)

/**
 * Map style. Google's own by default.
 *
 * `VITE_GOOGLE_MAPS_ID` points at a cloud-styled map, which is how Google wants
 * styling done now - the old inline `styles` array is ignored for vector maps
 * and is being retired. A map id is also what advanced markers require, so
 * without one this falls back to Google's classic markers.
 */
const MAP_ID = import.meta.env.VITE_GOOGLE_MAPS_MAP_ID as string | undefined
export const googleMapId = MAP_ID

/**
 * One in-flight load, shared.
 *
 * Two maps on a page, or a map remounted by a route change, must not inject the
 * script twice - Google warns about it in the console and the second load
 * clobbers the first library object. Memoising the promise rather than a boolean
 * also means a second caller arriving mid-load waits for the same load instead
 * of starting another.
 */
let loading: Promise<void> | null = null

export function loadGoogleMaps(): Promise<void> {
  if (!KEY) return Promise.reject(new Error('No Google Maps key configured.'))
  if (window.google?.maps) return Promise.resolve()
  if (loading) return loading

  loading = new Promise<void>((resolve, reject) => {
    const script = document.createElement('script')
    // `marker` for advanced markers, `geometry` for the polyline maths the route
    // map needs. Asked for up front because a library requested later is another
    // network round trip in the middle of an interaction.
    const params = new URLSearchParams({
      key: KEY,
      v: 'weekly',
      libraries: 'marker,geometry',
      loading: 'async',
    })
    script.src = `https://maps.googleapis.com/maps/api/js?${params}`
    script.async = true
    script.onload = () => resolve()
    script.onerror = () => {
      // Cleared so a later attempt can retry. A rejected promise cached forever
      // would make one flaky network moment mean no maps until reload.
      loading = null
      script.remove()
      reject(new Error('Google Maps failed to load.'))
    }
    document.head.appendChild(script)
  })
  return loading
}

/**
 * A pin element, styled like the rest of the interface.
 *
 * Built as DOM rather than configured through the API because Google's own
 * marker options cannot produce it and because both map vendors accept an
 * element - so the two implementations look identical rather than merely
 * similar, which is the whole point of the vendor being swappable.
 */
export function pinElement(options: { colour?: string; size?: number } = {}): HTMLElement {
  const { colour = 'bg-brand-600', size = 8 } = options
  const pin = document.createElement('div')
  pin.className =
    `grid size-${size} place-items-center rounded-full border-2 border-white ` +
    `${colour} text-white shadow-lifted`
  pin.innerHTML =
    '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
    'stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">' +
    '<path d="M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0 1 16 0Z"/><circle cx="12" cy="10" r="3"/></svg>'
  return pin
}
