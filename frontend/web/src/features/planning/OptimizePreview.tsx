/**
 * Optimize Plan proposal.
 *
 * Shows the proposed reordering alongside the current order. The explorer
 * explicitly accepts or rejects. Nothing is applied until they confirm.
 *
 * "Keep my order" is the primary escape: Mado proposes, the explorer decides.
 */

import { Check, X } from 'lucide-react'

import type { Plan, PlanStop } from '@/lib/types'
import { Button } from '@/design-system/primitives'
import { clockTime, duration } from './timeline-format'

interface Props {
  current: PlanStop[]
  proposal: Plan
  onAccept: () => void
  onReject: () => void
}

export function OptimizePreview({ current, proposal, onAccept, onReject }: Props) {
  const proposedStops = proposal.stops
  const reordered = proposedStops.length > 0 && proposedStops.some(
    (s, i) => s.experienceId !== (current[i]?.experienceId ?? '')
  )
  const savedTravel = current.reduce((sum, s) => sum + s.travelMinutes, 0) - proposal.totalTravelMinutes

  return (
    <div className="rounded-xl border border-brand-200 bg-brand-50 p-4 shadow-sm">
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="font-medium text-brand-900">Optimize proposal</p>
          {reordered ? (
            <p className="mt-0.5 text-sm text-brand-700">
              {savedTravel > 0
                ? `Save about ${duration(savedTravel)} of travel time with this order:`
                : 'Mado found a slightly more efficient sequence:'}
            </p>
          ) : (
            <p className="mt-0.5 text-sm text-brand-700">
              Your current order is already as efficient as possible.
            </p>
          )}
        </div>
        <button type="button" onClick={onReject} className="text-brand-400 hover:text-brand-700">
          <X className="size-4" aria-hidden />
        </button>
      </div>

      {reordered && (
        <>
          <ol className="mt-3 space-y-1">
            {proposedStops.map((stop, i) => {
              const currentIndex = current.findIndex((s) => s.experienceId === stop.experienceId)
              const moved = currentIndex !== i
              return (
                <li
                  key={stop.experienceId}
                  className="flex items-center gap-2 text-sm"
                >
                  <span className="grid size-5 shrink-0 place-items-center rounded-full bg-brand-100 text-[0.65rem] font-semibold text-brand-700">
                    {i + 1}
                  </span>
                  <span className={moved ? 'text-brand-800 font-medium' : 'text-brand-700'}>
                    {stop.title}
                  </span>
                  {moved && currentIndex !== -1 && (
                    <span className="ml-auto text-[0.65rem] text-brand-500">
                      was #{currentIndex + 1}
                    </span>
                  )}
                  <span className="ml-1 text-xs text-brand-500">{clockTime(stop.arriveAt)}</span>
                </li>
              )
            })}
          </ol>

          <div className="mt-4 flex items-center gap-2">
            <Button size="sm" onClick={onAccept}>
              <Check className="size-3.5" aria-hidden />
              Apply this order
            </Button>
            <Button size="sm" variant="secondary" onClick={onReject}>
              Keep my order
            </Button>
          </div>
        </>
      )}

      {!reordered && (
        <Button size="sm" variant="secondary" className="mt-3" onClick={onReject}>
          Got it
        </Button>
      )}
    </div>
  )
}
