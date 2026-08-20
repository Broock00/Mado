/**
 * Map rendering.
 *
 * Spec 82.01 s10: maps are infrastructure. The vendor draws tiles; Mado supplies
 * the pins and decides their order. That boundary is why this component takes an
 * array of already-ranked experiences and never queries a map provider for
 * places - the catalogue is ours, and a vendor's idea of what is nearby is not
 * the same as ours.
 *
 * The tile source is configurable, so switching vendors is a setting rather than
 * a rewrite, and the default needs no API key - development and self-hosting work
 * without a billing account.
 *
 * The library is loaded lazily by the pages that use it: MapLibre is around
 * 200KB gzipped, and most sessions never open a map.
 */

import { useEffect, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  LngLatBounds,
  Map as MapLibreMap,
  Marker,
  NavigationControl,
  Popup,
  type StyleSpecification,
} from 'maplibre-gl'
import 'maplibre-gl/dist/maplibre-gl.css'

import { FALLBACK_CENTRE as CENTRE, type ExperienceMapProps } from './types'

/**
 * Tile source.
 *
 * Raster by default. Vector tiles are the better technology - sharper at every
 * zoom, restylable, and smaller over the wire - but they depend on a worker to
 * decode them plus a sprite and glyph pipeline, which is several more things
 * that can fail between a request and a visible map. Raster tiles are one HTTP
 * request per square: they either arrive or they do not. For a map whose whole
 * job is answering "where is this", the simpler pipeline is the right default.
 *
 * `VITE_MAP_STYLE_URL` overrides this with any MapLibre style URL, so a
 * deployment with a vector tile vendor points at it and gets the better
 * rendering without a code change - which is what spec 82.01 s10 means by
 * treating maps as infrastructure.
 *
 * OpenStreetMap's own tile servers are a courtesy, not a CDN, and their usage
 * policy rules out heavy commercial traffic. A production deployment must set
 * VITE_MAP_STYLE_URL to its own vendor.
 */
const STYLE_OVERRIDE = import.meta.env.VITE_MAP_STYLE_URL as string | undefined

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

/** MapLibre's order: longitude first. */
const FALLBACK_CENTRE: [number, number] = [CENTRE.longitude, CENTRE.latitude]

export function MapLibreExperienceMap({ experiences, origin, className, zoom }: ExperienceMapProps) {
  const container = useRef<HTMLDivElement>(null)
  const map = useRef<MapLibreMap | null>(null)
  const markers = useRef<Marker[]>([])
  const navigate = useNavigate()

  // Create once. Re-creating on every render would refetch every tile.
  useEffect(() => {
    if (!container.current || map.current) return

    const instance = new MapLibreMap({
      container: container.current,
      style: STYLE_OVERRIDE ?? RASTER_STYLE,
      center: FALLBACK_CENTRE,
      zoom: zoom ?? 12,
      // Tilt and rotation add nothing to "where is this" and make the map
      // easy to leave in a confusing state on a touch screen.
      pitchWithRotate: false,
      dragRotate: false,
      attributionControl: { compact: true },
    })

    instance.addControl(new NavigationControl({ showCompass: false }), 'top-right')
    map.current = instance

    // MapLibre measures its container once at construction. This component mounts
    // inside a branch that has just been revealed - and behind a Suspense
    // boundary - so that measurement can happen while the element is still zero
    // by zero, leaving a canvas that never paints. Observing the container and
    // calling resize is the only reliable fix; a one-off timeout races the layout.
    const observer = new ResizeObserver(() => instance.resize())
    observer.observe(container.current)

    return () => {
      observer.disconnect()
      map.current?.remove()
      map.current = null
    }
  }, [zoom])

  // Pins are redrawn whenever the ranked set changes.
  useEffect(() => {
    const instance = map.current
    if (!instance) return

    markers.current.forEach((marker) => marker.remove())
    markers.current = []

    const located = experiences.filter((item) => item.venue?.latitude && item.venue?.longitude)
    const bounds = new LngLatBounds()

    located.forEach((item, index) => {
      const venue = item.venue!
      const element = document.createElement('button')
      element.type = 'button'
      element.setAttribute('aria-label', item.title)
      element.className =
        'grid size-7 place-items-center rounded-full border-2 border-white bg-brand-600 ' +
        'text-xs font-semibold text-white shadow-lifted transition-transform hover:scale-110'
      // Numbered to match the order of the list beside it, so the map and the
      // results are legibly the same thing rather than two parallel views.
      element.textContent = String(index + 1)
      element.addEventListener('click', () => navigate(`/experiences/${item.id}`))

      const marker = new Marker({ element })
        .setLngLat([venue.longitude, venue.latitude])
        .setPopup(
          new Popup({ offset: 16, closeButton: false }).setText(item.title),
        )
        .addTo(instance)

      markers.current.push(marker)
      bounds.extend([venue.longitude, venue.latitude])
    })

    if (origin) {
      const element = document.createElement('div')
      element.setAttribute('aria-label', 'Your location')
      element.className = 'size-4 rounded-full border-2 border-white bg-sand-900 shadow-lifted'
      markers.current.push(
        new Marker({ element })
          .setLngLat([origin.longitude, origin.latitude])
          .addTo(instance),
      )
      bounds.extend([origin.longitude, origin.latitude])
    }

    // Fit to what is actually shown. A fixed centre would put the pins off
    // screen the moment an explorer searches for something across town.
    if (!bounds.isEmpty() && zoom === undefined) {
      instance.fitBounds(bounds, { padding: 56, maxZoom: 15, duration: 0 })
    } else if (located.length === 1) {
      const venue = located[0].venue!
      instance.setCenter([venue.longitude, venue.latitude])
    }
  }, [experiences, origin, navigate, zoom])

  return (
    <div
      ref={container}
      className={className ?? 'h-[28rem] w-full overflow-hidden rounded-xl'}
      // The pins are buttons and reachable by keyboard; the canvas itself is
      // decorative, so it is not announced as an interactive region.
      role="region"
      aria-label="Map of results"
    />
  )
}

export default MapLibreExperienceMap
