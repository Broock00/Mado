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
import { ArrowLeft, CalendarX } from 'lucide-react'

import { api } from '@/lib/api'
import { Button, Card, EmptyState, Skeleton } from '@/design-system/primitives'
import { PlanSummary, PlanTimeline } from './PlanTimeline'
import { clockTime } from './timeline-format'

export function ItineraryPage() {
  const { itineraryId } = useParams<{ itineraryId: string }>()

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
    return (
      <div className="mx-auto max-w-3xl px-4 py-16">
        <EmptyState
          icon={<CalendarX className="size-8" />}
          title="Itinerary not found"
          description="It may have been deleted, or it belongs to another account."
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
    </div>
  )
}
