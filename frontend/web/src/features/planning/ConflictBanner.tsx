/**
 * Conflict and gap results from Check My Plan.
 *
 * Each conflict states the problem and offers explicit resolution options —
 * the explorer chooses what to do, Mado changes nothing automatically.
 * "Keep as-is" is always available: Mado identifies, not decides.
 */

import { AlertTriangle, CheckCircle, Clock, X } from 'lucide-react'

import type { PlanAnalysis, PlanGap } from '@/lib/types'
import { cn } from '@/lib/utils'
import { duration } from './timeline-format'

interface Props {
  analysis: PlanAnalysis
  onFillGap: (gap: PlanGap) => void
  onDismiss: () => void
}

const KIND_LABELS: Record<string, { label: string; color: string }> = {
  overlap: { label: 'Overlap', color: 'text-red-700 bg-red-50 border-red-200' },
  travel_gap: { label: 'Travel gap', color: 'text-orange-700 bg-orange-50 border-orange-200' },
  fixed_time_miss: { label: 'Fixed-time miss', color: 'text-red-700 bg-red-50 border-red-200' },
  window_overrun: { label: 'Window overrun', color: 'text-orange-700 bg-orange-50 border-orange-200' },
  budget_overrun: { label: 'Over budget', color: 'text-amber-700 bg-amber-50 border-amber-200' },
}

const RESOLUTION_LABELS: Record<string, string> = {
  move_stop: 'Move stop',
  change_duration: 'Change duration',
  remove_stop: 'Remove stop',
  keep_as_is: 'Keep as-is',
}

export function ConflictBanner({ analysis, onFillGap, onDismiss }: Props) {
  const { conflicts, gaps } = analysis
  const allClear = conflicts.length === 0

  return (
    <div className="rounded-xl border border-sand-200 bg-sand-100 p-4 shadow-card">
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-center gap-2">
          {allClear ? (
            <CheckCircle className="size-5 text-green-500" aria-hidden />
          ) : (
            <AlertTriangle className="size-5 text-red-500" aria-hidden />
          )}
          <p className="font-medium text-sand-900">
            {allClear
              ? 'No conflicts — plan looks good'
              : `${conflicts.length} conflict${conflicts.length > 1 ? 's' : ''} found`}
          </p>
        </div>
        <button type="button" onClick={onDismiss} className="text-sand-400 hover:text-sand-700">
          <X className="size-4" aria-hidden />
        </button>
      </div>

      {/* Conflicts */}
      {conflicts.length > 0 && (
        <ul className="mt-4 space-y-3">
          {conflicts.map((conflict, i) => {
            const meta = KIND_LABELS[conflict.kind] ?? { label: conflict.kind, color: 'text-sand-700 bg-sand-50 border-sand-200' }
            return (
              <li
                key={i}
                className={cn('rounded-lg border p-3 text-sm', meta.color)}
              >
                <div className="flex items-start gap-2">
                  <span className="shrink-0 rounded-full bg-white/80 px-2 py-0.5 text-[0.65rem] font-semibold">
                    {meta.label}
                  </span>
                  <p className="leading-snug">{conflict.message}</p>
                </div>
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {conflict.resolutions.map((res) => (
                    <span
                      key={res}
                      className="rounded-full border border-current/20 bg-white/50 px-2 py-0.5 text-[0.7rem] font-medium"
                    >
                      {RESOLUTION_LABELS[res] ?? res}
                    </span>
                  ))}
                </div>
              </li>
            )
          })}
        </ul>
      )}

      {/* Gaps */}
      {gaps.length > 0 && (
        <div className="mt-4">
          <p className="mb-2 text-sm font-medium text-sand-700">
            Free gaps — Mado can suggest something to fill them:
          </p>
          <ul className="space-y-2">
            {gaps.map((gap, i) => (
              <li
                key={i}
                className="flex items-center justify-between gap-3 rounded-lg border border-sand-200 bg-sand-50 px-3 py-2.5"
              >
                <span className="flex items-center gap-1.5 text-sm text-sand-700">
                  <Clock className="size-3.5 text-sand-400" aria-hidden />
                  {duration(gap.freeMinutes)} free
                  {gap.afterIndex === -1 ? ' before first stop' : ` after stop ${gap.afterIndex + 1}`}
                </span>
                <button
                  type="button"
                  onClick={() => onFillGap(gap)}
                  className="shrink-0 rounded-full bg-brand-700 px-2.5 py-1 text-xs font-medium text-white hover:bg-brand-800"
                >
                  Fill gap
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}
