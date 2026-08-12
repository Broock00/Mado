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

interface LocationState {
  latitude: number | null
  longitude: number | null
  /** Explicit consent, per spec PRODUCT-00 principle 9 - never assumed. */
  granted: boolean
  denied: boolean
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
  setLocation: (latitude: number, longitude: number) => void
  denyLocation: () => void
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
      location: { latitude: null, longitude: null, granted: false, denied: false },
      conciergeOpen: false,

      setUser: (user) => set({ user }),
      setCity: (citySlug) => set({ citySlug }),
      setPlace: (place) => set({ place }),
      setLocation: (latitude, longitude) =>
        set({ location: { latitude, longitude, granted: true, denied: false } }),
      denyLocation: () =>
        set({ location: { latitude: null, longitude: null, granted: false, denied: true } }),
      clearLocation: () =>
        set({ location: { latitude: null, longitude: null, granted: false, denied: false } }),
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
