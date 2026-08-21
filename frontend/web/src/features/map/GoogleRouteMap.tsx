/**
 * The route between a plan's stops, and the explorer's live position on it,
 * drawn by Google.
 *
 * The MapLibre implementation beside this one is the reference; everything it is
 * careful to be honest about, this is too:
 *
 * - **Estimated legs are dashed.** When the router could not serve a hop, the
 *   line is a straight one between two points. Drawing that solid claims a road
 *   exists along it, which is a statement nobody made.
 * - **The dot sits where the GPS says, not on the line.** Progress is measured
 *   by projecting onto the route, but the marker is drawn at the real fix. A dot
 *   glued to the line would tell somebody standing in the wrong street that they
 *   are in the right one.
 * - **Accuracy is drawn.** The circle is the browser's own uncertainty. A bare
 *   dot implies a precision a phone in a city rarely has.
 *
 * The one structural difference is how the lines are held. MapLibre wants a
 * GeoJSON source per line, updated in place; Google wants a Polyline object
 * whose path is reassigned. Same three lines either way - planned, covered, and
 * the live one from where you are to where you are going - so they are kept in a
 * map keyed by the same names the other implementation uses.
 */

import { useEffect, useRef, useState } from 'react'

import { type Position, projectOntoPath, routePath, splitPathAt } from '@/lib/geo'
import { googleMapId, loadGoogleMaps } from './google'
import { FALLBACK_CENTRE, type RouteMapProps } from './types'

const ROUTE_LINE = 'route'
const DONE_LINE = 'route-done'
const LIVE_LINE = 'route-live'

/** The planned route, before navigation starts. */
const PLANNED_COLOUR = '#15803d'
/** The part already covered, muted so it reads as behind you. */
const DONE_COLOUR = '#a8a29e'
/** The live line from where you are to where you are going. */
const LIVE_COLOUR = '#2563eb'

/**
 * How far off the route somebody can be and still be "on" it for the camera.
 *
 * Generous compared with the off-route threshold used for progress (60 m),
 * because this decides a camera and that decides a claim. Following somebody
 * who has wandered one street over is right; following somebody who is still
 * at home is how the route ends up off-screen.
 */
const NEAR_ROUTE_METRES = 250

/**
 * The tightest zoom a fix of this accuracy justifies.
 *
 * Showing a dot at a zoom its accuracy cannot support is the map equivalent of
 * quoting six decimal places from a guess.
 */
function zoomForAccuracy(accuracyMetres: number): number {
  if (!Number.isFinite(accuracyMetres) || accuracyMetres <= 20) return 18
  if (accuracyMetres <= 50) return 17
  if (accuracyMetres <= 120) return 16
  if (accuracyMetres <= 300) return 15
  return 14
}

/** GeoJSON order in, Google's order out. */
function toLatLng(path: Position[]): google.maps.LatLngLiteral[] {
  return path.map(([lng, lat]) => ({ lat, lng }))
}

export function GoogleRouteMap({
  route,
  titles,
  fix,
  alongMetres,
  follow,
  className,
}: RouteMapProps) {
  const container = useRef<HTMLDivElement>(null)
  const map = useRef<google.maps.Map | null>(null)
  const markers = useRef<google.maps.marker.AdvancedMarkerElement[]>([])
  const liveMarker = useRef<google.maps.marker.AdvancedMarkerElement | null>(null)
  const accuracy = useRef<google.maps.Circle | null>(null)
  const lines = useRef(new Map<string, google.maps.Polyline>())
  const [ready, setReady] = useState(false)
  // Set once the camera has framed the route, so later fixes do not keep
  // yanking the view back while somebody is panning around.
  const framed = useRef(false)

  useEffect(() => {
    let live = true
    // Captured now rather than read in the cleanup: the Map itself is created
    // once and never reassigned, so this is the same object either way, and
    // holding it here is what tells the linter so.
    const drawn = lines.current
    loadGoogleMaps()
      .then(() => {
        if (!live || !container.current || map.current) return
        map.current = new google.maps.Map(container.current, {
          center: { lat: FALLBACK_CENTRE.latitude, lng: FALLBACK_CENTRE.longitude },
          zoom: 12,
          mapId: googleMapId,
          tilt: 0,
          rotateControl: false,
          mapTypeControl: false,
          streetViewControl: false,
          fullscreenControl: false,
          clickableIcons: false,
        })
        setReady(true)
      })
      .catch(() => {
        // The plan itself is listed beneath the map, so a map that will not load
        // costs the picture and not the answer.
      })

    return () => {
      live = false
      for (const marker of markers.current) marker.map = null
      markers.current = []
      for (const line of drawn.values()) line.setMap(null)
      drawn.clear()
      liveMarker.current = null
      accuracy.current?.setMap(null)
      accuracy.current = null
      map.current = null
      framed.current = false
    }
  }, [])

  /**
   * Add or replace a line. Idempotent, so effects can re-run freely.
   *
   * An empty path clears the line rather than removing it: clearing happens on
   * ordinary transitions - stopping navigation empties the live line - and a
   * removed polyline would have to be rebuilt with all its styling the next
   * time somebody set off.
   */
  function setLine(
    id: string,
    path: Position[],
    options: { colour: string; width: number; dashed?: boolean },
  ) {
    const instance = map.current
    if (!instance) return

    const existing = lines.current.get(id)
    if (existing) {
      existing.setPath(toLatLng(path))
      return
    }
    if (path.length < 2) return

    lines.current.set(
      id,
      new google.maps.Polyline({
        map: instance,
        path: toLatLng(path),
        strokeColor: options.colour,
        // A dashed line in Google's API is a transparent stroke plus a repeating
        // symbol; there is no dash array. Weight goes on the symbol in that case
        // or the invisible stroke would still be drawn at full width.
        strokeOpacity: options.dashed ? 0 : 0.9,
        strokeWeight: options.width,
        ...(options.dashed
          ? {
              icons: [
                {
                  icon: {
                    path: 'M 0,-1 0,1',
                    strokeOpacity: 0.9,
                    strokeColor: options.colour,
                    strokeWeight: options.width,
                    scale: 2,
                  },
                  offset: '0',
                  repeat: '12px',
                },
              ],
            }
          : {}),
      }),
    )
  }

  // --- the route itself -----------------------------------------------------

  useEffect(() => {
    if (!map.current || !ready) return

    const path = routePath(route)
    // Any unroutable leg dashes the whole line: a mixed line would need one
    // polyline per leg, and the per-leg list beneath the map already says
    // exactly which hop was estimated.
    const anyEstimated = (route?.legs ?? []).some((leg) => leg.isEstimated)

    setLine(ROUTE_LINE, path, {
      colour: PLANNED_COLOUR,
      width: 5,
      dashed: anyEstimated,
    })

    for (const marker of markers.current) marker.map = null
    markers.current = []

    for (const point of route?.points ?? []) {
      const element = document.createElement('div')
      element.className =
        'grid size-7 place-items-center rounded-full bg-brand-700 text-xs font-semibold text-white shadow-lifted ring-2 ring-sand-100'
      // Numbered by position in the plan, so a marker matches the timeline even
      // when an earlier stop had no coordinates and was skipped.
      element.textContent = String(point.index + 1)
      element.title = titles[point.index] ?? ''

      markers.current.push(
        new google.maps.marker.AdvancedMarkerElement({
          map: map.current,
          position: { lat: point.latitude, lng: point.longitude },
          content: element,
          title: titles[point.index] ?? '',
        }),
      )
    }

    if (!framed.current && path.length > 1) {
      const bounds = new google.maps.LatLngBounds()
      for (const [lng, lat] of path) bounds.extend({ lat, lng })
      for (const point of route?.points ?? []) {
        bounds.extend({ lat: point.latitude, lng: point.longitude })
      }
      if (!bounds.isEmpty()) {
        map.current.fitBounds(bounds, 48)
        framed.current = true
      }
    }
    // `setLine` closes over refs only, so it is stable in every way that matters
    // and listing it would mean rebuilding it on each render to satisfy a rule
    // it does not break.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [route, titles, ready])

  // --- the part already covered --------------------------------------------

  useEffect(() => {
    if (!map.current || !ready) return

    const path = routePath(route)

    if (path.length < 2 || alongMetres == null || alongMetres <= 0) {
      setLine(DONE_LINE, [], { colour: DONE_COLOUR, width: 5 })
      setLine(LIVE_LINE, [], { colour: LIVE_COLOUR, width: 6 })
      return
    }

    const [behind, ahead] = splitPathAt(path, alongMetres)

    // Behind you, muted. Drawn over the planned route rather than by shortening
    // it, so the whole journey stays visible.
    setLine(DONE_LINE, behind, { colour: DONE_COLOUR, width: 5 })

    // Ahead of you, blue, and starting at your actual position rather than at
    // the point on the line nearest to it. Those are different places whenever
    // the fix is off the path, and a line that begins a few metres away reads
    // as somebody else's route - the whole point is that it connects *you* to
    // where you are going.
    const live = fix ? [[fix.longitude, fix.latitude] as Position, ...ahead] : ahead
    setLine(LIVE_LINE, live, { colour: LIVE_COLOUR, width: 6 })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [route, alongMetres, fix, ready])

  // --- the explorer --------------------------------------------------------

  useEffect(() => {
    const instance = map.current
    if (!instance || !ready) return

    if (!fix) {
      if (liveMarker.current) liveMarker.current.map = null
      liveMarker.current = null
      accuracy.current?.setMap(null)
      accuracy.current = null
      return
    }

    const here = { lat: fix.latitude, lng: fix.longitude }

    // A real circle rather than a 48-point polygon: Google has one, and it
    // stays a circle at every zoom instead of becoming a visible polygon when
    // the accuracy is poor enough to fill the screen.
    if (!accuracy.current) {
      accuracy.current = new google.maps.Circle({
        map: instance,
        center: here,
        radius: fix.accuracyMetres,
        strokeColor: LIVE_COLOUR,
        strokeOpacity: 0.45,
        strokeWeight: 1,
        fillColor: LIVE_COLOUR,
        fillOpacity: 0.12,
        clickable: false,
      })
    } else {
      accuracy.current.setCenter(here)
      accuracy.current.setRadius(fix.accuracyMetres)
    }

    if (!liveMarker.current) {
      const element = document.createElement('div')
      element.className = 'size-4 rounded-full border-2 border-white bg-blue-600 shadow-lifted'
      element.setAttribute('aria-label', 'Your position')
      liveMarker.current = new google.maps.marker.AdvancedMarkerElement({
        map: instance,
        position: here,
        content: element,
        title: 'Your position',
      })
    } else {
      // Drawn at the real fix, never snapped to the line. Progress is measured
      // against the route; the dot tells the truth about where the phone is.
      liveMarker.current.position = here
    }

    if (!follow) return

    // Centring on the fix alone is only right when the explorer is *on* the
    // route. Somebody who opens their plan at home, or across town, or before
    // setting off, gets the camera thrown to wherever their phone says they
    // are - and the route, every stop and the blue line all leave the screen at
    // once. What is left is an empty street grid with a dot on it, which reads
    // as the map having broken rather than as the map having followed you.
    //
    // So: follow them when they are near the route, and frame both otherwise.
    const path = routePath(route)
    const position: Position = [fix.longitude, fix.latitude]
    const projection = path.length > 1 ? projectOntoPath(path, position) : null
    const nearby = projection != null && projection.offRouteMetres <= NEAR_ROUTE_METRES

    if (nearby) {
      instance.panTo(here)
      // Never tighter than the fix can support. The interface says in words
      // that a 141 m fix is "not enough to place you on a street"; zooming to
      // street level anyway draws a confident picture of a position nobody
      // knows that precisely.
      const current = instance.getZoom() ?? 16
      instance.setZoom(Math.min(Math.max(current, 16), zoomForAccuracy(fix.accuracyMetres)))
      return
    }

    // Far away: show them and the start of the journey together, so the answer
    // to "where am I in relation to this plan" is on the screen.
    const bounds = new google.maps.LatLngBounds()
    bounds.extend(here)
    for (const [lng, lat] of path) bounds.extend({ lat, lng })
    for (const point of route?.points ?? []) {
      bounds.extend({ lat: point.latitude, lng: point.longitude })
    }
    if (!bounds.isEmpty()) instance.fitBounds(bounds, 56)
  }, [fix, follow, route, ready])

  // Two elements, matching the MapLibre implementation. The outer one carries
  // the layout that changes - the map grows when navigation starts - and the
  // inner one is handed to the vendor and never touched by React again.
  return (
    <div className={className ?? 'h-72 w-full'}>
      <div ref={container} className="size-full" />
    </div>
  )
}

export default GoogleRouteMap
