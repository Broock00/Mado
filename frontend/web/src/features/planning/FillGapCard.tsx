/**
 * Fill This Gap proposals.
 *
 * Shows the stops Mado found for a free gap. The explorer picks one to insert —
 * nothing is added until they tap "Add". "Dismiss" closes without any change.
 */

import { Clock, MapPin, X } from 'lucide-react'

import type { PlanGap, PlanStop } from '@/lib/types'
import { useLanguage } from '@/app/language-context'
import { Skeleton } from '@/design-system/primitives'
import { clockTime, duration } from './timeline-format'

interface Props {
  gap: PlanGap
  loading: boolean
  proposals: PlanStop[] | null
  currency?: string
  onAccept: (experienceId: string) => void
  onDismiss: () => void
}

export function FillGapCard({ gap, loading, proposals, currency = 'ETB', onAccept, onDismiss }: Props) {
  const { money } = useLanguage()

  return (
    <div className="rounded-xl border border-sand-200 bg-sand-100 p-4 shadow-card">
      <div className="flex items-center justify-between gap-2">
        <p className="font-medium text-sand-900">
          Fill gap after stop {gap.afterIndex + 1}
          {' — '}
          <span className="font-normal text-sand-500">{duration(gap.freeMinutes)} free</span>
        </p>
        <button type="button" onClick={onDismiss} className="text-sand-400 hover:text-sand-700">
          <X className="size-4" aria-hidden />
        </button>
      </div>
      <p className="mt-0.5 text-sm text-sand-500">
        {clockTime(gap.startsAt)} – {clockTime(gap.endsAt)}
      </p>

      <div className="mt-4">
        {loading ? (
          <div className="space-y-2">
            {[0, 1, 2].map((i) => (
              <div key={i} className="flex gap-3">
                <Skeleton className="size-12 shrink-0 rounded-lg" />
                <div className="flex-1 space-y-1.5 py-1">
                  <Skeleton className="h-3.5 w-40" />
                  <Skeleton className="h-3 w-24" />
                </div>
              </div>
            ))}
          </div>
        ) : !proposals || proposals.length === 0 ? (
          <p className="py-4 text-center text-sm text-sand-400">
            Nothing fitted that gap — try a different window or add a stop manually.
          </p>
        ) : (
          <ul className="space-y-2">
            {proposals.map((stop) => (
              <li
                key={stop.experienceId}
                className="flex items-center gap-3 rounded-lg border border-sand-200 bg-sand-50 p-2"
              >
                <div className="grid size-12 shrink-0 place-items-center rounded-lg bg-sand-200 text-sand-400">
                  <MapPin className="size-5" aria-hidden />
                </div>
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm font-medium text-sand-900">{stop.title}</p>
                  <p className="mt-0.5 flex items-center gap-1.5 text-xs text-sand-500">
                    <Clock className="size-3" aria-hidden />
                    {clockTime(stop.arriveAt)} for {duration(stop.dwellMinutes)}
                    {stop.estimatedCost > 0 && (
                      <span>{money(stop.estimatedCost, currency)}</span>
                    )}
                  </p>
                </div>
                <button
                  type="button"
                  onClick={() => onAccept(stop.experienceId)}
                  className="shrink-0 rounded-full bg-brand-700 px-2.5 py-1 text-xs font-medium text-white hover:bg-brand-800"
                >
                  Add
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  )
}
