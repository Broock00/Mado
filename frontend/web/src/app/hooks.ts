/** Shared data hooks. */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useCallback } from 'react'
import { api, tokenStore, type DiscoveryParams } from '@/lib/api'
import { useAppStore } from '@/app/store'
import type { ExperienceSummary } from '@/lib/types'

/**
 * Where discovery should look.
 *
 * A chosen place wins - somebody who searched for Brooklyn is asking about
 * Brooklyn, wherever they happen to be sitting. Otherwise the device's own
 * coordinates, which is the case that should need no interaction at all.
 *
 * The point is sent, never the place's name. Sending the text would re-resolve
 * it on every request, and a search that quietly resolved somewhere else the
 * second time is a hard thing to notice and a harder one to report.
 */
export function useDiscoveryParams(limit = 12): DiscoveryParams {
  const citySlug = useAppStore((s) => s.citySlug)
  const place = useAppStore((s) => s.place)
  const location = useAppStore((s) => s.location)

  if (place) {
    return {
      lat: place.latitude,
      lng: place.longitude,
      // A country is sent as a code and a wide place as a box; only somewhere
      // small enough to be a circle is sent as a radius.
      ...(place.countryCode
        ? { country: place.countryCode }
        : place.bbox
          ? { bbox: place.bbox }
          : { radiusKm: place.radiusKm }),
      limit,
    }
  }
  return {
    city: citySlug ?? undefined,
    lat: location.granted ? location.latitude : undefined,
    lng: location.granted ? location.longitude : undefined,
    limit,
  }
}

/**
 * Where the explorer actually is, in words.
 *
 * Resolved from the device's coordinates through a geocoding service, so it
 * names a street in a town nobody has ever added to Mado. Skipped entirely when
 * a place has been chosen - the interface should say Brooklyn then, not the
 * street the explorer is standing on.
 */
export function useLocationContext() {
  const location = useAppStore((s) => s.location)
  const place = useAppStore((s) => s.place)
  const enabled = Boolean(
    !place && location.granted && location.latitude != null && location.longitude != null,
  )

  return useQuery({
    queryKey: ['place-context', location.latitude, location.longitude],
    queryFn: () => api.resolvePlace(location.latitude!, location.longitude!),
    enabled,
    // Somebody does not move far enough to change neighbourhood while browsing,
    // and every miss costs a request to somebody else's service.
    staleTime: 30 * 60_000,
    retry: false,
  })
}

export function useCanvas() {
  const params = useDiscoveryParams()
  return useQuery({
    queryKey: ['canvas', params],
    queryFn: () => api.canvas(params),
    // Time-sensitive rails go stale quickly; a minute keeps "starting soon"
    // honest without hammering the API on every navigation.
    staleTime: 60_000,
  })
}

export function useSession() {
  const setUser = useAppStore((s) => s.setUser)
  return useQuery({
    queryKey: ['me'],
    queryFn: async () => {
      const me = await api.me()
      setUser(me)
      return me
    },
    // Only attempt when a token exists - anonymous browsing is a first-class
    // state, not a failed auth check (spec 10.01.01).
    enabled: Boolean(tokenStore.access),
    retry: false,
    staleTime: 5 * 60_000,
  })
}

/**
 * Toggle saved state with an optimistic update.
 *
 * Saving is a low-stakes, high-frequency action; waiting for a round-trip makes
 * the interface feel slower than the platform actually is. On failure the caches
 * roll back.
 */
export function useToggleSave() {
  const queryClient = useQueryClient()
  const user = useAppStore((s) => s.user)

  const mutation = useMutation({
    mutationFn: async (experience: ExperienceSummary) => {
      if (experience.isSaved) {
        await api.unsave('experience', experience.id)
        return { id: experience.id, saved: false }
      }
      await api.save('experience', experience.id)
      return { id: experience.id, saved: true }
    },
    onMutate: async (experience) => {
      await queryClient.cancelQueries()
      const snapshot = queryClient.getQueryCache().getAll().map((q) => ({
        key: q.queryKey,
        data: q.state.data,
      }))
      patchSavedState(queryClient, experience.id, !experience.isSaved)
      return { snapshot }
    },
    onError: (_error, _experience, context) => {
      context?.snapshot.forEach(({ key, data }) => queryClient.setQueryData(key, data))
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ['saved'] })
    },
  })

  return {
    toggle: mutation.mutate,
    isPending: mutation.isPending,
    requiresAuth: !user,
  }
}

/** Rewrite isSaved wherever this experience appears in any cached query. */
function patchSavedState(
  queryClient: ReturnType<typeof useQueryClient>,
  experienceId: string,
  saved: boolean,
) {
  const patch = (value: unknown): unknown => {
    if (Array.isArray(value)) return value.map(patch)
    if (value && typeof value === 'object') {
      const record = value as Record<string, unknown>
      if (record.id === experienceId && 'isSaved' in record) {
        return { ...record, isSaved: saved }
      }
      const next: Record<string, unknown> = {}
      let changed = false
      for (const [key, item] of Object.entries(record)) {
        const updated = patch(item)
        next[key] = updated
        if (updated !== item) changed = true
      }
      return changed ? next : value
    }
    return value
  }

  queryClient.getQueryCache().getAll().forEach((query) => {
    const current = query.state.data
    if (current === undefined) return
    const updated = patch(current)
    if (updated !== current) queryClient.setQueryData(query.queryKey, updated)
  })
}

/**
 * Whether one feature is on for whoever is looking (spec ADM-003).
 *
 * The whole resolved map is fetched once and shared, rather than a request per
 * flag. The server sends resolved booleans only - a client has no business
 * knowing a feature exists at 5% while it is off for this explorer.
 *
 * Returns `false` while loading and `false` if the request fails. Both are
 * deliberate: a flag turns something new on, so not knowing means the behaviour
 * that existed before, and a flag that flickered on as its request landed would
 * be worse than one that arrives a moment late.
 */
export function useFlag(key: string): boolean {
  const { data } = useQuery({
    queryKey: ['my-flags'],
    queryFn: () => api.myFlags(),
    // Changed by a moderator rather than by the explorer, so it will not change
    // under them mid-session; refetching per page would be a request for
    // nothing.
    staleTime: 5 * 60_000,
    retry: false,
  })
  return data?.[key] ?? false
}

/** Request browser location, recording consent or refusal. */
export function useRequestLocation() {
  const setLocation = useAppStore((s) => s.setLocation)
  const denyLocation = useAppStore((s) => s.denyLocation)

  return useCallback(() => {
    if (!('geolocation' in navigator)) {
      denyLocation()
      return
    }
    navigator.geolocation.getCurrentPosition(
      (position) => setLocation(position.coords.latitude, position.coords.longitude),
      () => denyLocation(),
      { enableHighAccuracy: false, timeout: 8000, maximumAge: 300_000 },
    )
  }, [setLocation, denyLocation])
}
