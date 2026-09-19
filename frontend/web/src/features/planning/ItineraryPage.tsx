/**
 * One saved itinerary.
 *
 * Renders the stored plan rather than recomputing it. The timings were worked
 * out against the travel estimates and event schedules that existed when it was
 * built, and re-planning on every view would quietly hand back a different
 * evening from the one that was saved.
 */

import { Link, useNavigate, useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { ArrowLeft, CalendarX, CloudOff, Pencil, WifiOff } from 'lucide-react'

import { api } from '@/lib/api'
import { useAppStore } from '@/app/store'
import { useIsOnline } from '@/app/offline'
import { useLanguage } from '@/app/language-context'
import { Button, EmptyState, Skeleton } from '@/design-system/primitives'
import { PlanSummary, PlanTimeline } from './PlanTimeline'
import { RouteGuidance } from './RouteGuidance'
import { clockTime, dayLabel } from './timeline-format'

export function ItineraryPage() {
  const { itineraryId } = useParams<{ itineraryId: string }>()
  const online = useIsOnline()
  const { t } = useLanguage()
  const navigate = useNavigate()
  const setActiveDraftId = useAppStore((s) => s.setActiveDraftId)

  const { data: itinerary, isLoading, isError } = useQuery({
    queryKey: ['itinerary', itineraryId],
    queryFn: () => api.itinerary(itineraryId!),
    enabled: Boolean(itineraryId),
  })

  if (isLoading) {
    return (
      <div className="mx-auto w-full max-w-3xl px-4 pb-24 pt-8 sm:px-6">
        <Skeleton className="h-4 w-24" />
        <Skeleton className="mt-4 h-8 w-64" />
        <Skeleton className="mt-6 h-64 w-full rounded-xl" />
      </div>
    )
  }

  if (isError || !itinerary) {
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

  const day = dayLabel(itinerary.startsAt)
  const underway =
    Date.parse(itinerary.startsAt) <= Date.now() && Date.parse(itinerary.endsAt) >= Date.now()
  const planId = itinerary.id

  function editInBuilder() {
    setActiveDraftId(planId)
    navigate('/plans')
  }

  return (
    <div className="mx-auto w-full max-w-3xl px-4 pb-24 pt-6 sm:px-6">
      <Link
        to="/plans"
        className="inline-flex items-center gap-1.5 text-sm text-sand-600 hover:text-sand-900"
      >
        <ArrowLeft className="size-4" aria-hidden />
        Planning
      </Link>

      <header className="mt-4 flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-xs font-medium uppercase tracking-wide text-brand-600">
            {underway ? 'On now' : day}
          </p>
          <h1 className="mt-1 text-2xl font-semibold tracking-tight text-sand-900">
            {itinerary.title}
          </h1>
          <p className="mt-1 text-sand-600">
            {underway ? `${day}, ` : null}
            {clockTime(itinerary.startsAt)} – {clockTime(itinerary.endsAt)}
          </p>
          <div className="mt-3">
            <PlanSummary
              stopCount={itinerary.stops.length}
              totalTravelMinutes={itinerary.totalTravelMinutes}
              totalCost={itinerary.estimatedCost ?? 0}
              currency={itinerary.currency}
            />
          </div>
        </div>
        <Button variant="secondary" onClick={editInBuilder}>
          <Pencil className="size-4" aria-hidden />
          Edit plan
        </Button>
      </header>

      {!online && (
        <p
          role="status"
          className="mt-4 rounded-lg bg-accent-100 px-3 py-2 text-sm text-accent-300"
        >
          <CloudOff className="mr-1.5 inline size-4 align-text-bottom" aria-hidden />
          {t('offline.savedCopy')}
        </p>
      )}

      {itinerary.rationale && (
        <p className="mt-4 text-sm leading-relaxed text-sand-600">{itinerary.rationale}</p>
      )}

      <div className="mt-6">
        <PlanTimeline stops={itinerary.stops} currency={itinerary.currency} />
      </div>

      <RouteGuidance itinerary={itinerary} />
    </div>
  )
}
