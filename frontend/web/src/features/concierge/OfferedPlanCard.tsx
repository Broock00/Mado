/**
 * A plan the concierge offered, inside the conversation.
 *
 * Planning belongs here rather than behind a form. An explorer who says "I'm
 * free this evening" has already given the planner everything it needs, and
 * asking them to restate it as a window, a budget and a stop count is asking
 * them to do the work again in a worse notation.
 *
 * Nothing is stored until they say yes. Most plans are looked at once and a
 * different one asked for, so keeping every draft would fill their list with
 * evenings they rejected.
 */

import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Check, Clock, Lock, Navigation, PenLine, Route } from 'lucide-react'

import { api } from '@/lib/api'
import { useAppStore } from '@/app/store'
import type { OfferedPlan } from '@/lib/types'
import { Button } from '@/design-system/primitives'

function clockTime(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

function duration(minutes: number): string {
  if (minutes < 60) return `${minutes} min`
  const hours = Math.floor(minutes / 60)
  const rest = minutes % 60
  return rest === 0 ? `${hours} hr` : `${hours} hr ${rest} min`
}

export function OfferedPlanCard({
  plan,
  conversationId,
  onClose,
}: {
  plan: OfferedPlan
  conversationId: string
  onClose: () => void
}) {
  const queryClient = useQueryClient()
  const setActiveDraftId = useAppStore((s) => s.setActiveDraftId)
  const navigate = useNavigate()
  const [savedId, setSavedId] = useState<string | null>(null)

  const keep = useMutation({
    mutationFn: () => api.acceptPlan(conversationId),
    onSuccess: (itinerary) => {
      setSavedId(itinerary.id)
      void queryClient.invalidateQueries({ queryKey: ['itineraries'] })
    },
  })

  const openInPlanner = useMutation({
    mutationFn: () => api.openOfferAsDraft(conversationId),
    onSuccess: (draft) => {
      setActiveDraftId(draft.id)
      void queryClient.invalidateQueries({ queryKey: ['itinerary-drafts'] })
      onClose()
      navigate('/plans')
    },
  })

  return (
    <div className="rounded-xl border border-brand-700/50 bg-brand-900/25 p-3">
      <div className="flex items-center gap-2 text-xs font-medium text-brand-800">
        <Route className="size-3.5" aria-hidden />
        A plan for you
      </div>

      <ol className="mt-2 space-y-2">
        {plan.stops.map((stop, index) => {
          const day = stop.dayIndex ?? 0
          const prevDay = index > 0 ? (plan.stops[index - 1].dayIndex ?? 0) : day
          const showDayHeader = index === 0 ? day > 0 || plan.stops.some((s) => (s.dayIndex ?? 0) > 0) : day !== prevDay
          return (
          <li key={`${stop.experienceId}-${stop.arriveAt}`}>
            {showDayHeader && (
              <p className="pb-1 pt-1 text-[0.7rem] font-semibold uppercase tracking-wide text-brand-800">
                Day {day + 1}
              </p>
            )}
            {/* Travel is drawn between stops because that is what it is - the
                gap you have to cross, not a property of the destination. */}
            {index > 0 && day === prevDay && stop.travelMinutes > 0 && (
              <p className="flex items-center gap-1.5 py-1 pl-2 text-[0.7rem] text-sand-500">
                <Navigation className="size-3" aria-hidden />
                {duration(stop.travelMinutes)}
                {stop.travelKm != null && ` · ${stop.travelKm.toFixed(1)} km`}
              </p>
            )}
            <div className="flex gap-2.5">
              <span
                className={
                  stop.isFixedTime
                    ? 'mt-0.5 grid size-5 shrink-0 place-items-center rounded-full bg-brand-700 text-[0.65rem] font-semibold text-white'
                    : 'mt-0.5 grid size-5 shrink-0 place-items-center rounded-full border border-sand-300 bg-sand-100 text-[0.65rem] font-medium text-sand-700'
                }
              >
                {index + 1}
              </span>
              <div className="min-w-0">
                <Link
                  to={`/experiences/${stop.experienceId}`}
                  onClick={onClose}
                  className="text-sm font-medium text-sand-900 hover:underline"
                >
                  {stop.title}
                </Link>
                <p className="flex flex-wrap items-center gap-x-2 text-xs text-sand-600">
                  <span className="inline-flex items-center gap-1">
                    <Clock className="size-3" aria-hidden />
                    {clockTime(stop.arriveAt)} – {clockTime(stop.departAt)}
                  </span>
                  {stop.estimatedCost > 0 && <span>· {stop.estimatedCost.toFixed(0)} ETB</span>}
                  {stop.isFixedTime && (
                    <span className="inline-flex items-center gap-0.5 text-brand-700">
                      <Lock className="size-2.5" aria-hidden />
                      set time
                    </span>
                  )}
                </p>
              </div>
            </div>
          </li>
          )
        })}
      </ol>

      <p className="mt-2.5 border-t border-brand-200/70 pt-2 text-xs text-sand-600">
        {plan.stops.length} stops · {duration(plan.totalTravelMinutes)} travel ·{' '}
        {plan.totalCost === 0 ? 'nothing to pay' : `about ${plan.totalCost.toFixed(0)} ETB`}
      </p>

      {/* What the planner could not fit, said rather than hidden. */}
      {plan.unmet.length > 0 && (
        <p className="mt-1 text-xs text-sand-500">{plan.unmet[0]}</p>
      )}

      <div className="mt-2.5">
        {savedId ? (
          <p className="flex items-center gap-1.5 text-xs text-brand-800">
            <Check className="size-3.5" aria-hidden />
            Kept —{' '}
            <Link to={`/plans/${savedId}`} onClick={onClose} className="underline">
              open it
            </Link>
          </p>
        ) : (
          <div className="flex flex-wrap gap-2">
            {/* Primary: open in the builder so the explorer can edit before committing. */}
            <Button
              size="sm"
              onClick={() => openInPlanner.mutate()}
              loading={openInPlanner.isPending}
            >
              <PenLine className="size-3.5" aria-hidden />
              Open in planner
            </Button>
            {/* Secondary: one-tap keep for explorers who want exactly what was offered. */}
            <Button
              size="sm"
              variant="secondary"
              onClick={() => keep.mutate()}
              loading={keep.isPending}
            >
              Keep as-is
            </Button>
          </div>
        )}
        {(keep.isError || openInPlanner.isError) && (
          <p className="mt-1 text-xs text-red-300" role="alert">
            {((keep.error ?? openInPlanner.error) as Error).message}
          </p>
        )}
      </div>
    </div>
  )
}
