/**
 * The explorer's live position while navigating.
 *
 * `watchPosition` rather than repeated `getCurrentPosition`: the browser keeps
 * the GPS warm and pushes a fix when it improves, which is both more accurate
 * and less draining than asking on a timer.
 *
 * **The position never leaves the device.** Nothing here posts anywhere. The
 * route was downloaded once; following it is arithmetic on this machine. A
 * platform streaming somebody's minute-by-minute location to its own servers
 * needs a reason, and "so the line moves" is not one.
 *
 * The watch is started only when navigation starts and torn down the moment it
 * stops or the component unmounts. A forgotten watch is a GPS running in a
 * background tab until the battery dies.
 */

import { useCallback, useEffect, useRef, useState } from 'react'

export interface LiveFix {
  latitude: number
  longitude: number
  /** Metres of uncertainty the browser reports. Drawn, not hidden. */
  accuracyMetres: number
  /** Degrees clockwise from north, when the device knows. Null when stationary. */
  heading: number | null
  /** Metres per second, when the device knows. */
  speed: number | null
  at: number
}

export type LocationStatus = 'idle' | 'requesting' | 'watching' | 'denied' | 'unavailable'

export interface LiveLocation {
  fix: LiveFix | null
  status: LocationStatus
  error: string | null
  start: () => void
  stop: () => void
}

export function useLiveLocation(): LiveLocation {
  const [fix, setFix] = useState<LiveFix | null>(null)
  const [status, setStatus] = useState<LocationStatus>('idle')
  const [error, setError] = useState<string | null>(null)
  const watchId = useRef<number | null>(null)

  const stop = useCallback(() => {
    if (watchId.current !== null) {
      navigator.geolocation.clearWatch(watchId.current)
      watchId.current = null
    }
    setStatus('idle')
  }, [])

  const start = useCallback(() => {
    if (!('geolocation' in navigator)) {
      setStatus('unavailable')
      setError('This browser cannot report your location.')
      return
    }
    if (watchId.current !== null) return

    setStatus('requesting')
    setError(null)

    watchId.current = navigator.geolocation.watchPosition(
      (position) => {
        setStatus('watching')
        setFix({
          latitude: position.coords.latitude,
          longitude: position.coords.longitude,
          accuracyMetres: position.coords.accuracy,
          heading: Number.isFinite(position.coords.heading) ? position.coords.heading : null,
          speed: Number.isFinite(position.coords.speed) ? position.coords.speed : null,
          at: position.timestamp,
        })
      },
      (failure) => {
        // A denial is permanent until the explorer changes it, so the watch is
        // released rather than left retrying against a closed door.
        if (failure.code === failure.PERMISSION_DENIED) {
          setStatus('denied')
          setError('Location is blocked for this site. Allow it to follow the route.')
          stop()
          return
        }
        setStatus('unavailable')
        setError(
          failure.code === failure.TIMEOUT
            ? 'Could not get a fix. Somewhere with a clearer view of the sky may help.'
            : 'Your position is not available right now.',
        )
      },
      {
        // Worth the battery here and nowhere else in the product: following a
        // line needs metres, and the coarse fix used for "near me" ranking is
        // hundreds of metres out.
        enableHighAccuracy: true,
        timeout: 15_000,
        // Never a cached fix while navigating - a stale position is exactly the
        // failure that makes somebody walk the wrong way.
        maximumAge: 0,
      },
    )
  }, [stop])

  useEffect(() => stop, [stop])

  return { fix, status, error, start, stop }
}
