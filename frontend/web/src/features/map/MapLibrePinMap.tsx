/**
 * A map with one draggable pin, drawn by MapLibre over OpenStreetMap tiles.
 *
 * The keyless half of `PinMap`. Everything about *why* a publisher places a pin
 * rather than typing an address lives in `LocationPicker`; this only knows how
 * to draw one and report where it was moved to.
 *
 * The pin follows `value` rather than being moved through a handle, so the two
 * implementations need no imperative surface between them and the picker cannot
 * end up with a pin somewhere its state does not agree with.
 */

import { useEffect, useRef } from 'react'
import {
  Map as MapLibreMap,
  Marker,
  NavigationControl,
  type StyleSpecification,
} from 'maplibre-gl'
import 'maplibre-gl/dist/maplibre-gl.css'

import { pinElement } from './google'
import { CITY_ZOOM, PLACE_ZOOM, type PinMapProps } from './types'

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

export function MapLibrePinMap({ centre, value, onPick, className }: PinMapProps) {
  const container = useRef<HTMLDivElement>(null)
  const map = useRef<MapLibreMap | null>(null)
  const marker = useRef<Marker | null>(null)
  // Held in a ref as well: the map's handlers are bound once and would
  // otherwise close over the first render's callback forever.
  const onPickRef = useRef(onPick)
  onPickRef.current = onPick

  useEffect(() => {
    if (!container.current || map.current) return

    const start: [number, number] = value
      ? [value.longitude, value.latitude]
      : [centre.longitude, centre.latitude]

    const instance = new MapLibreMap({
      container: container.current,
      style: STYLE_OVERRIDE ?? RASTER_STYLE,
      center: start,
      zoom: value ? PLACE_ZOOM : CITY_ZOOM,
      pitchWithRotate: false,
      dragRotate: false,
      attributionControl: { compact: true },
    })
    instance.addControl(new NavigationControl({ showCompass: false }), 'top-right')

    const pin = pinElement()
    pin.classList.add('-translate-y-2')
    marker.current = new Marker({ element: pin, draggable: true }).setLngLat(start).addTo(instance)

    // Dragging reads as "move this here", tapping as "put it there". Both are
    // natural and people use both, so both are supported.
    marker.current.on('dragend', () => {
      const { lat, lng } = marker.current!.getLngLat()
      onPickRef.current(lat, lng)
    })
    instance.on('click', (event) => onPickRef.current(event.lngLat.lat, event.lngLat.lng))

    map.current = instance

    // The picker mounts inside a section that has just been revealed, so its
    // container can be zero-sized at construction and the canvas never paints.
    const observer = new ResizeObserver(() => instance.resize())
    observer.observe(container.current)

    return () => {
      observer.disconnect()
      instance.remove()
      map.current = null
      marker.current = null
    }
    // Deliberately mounts once. `centre` and `value` seed the initial view; later
    // changes move the map through the effect below rather than rebuilding it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    if (!value || !map.current) return
    marker.current?.setLngLat([value.longitude, value.latitude])
    map.current.easeTo({ center: [value.longitude, value.latitude], duration: 400 })
  }, [value])

  return (
    <div
      ref={container}
      className={className ?? 'h-72 w-full overflow-hidden rounded-xl border border-sand-200'}
      role="application"
      aria-label="Map. Tap to place the pin, or drag it."
    />
  )
}

export default MapLibrePinMap
