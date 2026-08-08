/**
 * How to get between the stops (spec MAP-002).
 *
 * Fetched separately from the itinerary rather than folded into it. Routing
 * calls an external service and can be slow or unavailable, and a plan must
 * still render when it is - somebody looking at tonight's evening should not
 * get a spinner because a map server is having a bad day.
 *
 * Two honesty rules the interface keeps:
 *
 * - **A leg the router could not serve is labelled.** The line is dashed and
 *   the duration is marked as an estimate, because a straight line drawn solid
 *   claims a road that nobody verified.
 * - **Drift is stated, not silently applied.** The plan's own times were
 *   computed from a cheap estimate while it was being solved. When the real
 *   route disagrees materially, that is said out loud - an explorer who has
 *   already read the times deserves to be told they moved.
 */

import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { AlertTriangle, Car, Footprints, Route as RouteIcon } from 'lucide-react'

import { api } from '@/lib/api'
import type { Itinerary } from '@/lib/types'
import { Card, SectionHeading } from '@/design-system/primitives'
import { RouteMap } from '@/features/map/RouteMap'
import { cn } from '@/lib/utils'

const MODES = [
  { value: undefined, label: 'Suggested' },
  { value: 'walk' as const, label: 'Walking' },
  { value: 'drive' as const, label: 'Driving' },
]

export function RouteGuidance({ itinerary }: { itinerary: Itinerary }) {
  const [mode, setMode] = useState<'walk' | 'drive' | undefined>(undefined)

  const { data: route, isLoading, isError } = useQuery({
    queryKey: ['itinerary-route', itinerary.id, mode],
    queryFn: () => api.itineraryRoute(itinerary.id, mode),
    // A plan with one stop has nothing to route between.
    enabled: itinerary.stops.length > 1,
    retry: false,
  })

  if (itinerary.stops.length < 2) return null

  const titles = itinerary.stops.map((stop) => stop.title)

  return (
    <section className="mt-8">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <SectionHeading
          title="Getting between them"
          subtitle="Real roads, and how long each hop actually takes."
        />
        <div className="flex gap-1 rounded-lg border border-sand-200 p-0.5" role="group">
          {MODES.map((option) => (
            <button
              key={option.label}
              type="button"
              onClick={() => setMode(option.value)}
              aria-pressed={mode === option.value}
              className={cn(
                'rounded-md px-3 py-1.5 text-sm transition-colors',
                mode === option.value
                  ? 'bg-brand-100 font-medium text-brand-900'
                  : 'text-sand-600 hover:text-sand-900',
              )}
            >
              {option.label}
            </button>
          ))}
        </div>
      </div>

      {isLoading && <Card className="mt-3 p-5 text-sm text-sand-500">Working out the route…</Card>}

      {isError && (
        <Card className="mt-3 p-5 text-sm text-sand-600">
          Could not work out the route just now. The plan and its times are unchanged.
        </Card>
      )}

      {route && (
        <>
          {route.warning && (
            <Card className="mt-3 flex items-start gap-3 border-amber-200 bg-amber-50 p-4">
              <AlertTriangle className="mt-0.5 size-4 shrink-0 text-amber-700" aria-hidden />
              <p className="text-sm text-amber-900">{route.warning}</p>
            </Card>
          )}

          <Card className="mt-3 overflow-hidden p-0">
            <RouteMap route={route} titles={titles} className="h-72 w-full" />
          </Card>

          <Card className="mt-3 divide-y divide-sand-200 px-5">
            {route.legs.map((leg) => (
              <div
                key={`${leg.fromIndex}-${leg.toIndex}`}
                className="flex items-center justify-between gap-4 py-3"
              >
                <span className="flex min-w-0 items-center gap-2 text-sm text-sand-800">
                  {leg.mode === 'walk' ? (
                    <Footprints className="size-4 shrink-0 text-sand-500" aria-hidden />
                  ) : (
                    <Car className="size-4 shrink-0 text-sand-500" aria-hidden />
                  )}
                  <span className="truncate">
                    {titles[leg.fromIndex]} → {titles[leg.toIndex]}
                  </span>
                </span>
                <span className="shrink-0 text-sm text-sand-600">
                  {leg.durationMinutes} min · {leg.distanceKm} km
                  {leg.isEstimated && (
                    <span className="ml-1.5 text-xs text-sand-400">estimated</span>
                  )}
                </span>
              </div>
            ))}

            <div className="flex items-center justify-between gap-4 py-3 text-sm">
              <span className="flex items-center gap-2 font-medium text-sand-900">
                <RouteIcon className="size-4 text-sand-500" aria-hidden />
                Total travel
              </span>
              <span className="text-sand-700">
                {route.totalDurationMinutes} min · {route.totalDistanceKm} km
              </span>
            </div>
          </Card>

          {route.estimatedLegs > 0 && (
            <p className="mt-2 text-xs text-sand-500">
              {route.estimatedLegs === route.legs.length
                ? 'No routing service was reachable, so these are straight-line estimates.'
                : `${route.estimatedLegs} of these hops could not be routed and is shown as a
                   straight-line estimate, drawn dashed.`}
            </p>
          )}
        </>
      )}
    </section>
  )
}
