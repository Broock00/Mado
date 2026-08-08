/**
 * The route between a plan's stops, drawn on a map (spec MAP-002).
 *
 * A separate component from `ExperienceMap` rather than a flag on it. That one
 * draws unordered pins and fits them; this draws an ordered walk with numbered
 * stops and lines between them. Sharing an implementation would mean a growing
 * pile of conditionals inside every effect.
 *
 * Two things it is careful to be honest about:
 *
 * - **Estimated legs are dashed.** When the router could not serve a hop, the
 *   line is a straight one between two points. Drawing that solid claims a road
 *   exists along it, which is a statement nobody made.
 * - **Stops are numbered, not just pinned.** A plan is a sequence; a scatter of
 *   identical dots loses the one thing that makes it a plan.
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

export interface RouteMapProps {
  route: PlanRoute | null | undefined
  /** Stop titles in plan order. Positions come from `route.points`, which
   *  omits stops whose venue has no coordinates. */
  titles: string[]
  className?: string
}

export function RouteMap({ route, titles, className }: RouteMapProps) {
  const container = useRef<HTMLDivElement>(null)
  const map = useRef<MapLibreMap | null>(null)
  const markers = useRef<Marker[]>([])
  // Whether the style has finished loading. Tracked rather than asked, because
  // `once('load', ...)` registered after load has already fired never runs -
  // and that is exactly the order this component hits: the map mounts while the
  // route is still being fetched, so the first draw has nothing to draw, and
  // the second one arrives too late to register for an event already past.
  const styleReady = useRef(false)
  // Bumped when the style becomes ready, so the drawing effect re-runs then.
  const [ready, setReady] = useState(false)

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
    // recover on its own: observed here as a map with its line layers correctly
    // added, no tiles fetched, and a single `resize()` call fixing everything.
    const observer = new ResizeObserver(() => map.current?.resize())
    observer.observe(container.current)

    return () => {
      observer.disconnect()
      map.current?.remove()
      map.current = null
      styleReady.current = false
    }
  }, [])

  useEffect(() => {
    const instance = map.current
    if (!instance) return

    function draw() {
      if (!instance) return
      const bounds = new LngLatBounds()
      let hasPoints = false

      // Lines first, so numbered markers sit above them.
      for (const [index, leg] of (route?.legs ?? []).entries()) {
        if (leg.geometry.length < 2) continue
        const id = `leg-${index}`

        if (instance.getLayer(id)) instance.removeLayer(id)
        if (instance.getSource(id)) instance.removeSource(id)

        instance.addSource(id, {
          type: 'geojson',
          data: {
            type: 'Feature',
            properties: {},
            geometry: { type: 'LineString', coordinates: leg.geometry },
          },
        })
        instance.addLayer({
          id,
          type: 'line',
          source: id,
          layout: { 'line-cap': 'round', 'line-join': 'round' },
          paint: {
            'line-color': leg.isEstimated ? '#a8a29e' : '#15803d',
            'line-width': 4,
            'line-opacity': 0.85,
            // Dashed when this is a straight line rather than a road.
            ...(leg.isEstimated ? { 'line-dasharray': [2, 2] } : {}),
          },
        })

        for (const point of leg.geometry) {
          bounds.extend([point[0], point[1]])
          hasPoints = true
        }
      }

      for (const marker of markers.current) marker.remove()
      markers.current = []

      for (const point of route?.points ?? []) {
        const element = document.createElement('div')
        element.className =
          'grid size-7 place-items-center rounded-full bg-brand-700 text-xs font-semibold text-white shadow-lifted ring-2 ring-white'
        // Numbered by position in the plan, so a marker matches the timeline
        // even when an earlier stop had no coordinates and was skipped.
        element.textContent = String(point.index + 1)
        element.title = titles[point.index] ?? ''

        markers.current.push(
          new Marker({ element }).setLngLat([point.longitude, point.latitude]).addTo(instance),
        )
        bounds.extend([point.longitude, point.latitude])
        hasPoints = true
      }

      if (hasPoints && !bounds.isEmpty()) {
        instance.fitBounds(bounds, { padding: 48, maxZoom: 15, duration: 0 })
      }
    }

    // Sources cannot be added before the style has loaded. `ready` is in the
    // dependency list so this runs again the moment it is, rather than relying
    // on an event that may already have fired.
    if (styleReady.current) draw()
  }, [route, titles, ready])

  return <div ref={container} className={className ?? 'h-72 w-full rounded-card'} />
}
