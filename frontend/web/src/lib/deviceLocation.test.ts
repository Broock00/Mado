/**
 * Nearby device location gates.
 *
 * These exist because the Geolocation API will report a network/IP city
 * centroid as a successful fix. Accepting that as "near me" ranked explorers
 * in the wrong city. The accuracy ceiling is the product control; the request
 * options are how we ask for a real device fix first.
 */

import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  NEARBY_MAX_ACCURACY_METRES,
  assessNearbyFix,
  isNearbyAccuracyAcceptable,
  locationFailureMessage,
  mapGeolocationError,
  requestNearbyDeviceLocation,
} from './deviceLocation'

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('isNearbyAccuracyAcceptable', () => {
  it('accepts neighbourhood-scale GPS and Wi-Fi fixes', () => {
    expect(isNearbyAccuracyAcceptable(12)).toBe(true)
    expect(isNearbyAccuracyAcceptable(500)).toBe(true)
    expect(isNearbyAccuracyAcceptable(NEARBY_MAX_ACCURACY_METRES)).toBe(true)
  })

  it('rejects IP-grade and unknown uncertainty', () => {
    // Typical browser IP / cell estimates land well above the nearby ceiling.
    expect(isNearbyAccuracyAcceptable(NEARBY_MAX_ACCURACY_METRES + 1)).toBe(false)
    expect(isNearbyAccuracyAcceptable(5_000)).toBe(false)
    expect(isNearbyAccuracyAcceptable(50_000)).toBe(false)
    expect(isNearbyAccuracyAcceptable(0)).toBe(false)
    expect(isNearbyAccuracyAcceptable(-1)).toBe(false)
    expect(isNearbyAccuracyAcceptable(Number.NaN)).toBe(false)
    expect(isNearbyAccuracyAcceptable(Number.POSITIVE_INFINITY)).toBe(false)
  })
})

describe('assessNearbyFix', () => {
  it('keeps a precise device fix', () => {
    const result = assessNearbyFix({
      latitude: 7.5356197,
      longitude: 37.8525268,
      accuracy: 35,
    })
    expect(result).toEqual({
      ok: true,
      fix: {
        latitude: 7.5356197,
        longitude: 37.8525268,
        accuracyMetres: 35,
      },
    })
  })

  it('refuses an Addis-style city blob even when coordinates look confident', () => {
    // The Hosanna failure: browser returned Addis IP coords as a "success".
    const result = assessNearbyFix({
      latitude: 9.0245,
      longitude: 38.7485,
      accuracy: 25_000,
    })
    expect(result.ok).toBe(false)
    if (!result.ok) {
      expect(result.failure.kind).toBe('too_vague')
    }
  })

  it('refuses a missing accuracy rather than guessing', () => {
    const result = assessNearbyFix({
      latitude: 7.5,
      longitude: 37.8,
      accuracy: Number.NaN,
    })
    expect(result.ok).toBe(false)
    if (!result.ok) {
      expect(result.failure.kind).toBe('too_vague')
    }
  })
})

describe('mapGeolocationError', () => {
  it('maps permission denial separately from timeouts', () => {
    expect(mapGeolocationError({ code: 1 }).kind).toBe('denied')
    expect(mapGeolocationError({ code: 3 }).kind).toBe('unavailable')
    expect(mapGeolocationError({ code: 2 }).kind).toBe('unavailable')
    expect(mapGeolocationError(new Error('nope')).kind).toBe('unavailable')
  })
})

describe('locationFailureMessage', () => {
  it('never tells the explorer to wait for an IP fallback', () => {
    expect(locationFailureMessage({ kind: 'denied' })).toMatch(/blocked|Allow/i)
    expect(
      locationFailureMessage({ kind: 'too_vague', accuracyMetres: 20_000 }),
    ).toMatch(/precise|approximate|search/i)
    expect(
      locationFailureMessage({
        kind: 'unavailable',
        message: 'Your precise location is not available right now.',
      }),
    ).toMatch(/precise location/i)
  })
})

describe('requestNearbyDeviceLocation', () => {
  it('reports unsupported when geolocation is missing', async () => {
    vi.stubGlobal('navigator', {})
    const result = await requestNearbyDeviceLocation()
    expect(result).toEqual({ ok: false, failure: { kind: 'unsupported' } })
  })

  it('accepts a precise first fix without retrying', async () => {
    const getCurrentPosition = vi.fn((success: PositionCallback) => {
      success({
        coords: {
          latitude: 7.5356197,
          longitude: 37.8525268,
          accuracy: 40,
          altitude: null,
          altitudeAccuracy: null,
          heading: null,
          speed: null,
          toJSON() {
            return this
          },
        },
        timestamp: Date.now(),
        toJSON() {
          return this
        },
      } as GeolocationPosition)
    })
    vi.stubGlobal('navigator', { geolocation: { getCurrentPosition } })

    const result = await requestNearbyDeviceLocation()
    expect(getCurrentPosition).toHaveBeenCalledTimes(1)
    expect(getCurrentPosition).toHaveBeenCalledWith(
      expect.any(Function),
      expect.any(Function),
      expect.objectContaining({
        enableHighAccuracy: true,
        maximumAge: 0,
      }),
    )
    expect(result.ok).toBe(true)
    if (result.ok) {
      expect(result.fix.latitude).toBeCloseTo(7.5356197)
      expect(result.fix.longitude).toBeCloseTo(37.8525268)
    }
  })

  it('retries once when the first fix is too vague, then accepts a precise second', async () => {
    const getCurrentPosition = vi
      .fn()
      .mockImplementationOnce((success: PositionCallback) => {
        success({
          coords: { latitude: 9.0245, longitude: 38.7485, accuracy: 30_000 },
          timestamp: Date.now(),
        } as GeolocationPosition)
      })
      .mockImplementationOnce((success: PositionCallback) => {
        success({
          coords: { latitude: 7.5356197, longitude: 37.8525268, accuracy: 25 },
          timestamp: Date.now(),
        } as GeolocationPosition)
      })
    vi.stubGlobal('navigator', { geolocation: { getCurrentPosition } })

    const result = await requestNearbyDeviceLocation()
    expect(getCurrentPosition).toHaveBeenCalledTimes(2)
    expect(result.ok).toBe(true)
    if (result.ok) {
      expect(result.fix.latitude).toBeCloseTo(7.5356197)
    }
  })

  it('fails closed when both attempts are city-scale', async () => {
    const getCurrentPosition = vi.fn((success: PositionCallback) => {
      success({
        coords: { latitude: 9.0245, longitude: 38.7485, accuracy: 20_000 },
        timestamp: Date.now(),
      } as GeolocationPosition)
    })
    vi.stubGlobal('navigator', { geolocation: { getCurrentPosition } })

    const result = await requestNearbyDeviceLocation()
    expect(getCurrentPosition).toHaveBeenCalledTimes(2)
    expect(result.ok).toBe(false)
    if (!result.ok) {
      expect(result.failure.kind).toBe('too_vague')
    }
  })

  it('maps a permission error to denied without inventing coordinates', async () => {
    const getCurrentPosition = vi.fn(
      (_success: PositionCallback, error: PositionErrorCallback) => {
        error({
          code: 1,
          message: 'denied',
          PERMISSION_DENIED: 1,
          POSITION_UNAVAILABLE: 2,
          TIMEOUT: 3,
        } as GeolocationPositionError)
      },
    )
    vi.stubGlobal('navigator', { geolocation: { getCurrentPosition } })

    const result = await requestNearbyDeviceLocation()
    expect(result).toEqual({ ok: false, failure: { kind: 'denied' } })
  })
})
