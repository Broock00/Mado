/**
 * Choosing where something is, on a map.
 *
 * Replaces typing an address, which was the wrong interaction in both
 * directions: a publisher standing outside their own cafe does not know the
 * postal address a geocoder wants, and a text box gives no feedback at all until
 * after they have committed to it. Worse, a geocoder returns a confident,
 * precise-looking point for a bad query - so the failure mode was a listing
 * pinned somewhere plausible and wrong, with nobody able to tell.
 *
 * A map inverts that. The publisher sees where the pin is the entire time, and
 * the coordinates come from a deliberate act rather than an interpretation of
 * one. Three ways to place it, in the order people reach for them:
 *
 * 1. **Use my location** - the common case, because most people add a place
 *    while standing in it.
 * 2. **Tap or drag** - for adding somewhere they are not.
 * 3. **Search** - kept, but demoted to a way of *moving the map*, not a way of
 *    setting the answer. The pin still has to be placed.
 *
 * The address is reverse-geocoded and shown underneath, purely as confirmation.
 * The coordinates are the truth; a missing label is cosmetic.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import {
  Map as MapLibreMap,
  Marker,
  NavigationControl,
  type StyleSpecification,
} from 'maplibre-gl'
import 'maplibre-gl/dist/maplibre-gl.css'
import { Crosshair, LoaderCircle, MapPin, Search } from 'lucide-react'

import { api } from '@/lib/api'
import { Button, Input } from '@/design-system/primitives'

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

/** Close enough to read street names, which is what confirms a pin is right. */
const PLACE_ZOOM = 17
const CITY_ZOOM = 13

export interface PickedLocation {
  latitude: number
  longitude: number
  label: string | null
}

export interface LocationPickerProps {
  /** Where to open. The explorer's city centre if they have not shared location. */
  centre: { latitude: number; longitude: number }
  value: PickedLocation | null
  onChange: (location: PickedLocation) => void
  citySlug: string
  className?: string
}

export function LocationPicker({
  centre,
  value,
  onChange,
  citySlug,
  className,
}: LocationPickerProps) {
  const container = useRef<HTMLDivElement>(null)
  const map = useRef<MapLibreMap | null>(null)
  const marker = useRef<Marker | null>(null)
  // Held in a ref as well as state: the map's event handlers are bound once and
  // would otherwise close over the first render's callback forever.
  const onChangeRef = useRef(onChange)
  onChangeRef.current = onChange

  const [locating, setLocating] = useState(false)
  const [labelling, setLabelling] = useState(false)
  const [query, setQuery] = useState('')
  const [searching, setSearching] = useState(false)
  const [problem, setProblem] = useState<string | null>(null)

  /** Move the pin, then ask what is there. */
  const place = useCallback(
    async (latitude: number, longitude: number) => {
      setProblem(null)
      // Report the coordinates immediately. The label arrives later and must
      // never gate the answer - a publisher who taps and submits at once has
      // still chosen a real place.
      onChangeRef.current({ latitude, longitude, label: null })
      marker.current?.setLngLat([longitude, latitude])
      map.current?.easeTo({ center: [longitude, latitude], duration: 400 })

      setLabelling(true)
      try {
        const located = await api.locate(latitude, longitude)
        onChangeRef.current({ latitude, longitude, label: located.label ?? null })
      } catch {
        // A missing label is cosmetic; the pin is already placed.
      } finally {
        setLabelling(false)
      }
    },
    [],
  )

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

    const pin = document.createElement('div')
    pin.className =
      'grid size-8 -translate-y-2 place-items-center rounded-full border-2 border-white ' +
      'bg-brand-600 text-white shadow-lifted'
    pin.innerHTML =
      '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
      'stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">' +
      '<path d="M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0 1 16 0Z"/><circle cx="12" cy="10" r="3"/></svg>'

    marker.current = new Marker({ element: pin, draggable: true })
      .setLngLat(start)
      .addTo(instance)

    // Dragging reads as "move this here", tapping as "put it there". Both are
    // natural and people use both, so both are supported.
    marker.current.on('dragend', () => {
      const { lat, lng } = marker.current!.getLngLat()
      void place(lat, lng)
    })
    instance.on('click', (event) => {
      void place(event.lngLat.lat, event.lngLat.lng)
    })

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
    // changes move the map through `place` rather than by rebuilding it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  function useMyLocation() {
    if (!('geolocation' in navigator)) {
      setProblem('This browser cannot share your location. Tap the map instead.')
      return
    }
    setLocating(true)
    setProblem(null)
    navigator.geolocation.getCurrentPosition(
      (position) => {
        setLocating(false)
        void place(position.coords.latitude, position.coords.longitude)
      },
      (error) => {
        setLocating(false)
        setProblem(
          error.code === error.PERMISSION_DENIED
            ? 'Location is blocked for this site. Tap the map to place the pin instead.'
            : 'Could not get your location. Tap the map to place the pin instead.',
        )
      },
      // A venue pin needs street-level accuracy, and it is worth a few seconds
      // to get it rather than dropping the pin on a cell tower.
      { enableHighAccuracy: true, timeout: 10_000, maximumAge: 0 },
    )
  }

  async function search() {
    if (query.trim().length < 3) return
    setSearching(true)
    setProblem(null)
    try {
      const found = await api.geocode(query.trim(), citySlug)
      // Moves the map and the pin, but the publisher still sees and confirms it.
      // Search is a way of getting the map to roughly the right place, not a way
      // of asserting an answer.
      await place(found.latitude, found.longitude)
    } catch {
      setProblem('Could not find that. Try a landmark, or just tap the map.')
    } finally {
      setSearching(false)
    }
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-2">
        <Button
          type="button"
          variant="secondary"
          size="sm"
          onClick={useMyLocation}
          disabled={locating}
        >
          {locating ? (
            <LoaderCircle className="size-4 animate-spin" aria-hidden />
          ) : (
            <Crosshair className="size-4" aria-hidden />
          )}
          {locating ? 'Finding you…' : "I'm here now"}
        </Button>

        <div className="flex min-w-[12rem] flex-1 gap-2">
          <Input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') {
                event.preventDefault()
                void search()
              }
            }}
            placeholder="Or search for a landmark"
            aria-label="Search for a place to move the map"
          />
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => void search()}
            disabled={searching || query.trim().length < 3}
            aria-label="Search"
          >
            <Search className="size-4" aria-hidden />
          </Button>
        </div>
      </div>

      <div
        ref={container}
        className={className ?? 'h-72 w-full overflow-hidden rounded-xl border border-sand-200'}
        role="application"
        aria-label="Map. Tap to place the pin, or drag it."
      />

      <p className="text-sm text-sand-600">
        {value ? (
          <span className="inline-flex items-start gap-1.5">
            <MapPin className="mt-0.5 size-4 shrink-0 text-brand-600" aria-hidden />
            <span>
              <span className="text-sand-900">{value.label ?? 'Pin placed'}</span>
              {labelling && <span className="text-sand-500"> — checking…</span>}
              <span className="block text-xs text-sand-500">
                {value.latitude.toFixed(5)}, {value.longitude.toFixed(5)}
              </span>
            </span>
          </span>
        ) : (
          'Tap the map where the place is, or use the buttons above.'
        )}
      </p>

      {problem && (
        <p className="text-sm text-red-700" role="alert">
          {problem}
        </p>
      )}
    </div>
  )
}

export default LocationPicker
