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
const ACCURACY_SOURCE = 'accuracy'

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
      colour: '#15803d',
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
      setLine(instance, DONE_SOURCE, [], { colour: '#a8a29e', width: 5 })
      return
    }

    const [behind] = splitPathAt(path, alongMetres)
    // Drawn over the route in a muted colour rather than by shortening the route
    // line, so the whole journey stays visible - somebody wants to see where
    // they are going, not only what is left.
    setLine(instance, DONE_SOURCE, behind, { colour: '#a8a29e', width: 5 })
  }, [route, alongMetres, ready])

  // --- the explorer --------------------------------------------------------

  useEffect(() => {
    const instance = map.current
    if (!instance || !styleReady.current) return

    if (!fix) {
      liveMarker.current?.remove()
      liveMarker.current = null
      setLine(instance, ACCURACY_SOURCE, [], { colour: '#2563eb', width: 1 })
      return
    }

    setLine(instance, ACCURACY_SOURCE, accuracyCircle(fix), {
      colour: '#2563eb',
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

  return <div ref={container} className={className ?? 'h-72 w-full rounded-card'} />
}

/** Add or replace a line layer. Idempotent, so effects can re-run freely. */
function setLine(
  instance: MapLibreMap,
  id: string,
  path: Position[],
  options: { colour: string; width: number; dashed?: boolean; opacity?: number },
) {
  const data = {
    type: 'Feature' as const,
    properties: {},
    geometry: { type: 'LineString' as const, coordinates: path },
  }

  const existing = instance.getSource(id)
  if (existing && 'setData' in existing) {
    // Updated in place rather than removed and re-added: on every GPS tick the
    // remove/add cycle makes the line flicker.
    ;(existing as { setData: (value: typeof data) => void }).setData(data)
    return
  }

  if (path.length < 2) return

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
