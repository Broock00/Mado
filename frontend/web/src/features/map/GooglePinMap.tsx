/**
 * A map with one draggable pin, drawn by Google.
 *
 * The keyed half of `PinMap`, behaviourally identical to the MapLibre one: tap
 * to place, drag to move, and the pin follows `value` rather than an imperative
 * handle.
 *
 * `clickableIcons` is off deliberately. Google's own points of interest are
 * clickable by default, and on this map a tap means "the venue is here" - a tap
 * that opened Google's info card for the shop next door instead of moving the
 * pin would be the one interaction this whole component exists to make reliable.
 */

import { useEffect, useRef, useState } from 'react'

import { googleMapId, loadGoogleMaps, pinElement } from './google'
import { CITY_ZOOM, PLACE_ZOOM, type PinMapProps } from './types'

export function GooglePinMap({ centre, value, onPick, className }: PinMapProps) {
  const container = useRef<HTMLDivElement>(null)
  const map = useRef<google.maps.Map | null>(null)
  const marker = useRef<google.maps.marker.AdvancedMarkerElement | null>(null)
  const onPickRef = useRef(onPick)
  onPickRef.current = onPick
  // Seeds the initial camera. Read from a ref so a later change does not put
  // the map back where it started while somebody is looking at it.
  const start = useRef(value ?? centre)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    let live = true
    loadGoogleMaps()
      .then(() => {
        if (!live || !container.current || map.current) return
        const origin = {
          lat: start.current.latitude,
          lng: start.current.longitude,
        }
        const instance = new google.maps.Map(container.current, {
          center: origin,
          zoom: value ? PLACE_ZOOM : CITY_ZOOM,
          mapId: googleMapId,
          tilt: 0,
          rotateControl: false,
          mapTypeControl: false,
          streetViewControl: false,
          fullscreenControl: false,
          clickableIcons: false,
        })

        const pin = pinElement()
        marker.current = new google.maps.marker.AdvancedMarkerElement({
          map: instance,
          position: origin,
          content: pin,
          gmpDraggable: true,
        })

        marker.current.addListener('dragend', (event: google.maps.MapMouseEvent) => {
          if (event.latLng) onPickRef.current(event.latLng.lat(), event.latLng.lng())
        })
        instance.addListener('click', (event: google.maps.MapMouseEvent) => {
          if (event.latLng) onPickRef.current(event.latLng.lat(), event.latLng.lng())
        })

        map.current = instance
      })
      .catch(() => {
        if (live) setFailed(true)
      })

    return () => {
      live = false
      if (marker.current) marker.current.map = null
      marker.current = null
      map.current = null
    }
    // Mounts once; `value` moves the pin through the effect below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    if (!value || !map.current) return
    const position = { lat: value.latitude, lng: value.longitude }
    if (marker.current) marker.current.position = position
    map.current.panTo(position)
  }, [value])

  if (failed) {
    return (
      <div
        className={className ?? 'h-72 w-full overflow-hidden rounded-xl border border-sand-200'}
      >
        <p className="grid size-full place-items-center bg-sand-100 p-4 text-center text-sm text-sand-600">
          The map could not load. Use &ldquo;I&rsquo;m here now&rdquo; or search for the place
          instead.
        </p>
      </div>
    )
  }

  return (
    <div
      ref={container}
      className={className ?? 'h-72 w-full overflow-hidden rounded-xl border border-sand-200'}
      role="application"
      aria-label="Map. Tap to place the pin, or drag it."
    />
  )
}

export default GooglePinMap
