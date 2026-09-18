/**
 * Listing details for the stops in a plan.
 *
 * A plan only carries titles and times - enough to be feasible, not enough to
 * draw a card. Each stop is an experience, so the photo is fetched by that id.
 * Failures are ignored per stop: a missing photo must not blank the evening,
 * and the planner's own times stay authoritative.
 */

import { useQueries } from '@tanstack/react-query'

import { api } from '@/lib/api'
import type { ExperienceDetail, PlanStop } from '@/lib/types'

export function useStopDetails(stops: PlanStop[]): Map<string, ExperienceDetail> {
  const results = useQueries({
    queries: stops.map((stop) => ({
      queryKey: ['experience', stop.experienceId],
      queryFn: () => api.experience(stop.experienceId),
      staleTime: 30 * 60_000,
      retry: false,
    })),
  })

  const byId = new Map<string, ExperienceDetail>()
  stops.forEach((stop, index) => {
    const detail = results[index]?.data
    if (detail) byId.set(stop.experienceId, detail)
  })
  return byId
}
