/**
 * A plan rendered as a timeline.
 *
 * The ordering and the timings were computed by the planner and are feasible as
 * given, so this component only displays them - it never re-sorts, re-times or
 * hides a stop. A plan the interface quietly rearranged would no longer be the
 * plan that was checked for feasibility.
 *
 * Travel is drawn *between* stops rather than as a field on them, because that
 * is what it is: the gap you have to cross. Showing "12 min travel" as a
 * property of the destination reads as though it were an attribute of the place.
 */

import { Link } from 'react-router-dom'
import { Clock, Navigation, Lock } from 'lucide-react'
import type { PlanStop } from '@/lib/types'
import { cn } from '@/lib/utils'
import { clockTime, duration } from './timeline-format'

function TravelLeg({ stop }: { stop: PlanStop }) {
  return (
    <div className="flex items-center gap-2 py-2 pl-[1.4rem] text-xs text-sand-500">
      <Navigation className="size-3.5 shrink-0" aria-hidden />
      <span>
        {duration(stop.travelMinutes)} travel
        {stop.travelKm != null && ` · ${stop.travelKm.toFixed(1)} km`}
      </span>
    </div>
  )
}

function Stop({
  stop,
  position,
  isLast,
}: {
  stop: PlanStop
  position: number
  isLast: boolean
}) {
  return (
    <li className="relative flex gap-4">
      {/* The rail and its node. Filled for a fixed-time stop so the anchors of
          the evening are visible at a glance. The connector is omitted on the
          last stop - a line trailing past the end suggests something follows. */}
      <div className="flex flex-col items-center">
        <span
          className={cn(
            'mt-1 grid size-7 shrink-0 place-items-center rounded-full border text-xs font-medium',
            stop.isFixedTime
              ? 'border-brand-600 bg-brand-600 text-white'
              : 'border-sand-300 bg-sand-100 text-sand-700',
          )}
        >
          {position}
        </span>
        {!isLast && <span className="mt-1 w-px flex-1 bg-sand-200" aria-hidden />}
      </div>

      <div className="min-w-0 flex-1 pb-6">
        <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <Link
            to={`/experiences/${stop.experienceId}`}
            className="font-medium text-sand-900 hover:underline"
          >
            {stop.title}
          </Link>
          {stop.isFixedTime && (
            <span className="inline-flex items-center gap-1 rounded-full bg-accent-100 px-2 py-0.5 text-[0.7rem] font-medium text-brand-700">
              <Lock className="size-3" aria-hidden />
              Set time
            </span>
          )}
        </div>

        <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-sand-600">
          <span className="inline-flex items-center gap-1">
            <Clock className="size-3.5" aria-hidden />
            <time dateTime={stop.arriveAt}>{clockTime(stop.arriveAt)}</time>
            {' – '}
            <time dateTime={stop.departAt}>{clockTime(stop.departAt)}</time>
          </span>
          <span className="text-sand-400" aria-hidden>
            ·
          </span>
          <span>{duration(stop.dwellMinutes)}</span>
          {stop.estimatedCost > 0 && (
            <>
              <span className="text-sand-400" aria-hidden>
                ·
              </span>
              <span>{stop.estimatedCost.toFixed(0)} ETB</span>
            </>
          )}
        </div>

        {stop.note && <p className="mt-1 text-sm text-sand-500">{stop.note}</p>}
      </div>
    </li>
  )
}

export function PlanTimeline({ stops }: { stops: PlanStop[] }) {
  if (stops.length === 0) return null

  return (
    <ol className="mt-2">
      {stops.map((stop, index) => (
        <div key={`${stop.experienceId}-${stop.arriveAt}`}>
          {/* The first stop's travel is from wherever the explorer already is,
              which we cannot describe usefully, so it is not drawn. */}
          {index > 0 && stop.travelMinutes > 0 && <TravelLeg stop={stop} />}
          <Stop stop={stop} position={index + 1} isLast={index === stops.length - 1} />
        </div>
      ))}
    </ol>
  )
}

export function PlanSummary({
  totalTravelMinutes,
  totalCost,
  currency,
  stopCount,
}: {
  totalTravelMinutes: number
  totalCost: number
  currency: string
  stopCount: number
}) {
  return (
    <dl className="flex flex-wrap gap-x-8 gap-y-2 text-sm">
      <div>
        <dt className="text-sand-500">Stops</dt>
        <dd className="font-medium text-sand-900">{stopCount}</dd>
      </div>
      <div>
        <dt className="text-sand-500">Travel</dt>
        <dd className="font-medium text-sand-900">{duration(totalTravelMinutes)}</dd>
      </div>
      <div>
        <dt className="text-sand-500">Estimated cost</dt>
        <dd className="font-medium text-sand-900">
          {totalCost === 0 ? 'Free' : `${totalCost.toFixed(0)} ${currency}`}
        </dd>
      </div>
    </dl>
  )
}
