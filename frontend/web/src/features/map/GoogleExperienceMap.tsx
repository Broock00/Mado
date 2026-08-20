/**
 * The results map, drawn by Google.
 *
 * Behaviourally identical to the MapLibre implementation beside it, because the
 * two are chosen by a setting and a map that behaved differently depending on
 * who was billing for it would be a map nobody could reason about. Same numbered
 * pins matching the list, same fit-to-what-is-shown camera, same refusal to tilt
 * or rotate.
 *
 * What it does *not* do is ask Google what is nearby. Spec 82.01 s10 again: the
 * catalogue is ours and the ranking is ours, so the pins come from the
 * already-ranked array this is handed. The vendor draws the ground under them.
 */

import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { googleMapId, loadGoogleMaps } from './google'
import { FALLBACK_CENTRE, type ExperienceMapProps } from './types'

export function GoogleExperienceMap({
  experiences,
  origin,
  className,
  zoom,
}: ExperienceMapProps) {
  const container = useRef<HTMLDivElement>(null)
  const map = useRef<google.maps.Map | null>(null)
  const markers = useRef<google.maps.marker.AdvancedMarkerElement[]>([])
  const info = useRef<google.maps.InfoWindow | null>(null)
  const navigate = useNavigate()
  // Drives the pin effect: the library arrives asynchronously, so the first
  // render has no map to draw on and the effect must run again once there is.
  const [ready, setReady] = useState(false)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    let live = true
    loadGoogleMaps()
      .then(() => {
        if (!live || !container.current || map.current) return
        map.current = new google.maps.Map(container.current, {
          center: { lat: FALLBACK_CENTRE.latitude, lng: FALLBACK_CENTRE.longitude },
          zoom: zoom ?? 12,
          mapId: googleMapId,
          // Tilt and rotation add nothing to "where is this" and make the map
          // easy to leave in a confusing state on a touch screen.
          tilt: 0,
          rotateControl: false,
          mapTypeControl: false,
          streetViewControl: false,
          fullscreenControl: false,
          clickableIcons: false,
        })
        info.current = new google.maps.InfoWindow({ disableAutoPan: true })
        setReady(true)
      })
      .catch(() => {
        if (live) setFailed(true)
      })

    return () => {
      live = false
      for (const marker of markers.current) marker.map = null
      markers.current = []
      info.current?.close()
      info.current = null
      // Google has no `destroy`. Dropping the reference and letting React remove
      // the container is the documented way to be rid of a map; holding the
      // instance would keep its listeners and its DOM alive after unmount.
      map.current = null
    }
  }, [zoom])

  // Pins are redrawn whenever the ranked set changes.
  useEffect(() => {
    const instance = map.current
    if (!instance || !ready) return

    for (const marker of markers.current) marker.map = null
    markers.current = []

    const located = experiences.filter((item) => item.venue?.latitude && item.venue?.longitude)
    const bounds = new google.maps.LatLngBounds()

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

      const position = { lat: venue.latitude, lng: venue.longitude }
      const marker = new google.maps.marker.AdvancedMarkerElement({
        map: instance,
        position,
        content: element,
        title: item.title,
      })
      // On the element, not the marker: `gmp-click` only fires for markers
      // Google made interactive, and a plain listener on our own button works
      // for a pointer and for the keyboard, which the marker's own event does
      // not.
      element.addEventListener('click', () => navigate(`/experiences/${item.id}`))
      element.addEventListener('mouseenter', () => {
        info.current?.setContent(item.title)
        info.current?.open({ map: instance, anchor: marker })
      })
      element.addEventListener('mouseleave', () => info.current?.close())

      markers.current.push(marker)
      bounds.extend(position)
    })

    if (origin) {
      const element = document.createElement('div')
      element.setAttribute('aria-label', 'Your location')
      element.className = 'size-4 rounded-full border-2 border-white bg-sand-900 shadow-lifted'
      const position = { lat: origin.latitude, lng: origin.longitude }
      markers.current.push(
        new google.maps.marker.AdvancedMarkerElement({
          map: instance,
          position,
          content: element,
          title: 'Your location',
        }),
      )
      bounds.extend(position)
    }

    // Fit to what is actually shown. A fixed centre would put the pins off
    // screen the moment an explorer searches for something across town.
    if (!bounds.isEmpty() && zoom === undefined) {
      instance.fitBounds(bounds, 56)
      // fitBounds on a handful of pins in one street zooms to the building.
      // Capped once the camera has settled, because the zoom is not applied
      // synchronously and reading it straight after fitBounds gives the old one.
      google.maps.event.addListenerOnce(instance, 'idle', () => {
        const settled = instance.getZoom()
        if (settled !== undefined && settled > 15) instance.setZoom(15)
      })
    } else if (located.length === 1) {
      const venue = located[0].venue!
      instance.setCenter({ lat: venue.latitude, lng: venue.longitude })
    }
  }, [experiences, origin, navigate, zoom, ready])

  if (failed) {
    return (
      <div
        className={className ?? 'h-[28rem] w-full overflow-hidden rounded-xl'}
        role="region"
        aria-label="Map of results"
      >
        <p className="grid size-full place-items-center bg-sand-100 p-4 text-center text-sm text-sand-600">
          The map could not load. The results are listed above.
        </p>
      </div>
    )
  }

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

export default GoogleExperienceMap
