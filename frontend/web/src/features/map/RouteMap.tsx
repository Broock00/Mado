/**
 * The route between a plan's stops, and the explorer's live position on it.
 *
 * A separate component from `ExperienceMap` rather than a flag on it. That one
 * draws unordered pins and fits them; this draws an ordered walk with numbered
 * stops, a continuous line, and - while navigating - a moving dot.
 *
 * Three things it is careful to be honest about:
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
 */

import { useEffect, useRef, useState } from 'react'
import {
  Map as MapLibreMap,
  Marker,
  LngLatBounds,
  type GeoJSONSourceSpecification,
  type StyleSpecification,
} from 'maplibre-gl'
import 'maplibre-gl/dist/maplibre-gl.css'

import type { PlanRoute } from '@/lib/types'
import { type Position, routePath, splitPathAt } from '@/lib/geo'
import type { LiveFix } from './useLiveLocation'

const RASTER_STYLE: StyleSpecification = {
  version: 8,
  sources: {
    osm: {
      type: 'raster',
      tiles: ['https://tile.openstreetmap.org/{z}/{x}/{y}.png'],
      tileSize: 256,
      maxzoom: 19,
      attribution: '© <a href="https://openstreetmap.org/copyright">OpenStreetMap</a>',
    },
  },
  layers: [{ id: 'osm', type: 'raster', source: 'osm' }],
}

const FALLBACK_CENTRE: [number, number] = [38.7525, 9.0192]

const ROUTE_SOURCE = 'route'
const DONE_SOURCE = 'route-done'
const LIVE_SOURCE = 'route-live'
const ACCURACY_SOURCE = 'accuracy'

/** The planned route, before navigation starts. */
const PLANNED_COLOUR = '#15803d'
/** The part already covered, muted so it reads as behind you. */
const DONE_COLOUR = '#a8a29e'
/** The live line from where you are to where you are going. */
const LIVE_COLOUR = '#2563eb'

export interface RouteMapProps {
  route: PlanRoute | null | undefined
  /** Stop titles in plan order. Positions come from `route.points`. */
  titles: string[]
  /** The live fix while navigating. Null when not. */
  fix?: LiveFix | null
  /** Metres travelled along the route, for shading the part already covered. */
  alongMetres?: number | null
  /** Keep the camera on the explorer instead of the whole route. */
  follow?: boolean
  className?: string
}

function accuracyCircle(fix: LiveFix): Position[] {
  const points: Position[] = []
  const latRadius = fix.accuracyMetres / 111_320
  const lonRadius = latRadius / Math.cos((fix.latitude * Math.PI) / 180)
  for (let i = 0; i <= 48; i++) {
    const angle = (i / 48) * Math.PI * 2
    points.push([
      fix.longitude + lonRadius * Math.cos(angle),
      fix.latitude + latRadius * Math.sin(angle),
    ])
  }
  return points
}

export function RouteMap({
  route,
  titles,
  fix,
  alongMetres,
  follow,
  className,
}: RouteMapProps) {
  const container = useRef<HTMLDivElement>(null)
  const map = useRef<MapLibreMap | null>(null)
  const markers = useRef<Marker[]>([])
  const liveMarker = useRef<Marker | null>(null)
  // Whether the style has finished loading. Tracked rather than asked, because
  // `once('load', ...)` registered after load has already fired never runs -
  // and that is exactly the order here: the map mounts while the route is still
  // being fetched, so the first draw has nothing to draw.
  const styleReady = useRef(false)
  const [ready, setReady] = useState(false)
  // Set once the camera has framed the route, so later fixes do not keep
  // yanking the view back while somebody is panning around.
  const framed = useRef(false)

  useEffect(() => {
    if (!container.current || map.current) return

    map.current = new MapLibreMap({
      container: container.current,
      style: RASTER_STYLE,
      center: FALLBACK_CENTRE,
      zoom: 12,
      pitchWithRotate: false,
      dragRotate: false,
      attributionControl: { compact: true },
    })

    map.current.once('load', () => {
      styleReady.current = true
      setReady(true)
    })

    // The map is created the moment the route arrives, which can be before the
    // browser has laid the card out - MapLibre then measures a zero-width
    // container, never requests a tile, and never fires `load`. It does not
    // recover on its own: observed as a map with its line layers correctly
    // added, no tiles fetched, and a single `resize()` fixing everything.
    const observer = new ResizeObserver(() => map.current?.resize())
    observer.observe(container.current)

    return () => {
      observer.disconnect()
      map.current?.remove()
      map.current = null
      styleReady.current = false
      framed.current = false
    }
  }, [])

  // --- the route itself -----------------------------------------------------

  useEffect(() => {
    const instance = map.current
    if (!instance || !styleReady.current) return

    const path = routePath(route)
    // Any unroutable leg dashes the whole line: a mixed line would need one
    // source per leg, and the per-leg list beneath the map already says exactly
    // which hop was estimated.
    const anyEstimated = (route?.legs ?? []).some((leg) => leg.isEstimated)

    setLine(instance, ROUTE_SOURCE, path, {
      colour: PLANNED_COLOUR,
      width: 5,
      dashed: anyEstimated,
    })

    for (const marker of markers.current) marker.remove()
    markers.current = []

    for (const point of route?.points ?? []) {
      const element = document.createElement('div')
      element.className =
        'grid size-7 place-items-center rounded-full bg-brand-700 text-xs font-semibold text-white shadow-lifted ring-2 ring-white'
      // Numbered by position in the plan, so a marker matches the timeline even
      // when an earlier stop had no coordinates and was skipped.
      element.textContent = String(point.index + 1)
      element.title = titles[point.index] ?? ''

      markers.current.push(
        new Marker({ element }).setLngLat([point.longitude, point.latitude]).addTo(instance),
      )
    }

    if (!framed.current && path.length > 1) {
      const bounds = new LngLatBounds()
      for (const p of path) bounds.extend(p)
      for (const point of route?.points ?? []) bounds.extend([point.longitude, point.latitude])
      if (!bounds.isEmpty()) {
        instance.fitBounds(bounds, { padding: 48, maxZoom: 15, duration: 0 })
        framed.current = true
      }
    }
  }, [route, titles, ready])

  // --- the part already covered --------------------------------------------

  useEffect(() => {
    const instance = map.current
    if (!instance || !styleReady.current) return

    const path = routePath(route)

    if (path.length < 2 || alongMetres == null || alongMetres <= 0) {
      setLine(instance, DONE_SOURCE, [], { colour: DONE_COLOUR, width: 5 })
      setLine(instance, LIVE_SOURCE, [], { colour: LIVE_COLOUR, width: 6 })
      return
    }

    const [behind, ahead] = splitPathAt(path, alongMetres)

    // Behind you, muted. Drawn over the planned route rather than by shortening
    // it, so the whole journey stays visible.
    setLine(instance, DONE_SOURCE, behind, { colour: DONE_COLOUR, width: 5 })

    // Ahead of you, blue, and starting at your actual position rather than at
    // the point on the line nearest to it. Those are different places whenever
    // the fix is off the path, and a line that begins a few metres away reads
    // as somebody else's route - the whole point is that it connects *you* to
    // where you are going.
    const live = fix ? [[fix.longitude, fix.latitude] as Position, ...ahead] : ahead
    setLine(instance, LIVE_SOURCE, live, { colour: LIVE_COLOUR, width: 6 })
  }, [route, alongMetres, fix, ready])

  // --- the explorer --------------------------------------------------------

  useEffect(() => {
    const instance = map.current
    if (!instance || !styleReady.current) return

    if (!fix) {
      liveMarker.current?.remove()
      liveMarker.current = null
      setLine(instance, ACCURACY_SOURCE, [], { colour: LIVE_COLOUR, width: 1 })
      return
    }

    setLine(instance, ACCURACY_SOURCE, accuracyCircle(fix), {
      colour: LIVE_COLOUR,
      width: 1,
      opacity: 0.45,
    })

    if (!liveMarker.current) {
      const element = document.createElement('div')
      element.className = 'size-4 rounded-full border-2 border-white bg-blue-600 shadow-lifted'
      element.setAttribute('aria-label', 'Your position')
      liveMarker.current = new Marker({ element })
        .setLngLat([fix.longitude, fix.latitude])
        .addTo(instance)
    } else {
      // Drawn at the real fix, never snapped to the line. Progress is measured
      // against the route; the dot tells the truth about where the phone is.
      liveMarker.current.setLngLat([fix.longitude, fix.latitude])
    }

    if (follow) {
      instance.easeTo({
        center: [fix.longitude, fix.latitude],
        zoom: Math.max(instance.getZoom(), 16),
        duration: 600,
      })
    }
  }, [fix, follow, ready])

  // Two elements, deliberately.
  //
  // MapLibre adds `maplibregl-map` to its container's classList, and React owns
  // the `className` attribute of anything it renders. Put both on one element
  // and the next time React writes className - here, when the map grows on
  // starting navigation - it rewrites the whole attribute and silently deletes
  // MapLibre's class, taking the CSS that positions the canvas with it. The map
  // turns white while the instance is still alive and the data still correct,
  // which is a very hard failure to read.
  //
  // So the outer element carries the layout that changes, and the inner one is
  // handed to MapLibre and never touched by React again.
  return (
    <div className={className ?? 'h-72 w-full'}>
      <div ref={container} className="size-full" />
    </div>
  )
}

/** The inline data a geojson source accepts. Taken from MapLibre's own type
 *  rather than the global `GeoJSON` namespace, which is not declared here. */
type SourceData = Exclude<GeoJSONSourceSpecification['data'], string>

/** Geometry signatures per source, so identical updates can be skipped. */
const lastData = new Map<string, string>()

function signatureOf(path: Position[]): string {
  const first = path[0]
  const last = path[path.length - 1]
  return `${path.length}:${first?.[0]},${first?.[1]},${last?.[0]},${last?.[1]}`
}

/**
 * Add or replace a line layer. Idempotent, so effects can re-run freely.
 *
 * An empty path clears the line rather than removing the layer, and it clears
 * it with an empty FeatureCollection - never a LineString with no coordinates.
 * That is invalid GeoJSON: the spec requires at least two positions, and
 * feeding it to a source leaves the source in a broken state rather than an
 * empty one. Clearing happens on ordinary transitions - stopping navigation
 * empties the accuracy ring - so this is a normal path, not an edge case.
 */
function setLine(
  instance: MapLibreMap,
  id: string,
  path: Position[],
  options: { colour: string; width: number; dashed?: boolean; opacity?: number },
) {
  const data: SourceData =
    path.length >= 2
      ? {
          type: 'Feature',
          properties: {},
          geometry: { type: 'LineString', coordinates: path },
        }
      : { type: 'FeatureCollection', features: [] }

  const signature = path.length >= 2 ? signatureOf(path) : 'empty'

  const existing = instance.getSource(id)
  if (existing && 'setData' in existing) {
    // Skipped when the geometry has not actually changed. Re-setting identical
    // data still invalidates the source, and doing that on every GPS tick keeps
    // the style perpetually mid-update.
    if (lastData.get(id) === signature) return
    lastData.set(id, signature)

    // Updated in place rather than removed and re-added: the remove/add cycle
    // makes the line flicker.
    ;(existing as { setData: (value: SourceData) => void }).setData(data)
    return
  }

  // Nothing to draw and nothing to clear.
  if (path.length < 2) return

  lastData.set(id, signature)
  instance.addSource(id, { type: 'geojson', data })
  instance.addLayer({
    id,
    type: 'line',
    source: id,
    layout: { 'line-cap': 'round', 'line-join': 'round' },
    paint: {
      'line-color': options.colour,
      'line-width': options.width,
      'line-opacity': options.opacity ?? 0.9,
      ...(options.dashed ? { 'line-dasharray': [2, 2] } : {}),
    },
  })
}
