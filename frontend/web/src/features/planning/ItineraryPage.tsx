/**
 * One saved itinerary.
 *
 * Renders the stored plan rather than recomputing it. The timings were worked
 * out against the travel estimates and event schedules that existed when it was
 * built, and re-planning on every view would quietly hand back a different
 * evening from the one that was saved.
 */

import { Link, useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { ArrowLeft, CalendarX, CloudOff, WifiOff } from 'lucide-react'

import { api } from '@/lib/api'
import { useIsOnline } from '@/app/offline'
import { useLanguage } from '@/app/language-context'
import { Button, Card, EmptyState, Skeleton } from '@/design-system/primitives'
import { PlanSummary, PlanTimeline } from './PlanTimeline'
import { RouteGuidance } from './RouteGuidance'
import { clockTime } from './timeline-format'

export function ItineraryPage() {
  const { itineraryId } = useParams<{ itineraryId: string }>()
  const online = useIsOnline()
  const { t } = useLanguage()

  const { data: itinerary, isLoading, isError } = useQuery({
    queryKey: ['itinerary', itineraryId],
    queryFn: () => api.itinerary(itineraryId!),
    enabled: Boolean(itineraryId),
  })

  if (isLoading) {
    return (
      <div className="mx-auto w-full max-w-3xl px-4 py-8 sm:px-6">
        <Skeleton className="h-8 w-64" />
        <Skeleton className="mt-4 h-64 w-full" />
      </div>
    )
  }

  if (isError || !itinerary) {
    // Two very different causes, and telling somebody standing on a street with
    // no signal that their plan "may have been deleted" is both wrong and
    // alarming. Offline, the honest answer is that this one was never stored
    // here - and what to do about it next time.
    const unreachable = !online
    return (
      <div className="mx-auto max-w-3xl px-4 py-16">
        <EmptyState
          icon={unreachable ? <WifiOff className="size-8" /> : <CalendarX className="size-8" />}
          title={unreachable ? t('offline.notStored') : 'Itinerary not found'}
          description={
            unreachable
              ? t('offline.notStored.detail')
              : 'It may have been deleted, or it belongs to another account.'
          }
          action={
            <Link to="/plans">
              <Button>Back to planning</Button>
            </Link>
          }
        />
      </div>
    )
  }

  const day = new Date(itinerary.startsAt).toLocaleDateString([], {
    weekday: 'long',
    day: 'numeric',
    month: 'long',
  })

  return (
    <div className="mx-auto w-full max-w-3xl px-4 pb-24 pt-6 sm:px-6">
      <Link
        to="/plans"
        className="inline-flex items-center gap-1.5 text-sm text-sand-600 hover:text-sand-900"
      >
        <ArrowLeft className="size-4" aria-hidden />
        Planning
      </Link>

      <header className="mt-4">
        <h1 className="text-2xl font-semibold tracking-tight text-sand-900">
          {itinerary.title}
        </h1>
        <p className="mt-1 text-sand-600">
          {day}, {clockTime(itinerary.startsAt)} – {clockTime(itinerary.endsAt)}
        </p>
      </header>

      {/* Offline, this is by definition the stored copy - the request either
          came from the cache or failed. Said plainly, because a plan that looks
          live is how somebody turns up to a date that was called off this
          morning. */}
      {!online && (
        <p
          role="status"
          className="mt-4 rounded-lg bg-accent-100 px-3 py-2 text-sm text-accent-300"
        >
          <CloudOff className="mr-1.5 inline size-4 align-text-bottom" aria-hidden />
          {t('offline.savedCopy')}
        </p>
      )}

      <Card className="mt-6 p-5">
        <PlanSummary
          stopCount={itinerary.stops.length}
          totalTravelMinutes={itinerary.totalTravelMinutes}
          totalCost={itinerary.estimatedCost ?? 0}
          currency={itinerary.currency}
        />

        {itinerary.rationale && (
          <p className="mt-4 text-sm text-sand-600">{itinerary.rationale}</p>
        )}

        <div className="mt-6 border-t border-sand-200 pt-4">
          <PlanTimeline stops={itinerary.stops} />
        </div>
      </Card>

      <RouteGuidance itinerary={itinerary} />
    </div>
  )
}
