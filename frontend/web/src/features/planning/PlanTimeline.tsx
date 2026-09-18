/**
 * A plan rendered as a timeline.
 *
 * Time runs down the page because that is how an evening is read - first, then
 * next, then last, all visible at once. A sideways carousel hid the sequence
 * and made travel between stops unreadable.
 *
 * Photos come from the listing when they load. Times never do: those were
 * solved by the planner and must stay the ones that were checked.
 *
 * Travel is drawn *between* stops rather than as a field on them, because that
 * is what it is: the gap you have to cross.
 */

import { Link } from 'react-router-dom'
import { Clock, Lock, MapPin, Navigation } from 'lucide-react'

import { useLanguage } from '@/app/language-context'
import type { PlanStop } from '@/lib/types'
import { cn } from '@/lib/utils'
import { clockTime, duration } from './timeline-format'
import { useStopDetails } from './useStopDetails'

export function PlanTimeline({
  stops,
  currency,
}: {
  stops: PlanStop[]
  currency: string
}) {
  const details = useStopDetails(stops)
  if (stops.length === 0) return null

  return (
    <ol className="mt-1">
      {stops.map((stop, index) => {
        const detail = details.get(stop.experienceId)
        return (
          <li
            key={`${stop.experienceId}-${stop.arriveAt}`}
            className="motion-safe:animate-[planRise_420ms_var(--ease-out-soft)_both]"
            style={{ animationDelay: `${index * 60}ms` }}
          >
            {index > 0 && stop.travelMinutes > 0 && <TravelLeg stop={stop} />}
            <StopRow
              stop={stop}
              position={index + 1}
              isLast={index === stops.length - 1}
              currency={currency}
              imageUrl={detail?.media[0]?.url}
              imageAlt={detail?.media[0]?.altText}
              where={detail?.venue?.neighborhood?.name || detail?.venue?.name}
            />
          </li>
        )
      })}
    </ol>
  )
}

function TravelLeg({ stop }: { stop: PlanStop }) {
  return (
    <div className="grid grid-cols-[3.5rem_1.5rem_minmax(0,1fr)] gap-x-3 py-1">
      <span aria-hidden />
      <div className="flex justify-center" aria-hidden>
        <span className="w-px border-l border-dashed border-sand-400" />
      </div>
      <p className="flex items-center gap-1.5 py-1.5 text-xs text-sand-500">
        <Navigation className="size-3.5 shrink-0" aria-hidden />
        {duration(stop.travelMinutes)} travel
        {stop.travelKm != null && ` · ${stop.travelKm.toFixed(1)} km`}
      </p>
    </div>
  )
}

function StopRow({
  stop,
  position,
  isLast,
  currency,
  imageUrl,
  imageAlt,
  where,
}: {
  stop: PlanStop
  position: number
  isLast: boolean
  currency: string
  imageUrl?: string | null
  imageAlt?: string | null
  where?: string | null
}) {
  const { money } = useLanguage()

  return (
    <div className="grid grid-cols-[3.5rem_1.5rem_minmax(0,1fr)] gap-x-3">
      <time
        dateTime={stop.arriveAt}
        className="pt-3 text-right text-sm font-semibold tabular-nums text-sand-800"
      >
        {clockTime(stop.arriveAt)}
      </time>

      <div className="flex flex-col items-center">
        <span
          className={cn(
            'mt-3 grid size-6 shrink-0 place-items-center rounded-full text-[0.7rem] font-semibold',
            stop.isFixedTime
              ? 'bg-brand-600 text-white'
              : 'border border-sand-300 bg-sand-100 text-sand-700',
          )}
        >
          {position}
        </span>
        {!isLast && <span className="mt-1 w-px flex-1 bg-sand-300" aria-hidden />}
      </div>

      <article
        className={cn(
          'flex gap-3 rounded-xl border border-sand-200 bg-sand-100 p-2.5 transition-colors hover:border-sand-300',
          isLast ? 'mb-0' : 'mb-1',
        )}
      >
        <Link
          to={`/experiences/${stop.experienceId}`}
          className="relative size-16 shrink-0 overflow-hidden rounded-lg bg-sand-200 sm:size-[4.5rem]"
        >
          {imageUrl ? (
            <img src={imageUrl} alt={imageAlt ?? ''} className="size-full object-cover" />
          ) : (
            <span className="grid size-full place-items-center text-sand-500">
              <MapPin className="size-5" aria-hidden />
            </span>
          )}
        </Link>

        <div className="min-w-0 flex-1 py-0.5">
          <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
            <Link
              to={`/experiences/${stop.experienceId}`}
              className="font-medium text-sand-900 hover:underline"
            >
              {stop.title}
            </Link>
            {stop.isFixedTime && (
              <span className="inline-flex items-center gap-1 rounded-full bg-accent-100 px-2 py-0.5 text-[0.65rem] font-medium text-brand-700">
                <Lock className="size-3" aria-hidden />
                Set time
              </span>
            )}
          </div>
          <p className="mt-0.5 flex flex-wrap items-center gap-x-2 text-sm text-sand-600">
            <span className="inline-flex items-center gap-1">
              <Clock className="size-3.5" aria-hidden />
              until {clockTime(stop.departAt)}
            </span>
            <span aria-hidden className="text-sand-400">
              ·
            </span>
            <span>{duration(stop.dwellMinutes)}</span>
            {stop.estimatedCost > 0 && (
              <>
                <span aria-hidden className="text-sand-400">
                  ·
                </span>
                <span>{money(stop.estimatedCost, currency)}</span>
              </>
            )}
          </p>
          {where && <p className="mt-0.5 truncate text-xs text-sand-500">{where}</p>}
          {stop.note && <p className="mt-1 text-sm text-sand-500">{stop.note}</p>}
        </div>
      </article>
    </div>
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
  const { money } = useLanguage()

  return (
    <p className="text-sm text-sand-600">
      <span className="font-medium text-sand-900">{stopCount}</span>
      {stopCount === 1 ? ' stop' : ' stops'}
      {' · '}
      {duration(totalTravelMinutes)} travel
      {' · '}
      {totalCost === 0 ? 'Free' : money(totalCost, currency)}
    </p>
  )
}
