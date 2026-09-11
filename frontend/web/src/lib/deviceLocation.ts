/**
 * Device location for "Use my location" / near-me.
 *
 * The browser Geolocation API has no "disable IP" switch. On a laptop without
 * GPS it will happily return a network/IP city centroid and call it a success -
 * which is how an explorer in Hosanna ended up ranked around Addis Ababa.
 *
 * The permanent rule: ask for a fresh high-accuracy fix, read `accuracy`, and
 * only treat the result as "near me" when the uncertainty is small enough for
 * nearby search. Anything coarser is a failure with a real error state, never
 * a silent substitute. There is no GeoIP fallback here and must never be one.
 *
 * Navigation keeps a stricter gate (`UNUSABLE_ACCURACY_METRES` = 100): placing
 * somebody on a walking line needs metres. Nearby ranking only needs the right
 * neighbourhood, which is why this ceiling is wider - but still far below the
 * multi-kilometre blobs typical of IP geolocation.
 */

/** Fresh, high-accuracy request shared by discover / near-me / concierge. */
export const DEVICE_LOCATION_OPTIONS: PositionOptions = {
  enableHighAccuracy: true,
  maximumAge: 0,
  // GPS cold starts need more than a few seconds; cutting this short forces the
  // browser back onto a cached network estimate, which is the failure mode.
  timeout: 20_000,
}

/**
 * Ceiling on `coords.accuracy` for nearby discovery and autocomplete bias.
 *
 * Below this: Wi-Fi / GPS neighbourhood fixes. Above it: treat as unusable so
 * a city-scale IP guess cannot drive "near me". Tuned against the product need
 * (right area for rails), not against street-level navigation.
 */
export const NEARBY_MAX_ACCURACY_METRES = 2_000

export type DeviceLocationFailureKind =
  | 'denied'
  | 'unavailable'
  | 'too_vague'
  | 'unsupported'

export interface DeviceLocationFix {
  latitude: number
  longitude: number
  accuracyMetres: number
}

export type DeviceLocationFailure =
  | { kind: 'denied' }
  | { kind: 'unsupported' }
  | { kind: 'unavailable'; message: string }
  | { kind: 'too_vague'; accuracyMetres: number }

export type DeviceLocationResult =
  | { ok: true; fix: DeviceLocationFix }
  | { ok: false; failure: DeviceLocationFailure }

/** True when the reported uncertainty is tight enough for nearby search. */
export function isNearbyAccuracyAcceptable(accuracyMetres: number): boolean {
  return Number.isFinite(accuracyMetres) && accuracyMetres > 0 && accuracyMetres <= NEARBY_MAX_ACCURACY_METRES
}

/**
 * Decide whether a Geolocation API success is usable for "near me".
 *
 * A missing or non-finite accuracy is refused: without it we cannot tell a
 * precise fix from a city blob, and guessing would re-open the Addis failure.
 */
export function assessNearbyFix(coords: {
  latitude: number
  longitude: number
  accuracy: number
}): DeviceLocationResult {
  const accuracyMetres = coords.accuracy
  if (!isNearbyAccuracyAcceptable(accuracyMetres)) {
    return {
      ok: false,
      failure: {
        kind: 'too_vague',
        accuracyMetres: Number.isFinite(accuracyMetres) ? accuracyMetres : Number.POSITIVE_INFINITY,
      },
    }
  }
  return {
    ok: true,
    fix: {
      latitude: coords.latitude,
      longitude: coords.longitude,
      accuracyMetres,
    },
  }
}

export function mapGeolocationError(error: unknown): DeviceLocationFailure {
  if (error && typeof error === 'object' && 'code' in error) {
    const code = (error as GeolocationPositionError).code
    if (code === 1 /* PERMISSION_DENIED */) return { kind: 'denied' }
    if (code === 3 /* TIMEOUT */) {
      return {
        kind: 'unavailable',
        message:
          'Could not get a precise location in time. Try again outdoors, or search for a place.',
      }
    }
  }
  return {
    kind: 'unavailable',
    message: 'Your precise location is not available right now. Try again, or search for a place.',
  }
}

/** Copy for banners and empty states - one sentence per failure kind. */
export function locationFailureMessage(failure: DeviceLocationFailure): string {
  switch (failure.kind) {
    case 'denied':
      return 'Location is blocked for this site. Allow it in your browser settings, or search for a place above.'
    case 'unsupported':
      return 'This browser cannot report your location. Search for a place above instead.'
    case 'too_vague':
      return 'We could only get an approximate location, which is not accurate enough for nearby. Try again with location services on, or search for a place.'
    case 'unavailable':
      return failure.message
  }
}

function readPosition(): Promise<GeolocationPosition> {
  return new Promise((resolve, reject) => {
    navigator.geolocation.getCurrentPosition(resolve, reject, DEVICE_LOCATION_OPTIONS)
  })
}

/**
 * Obtain a device fix suitable for nearby search.
 *
 * On a too-vague first answer, tries once more: phones often return a coarse
 * network estimate before the GPS lock arrives. A second vague result is final -
 * never accepted "because something came back".
 */
export async function requestNearbyDeviceLocation(
  options: { retryOnceIfVague?: boolean } = {},
): Promise<DeviceLocationResult> {
  const retryOnceIfVague = options.retryOnceIfVague !== false

  if (typeof navigator === 'undefined' || !('geolocation' in navigator)) {
    return { ok: false, failure: { kind: 'unsupported' } }
  }

  try {
    let assessed = assessNearbyFix((await readPosition()).coords)
    if (!assessed.ok && assessed.failure.kind === 'too_vague' && retryOnceIfVague) {
      assessed = assessNearbyFix((await readPosition()).coords)
    }
    return assessed
  } catch (error) {
    return { ok: false, failure: mapGeolocationError(error) }
  }
}
