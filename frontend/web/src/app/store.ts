/**
 * Global client state (spec 80.06 s13).
 *
 * Deliberately small. Server data lives in React Query; this store holds only
 * what the client itself owns: session, chosen city, and location consent.
 */

import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import type { Me } from '@/lib/types'
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
  citySlug: string
  location: LocationState
  conciergeOpen: boolean

  setUser: (user: Me | null) => void
  setCity: (slug: string) => void
  setLocation: (latitude: number, longitude: number) => void
  denyLocation: () => void
  clearLocation: () => void
  toggleConcierge: (open?: boolean) => void
  signOut: () => void
}

const DEFAULT_CITY = 'addis-ababa'

export const useAppStore = create<AppState>()(
  persist(
    (set) => ({
      user: null,
      citySlug: DEFAULT_CITY,
      location: { latitude: null, longitude: null, granted: false, denied: false },
      conciergeOpen: false,

      setUser: (user) => set({ user }),
      setCity: (citySlug) => set({ citySlug }),
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
      partialize: (state) => ({ citySlug: state.citySlug }),
    },
  ),
)
