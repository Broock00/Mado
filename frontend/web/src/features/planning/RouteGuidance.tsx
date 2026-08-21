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

import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  AlertTriangle,
  Car,
  Crosshair,
  Footprints,
  Navigation,
  Route as RouteIcon,
  Square,
} from 'lucide-react'

import { api } from '@/lib/api'
import type { Itinerary } from '@/lib/types'
import { Button, Card, SectionHeading } from '@/design-system/primitives'
import { RouteMap } from '@/features/map/RouteMap'
import { useLiveLocation } from '@/features/map/useLiveLocation'
import { formatDistance } from '@/lib/geo'
import { cn } from '@/lib/utils'
import { useNavigation } from './useNavigation'

const MODES = [
  { value: undefined, label: 'Suggested' },
  { value: 'walk' as const, label: 'Walking' },
  { value: 'drive' as const, label: 'Driving' },
]

export function RouteGuidance({ itinerary }: { itinerary: Itinerary }) {
  const [mode, setMode] = useState<'walk' | 'drive' | undefined>(undefined)
  const [navigating, setNavigating] = useState(false)
  const location = useLiveLocation()

  const { data: route, isLoading, isError } = useQuery({
    queryKey: ['itinerary-route', itinerary.id, mode],
    queryFn: () => api.itineraryRoute(itinerary.id, mode),
    // A plan with one stop has nothing to route between.
    enabled: itinerary.stops.length > 1,
    retry: false,
  })

  // Memoised because RouteMap keys an effect on it. A fresh array every render
  // made the route line and every marker rebuild on each GPS tick, which kept
  // MapLibre permanently mid-update and stopped it ever painting.
  const titles = useMemo(() => itinerary.stops.map((stop) => stop.title), [itinerary.stops])
  const progress = useNavigation(route, location.fix, navigating)

  if (itinerary.stops.length < 2) return null

  function startNavigation() {
    setNavigating(true)
    location.start()
  }

  function stopNavigation() {
    setNavigating(false)
    location.stop()
  }

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
            <Card className="mt-3 flex items-start gap-3 border-accent-300/40 bg-accent-100 p-4">
              <AlertTriangle className="mt-0.5 size-4 shrink-0 text-accent-300" aria-hidden />
              <p className="text-sm text-accent-300">{route.warning}</p>
            </Card>
          )}

          <div className="mt-3 flex flex-wrap items-center gap-2">
            {navigating ? (
              <Button variant="secondary" onClick={stopNavigation}>
                <Square className="size-4" aria-hidden />
                Stop navigating
              </Button>
            ) : (
              <Button onClick={startNavigation}>
                <Navigation className="size-4" aria-hidden />
                Start navigating
              </Button>
            )}
            {navigating && (
              <span className="text-xs text-sand-500">
                Your position stays on this device - Mado is not told where you are.
              </span>
            )}
          </div>

          {navigating && <NavigationStatus location={location} progress={progress} titles={titles} />}

          <Card className="mt-3 overflow-hidden p-0">
            <RouteMap
              route={route}
              titles={titles}
              fix={navigating ? location.fix : null}
              alongMetres={progress.alongMetres}
              follow={navigating}
              // Taller while navigating. A 288px strip is fine for glancing at
              // a route; it is not enough to walk by, where the useful thing is
              // seeing the street ahead rather than the whole city.
              className={navigating ? 'h-[26rem] w-full' : 'h-72 w-full'}
            />
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

/**
 * What is happening right now, while navigating.
 *
 * Ordered by what somebody walking needs at a glance: how far to the next stop,
 * then anything wrong. Problems are stated plainly rather than dressed as
 * progress - a figure computed from a 300-metre GPS fix, or from a position
 * nowhere near the route, describes nothing.
 */
function NavigationStatus({
  location,
  progress,
  titles,
}: {
  location: ReturnType<typeof useLiveLocation>
  progress: ReturnType<typeof useNavigation>
  titles: string[]
}) {
  if (location.status === 'denied' || location.status === 'unavailable') {
    return (
      <Card className="mt-3 flex items-start gap-3 p-4">
        <AlertTriangle className="mt-0.5 size-4 shrink-0 text-sand-500" aria-hidden />
        <p className="text-sm text-sand-700">{location.error}</p>
      </Card>
    )
  }

  if (!location.fix) {
    return (
      <Card className="mt-3 flex items-center gap-3 p-4">
        <Crosshair className="size-4 shrink-0 animate-pulse text-brand-600" aria-hidden />
        <p className="text-sm text-sand-600">Finding you…</p>
      </Card>
    )
  }

  if (progress.isFixTooVague) {
    return (
      <Card className="mt-3 flex items-start gap-3 p-4">
        <AlertTriangle className="mt-0.5 size-4 shrink-0 text-accent-300" aria-hidden />
        <p className="text-sm text-sand-700">
          Your position is only accurate to about{' '}
          {Math.round(location.fix.accuracyMetres)} m, which is not enough to place you on a
          street. The dot shows roughly where you are.
        </p>
      </Card>
    )
  }

  if (progress.hasArrived) {
    return (
      <Card className="mt-3 border-brand-700/50 bg-brand-900/30 p-4">
        <p className="text-sm font-medium text-brand-200">
          You have reached the last stop. Enjoy your evening.
        </p>
      </Card>
    )
  }

  const nextTitle =
    progress.nextStopIndex === null ? null : (titles[progress.nextStopIndex] ?? 'the next stop')

  return (
    <Card className="mt-3 p-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <p className="text-sand-900">
          <span className="text-lg font-semibold">
            {progress.metresToNextStop === null
              ? '—'
              : formatDistance(progress.metresToNextStop)}
          </span>{' '}
          to {nextTitle}
        </p>
        {progress.minutesToNextStop !== null && (
          <p className="text-sm text-sand-600">about {progress.minutesToNextStop} min on foot</p>
        )}
      </div>

      {progress.metresRemaining !== null && (
        <p className="mt-1 text-xs text-sand-500">
          {formatDistance(progress.metresRemaining)} left on the whole route
        </p>
      )}

      {progress.isOffRoute && (
        <p className="mt-3 rounded-lg bg-accent-100 px-3 py-2 text-sm text-accent-300">
          You are about {formatDistance(progress.offRouteMetres ?? 0)} from the route. Head
          back to the line to carry on - Mado will not re-route you.
        </p>
      )}
    </Card>
  )
}
