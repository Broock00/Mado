/**
 * Global client state (spec 80.06 s13).
 *
 * Deliberately small. Server data lives in React Query; this store holds only
 * what the client itself owns: session, chosen city, and location consent.
 */

import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import type { ChosenPlace, Me } from '@/lib/types'
import { tokenStore } from '@/lib/api'

/**
 * Lifecycle of "Use my location".
 *
 * `ready` is the only state whose coordinates may reach nearby APIs. Coarse
 * network/IP successes land in `too_vague` rather than `ready`, so a wrong city
 * cannot silently rank the feed. `denied` is permission only - timeouts and
 * hardware failures are `unavailable`, so the explorer can try again.
 */
export type DeviceLocationStatus =
  | 'idle'
  | 'requesting'
  | 'ready'
  | 'denied'
  | 'unavailable'
  | 'too_vague'

export interface LocationState {
  latitude: number | null
  longitude: number | null
  /** Metres of uncertainty from the Geolocation API. Null when not ready. */
  accuracyMetres: number | null
  /** Explicit consent lifecycle, per spec PRODUCT-00 principle 9. */
  status: DeviceLocationStatus
}

export const IDLE_LOCATION: LocationState = {
  latitude: null,
  longitude: null,
  accuracyMetres: null,
  status: 'idle',
}

/** Coordinates may be sent to discover / autocomplete / concierge only then. */
export function isLocationReady(location: LocationState): boolean {
  return (
    location.status === 'ready' &&
    location.latitude != null &&
    location.longitude != null
  )
}

interface AppState {
  user: Me | null
  /** Null means "wherever I am" - the server resolves it from coordinates. */
  citySlug: string | null
  /**
   * A place the explorer chose to look at instead of where they are.
   *
   * Null is the normal state: discovery follows the device's coordinates. This
   * is set when somebody searches for somewhere - Brooklyn, 5th Avenue - and it
   * carries the resolved point rather than the text, so the same search cannot
   * quietly resolve somewhere else on the next request.
   */
  place: ChosenPlace | null
  location: LocationState
  conciergeOpen: boolean

  setUser: (user: Me | null) => void
  setCity: (slug: string | null) => void
  setPlace: (place: ChosenPlace | null) => void
  beginLocationRequest: () => void
  setLocationReady: (latitude: number, longitude: number, accuracyMetres: number) => void
  setLocationDenied: () => void
  setLocationUnavailable: () => void
  setLocationTooVague: (accuracyMetres: number | null) => void
  clearLocation: () => void
  toggleConcierge: (open?: boolean) => void
  signOut: () => void
}

export const useAppStore = create<AppState>()(
  persist(
    (set) => ({
      user: null,
      citySlug: null,
      place: null,
      location: { ...IDLE_LOCATION },
      conciergeOpen: false,

      setUser: (user) => set({ user }),
      setCity: (citySlug) => set({ citySlug }),
      setPlace: (place) => set({ place }),
      beginLocationRequest: () =>
        set({
          // Clear coords while requesting so a previous ready fix cannot keep
          // driving nearby APIs under a "finding you" banner.
          location: {
            latitude: null,
            longitude: null,
            accuracyMetres: null,
            status: 'requesting',
          },
        }),
      setLocationReady: (latitude, longitude, accuracyMetres) =>
        set({
          location: {
            latitude,
            longitude,
            accuracyMetres,
            status: 'ready',
          },
        }),
      setLocationDenied: () =>
        set({
          location: {
            latitude: null,
            longitude: null,
            accuracyMetres: null,
            status: 'denied',
          },
        }),
      setLocationUnavailable: () =>
        set({
          location: {
            latitude: null,
            longitude: null,
            accuracyMetres: null,
            status: 'unavailable',
          },
        }),
      setLocationTooVague: (accuracyMetres) =>
        set({
          location: {
            latitude: null,
            longitude: null,
            accuracyMetres,
            status: 'too_vague',
          },
        }),
      clearLocation: () => set({ location: { ...IDLE_LOCATION } }),
      toggleConcierge: (open) =>
        set((state) => ({ conciergeOpen: open ?? !state.conciergeOpen })),
      signOut: () => {
        tokenStore.clear()
        set({ user: null })
      },
    }),
    {
      name: 'mado.app',
      // The user object is refetched from /me on load, and coordinates are
      // re-requested per session, so neither is persisted. Persisting a stale
      // location would silently mis-rank the first page view.
      // The chosen place is persisted: somebody planning a trip to Lisbon
      // should not be dragged back to their own street by a page refresh.
      partialize: (state) => ({ citySlug: state.citySlug, place: state.place }),

      // Version 0 had no city picker and started every browser on a hardcoded
      // 'addis-ababa', which then persisted forever - so an explorer anywhere
      // on earth kept being shown one Ethiopian city, and changing the default
      // alone would not have moved a single existing browser. Dropping it is
      // safe precisely because v0 offered no way to choose: every stored value
      // was the default rather than somebody's decision.
      version: 1,
      migrate: (persisted, version) => {
        const state = (persisted ?? {}) as { citySlug?: string | null }
        if (version < 1) return { ...state, citySlug: null }
        return state
      },
    },
  ),
)
