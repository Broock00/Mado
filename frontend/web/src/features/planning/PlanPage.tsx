/**
 * The Journey Planner (spec 10.01.04).
 *
 * Asks for the few things the planner genuinely needs - a window, a budget, how
 * many stops - and nothing else. The evening itself sits beside those controls
 * so the page is a workspace, not a form stacked on a list stacked on another
 * list.
 *
 * A plan is computed but not saved. Most are looked at once and discarded, so
 * saving is a deliberate second step rather than a side effect of asking.
 */

import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  CalendarDays,
  Check,
  Moon,
  Sparkles,
  Sun,
  Trash2,
  TriangleAlert,
} from 'lucide-react'

import { api } from '@/lib/api'
import { isLocationReady, useAppStore } from '@/app/store'
import { useLocationContext, useRequestLocation } from '@/app/hooks'
import { useLanguage } from '@/app/language-context'
import type { Itinerary, Plan, PlanRequestInput } from '@/lib/types'
import { Button, Card, EmptyState, Input, Skeleton } from '@/design-system/primitives'
import { PlaceFilter } from '@/features/discover/PlaceFilter'
import { cn } from '@/lib/utils'
import { PlanSummary, PlanTimeline } from './PlanTimeline'
import { clockTime, dayLabel } from './timeline-format'

const WINDOWS = [
  { key: 'tonight', label: 'Tonight', Icon: Moon },
  { key: 'tomorrow', label: 'Tomorrow', Icon: Sun },
  { key: 'weekend', label: 'This weekend', Icon: CalendarDays },
] as const

type WindowKey = (typeof WINDOWS)[number]['key']

/**
 * Resolve a preset to an absolute window in the browser's own timezone.
 *
 * Sent as ISO strings *with an offset* because the API rejects naive timestamps:
 * a plan is a sequence of wall-clock promises, and letting the server guess a
 * zone produces an itinerary that is silently hours wrong.
 */
function resolveWindow(key: WindowKey): { startsAt: string; endsAt: string } {
  const now = new Date()
  const start = new Date(now)
  const end = new Date(now)

  if (key === 'tonight') {
    if (now.getHours() < 17) start.setHours(17, 0, 0, 0)
    end.setHours(23, 30, 0, 0)
    if (end <= start) end.setDate(end.getDate() + 1)
  } else if (key === 'tomorrow') {
    start.setDate(start.getDate() + 1)
    start.setHours(10, 0, 0, 0)
    end.setDate(end.getDate() + 1)
    end.setHours(22, 0, 0, 0)
  } else {
    const daysUntilSaturday = (6 - now.getDay() + 7) % 7
    start.setDate(start.getDate() + daysUntilSaturday)
    start.setHours(10, 0, 0, 0)
    end.setDate(end.getDate() + daysUntilSaturday)
    end.setHours(22, 0, 0, 0)
  }

  return { startsAt: start.toISOString(), endsAt: end.toISOString() }
}

function windowHint(key: WindowKey): string {
  const { startsAt, endsAt } = resolveWindow(key)
  const start = new Date(startsAt)
  const end = new Date(endsAt)
  const times = `${clockTime(start)} – ${clockTime(end)}`
  if (key !== 'weekend') return times
  const day = start.toLocaleDateString([], { weekday: 'short' })
  return `${day} ${times}`
}

function windowLabel(plan: Plan): string | null {
  if (plan.stops.length === 0) return null
  const first = plan.stops[0]
  const last = plan.stops[plan.stops.length - 1]
  return `${dayLabel(first.arriveAt)}, ${clockTime(first.arriveAt)} – ${clockTime(last.departAt)}`
}

function placeName(slug: string | null | undefined): string | null {
  if (!slug) return null
  return slug.replace(/-/g, ' ')
}

export function PlanPage() {
  const city = useAppStore((s) => s.citySlug)
  const place = useAppStore((s) => s.place)
  const location = useAppStore((s) => s.location)
  const user = useAppStore((s) => s.user)
  const openConcierge = useAppStore((s) => s.toggleConcierge)
  const queryClient = useQueryClient()
  const requestLocation = useRequestLocation()
  const { data: context } = useLocationContext()
  const { money } = useLanguage()

  const [windowKey, setWindowKey] = useState<WindowKey>('tonight')
  const [maxStops, setMaxStops] = useState(3)
  const [budget, setBudget] = useState('')
  const [freeOnly, setFreeOnly] = useState(false)
  const [plan, setPlan] = useState<Plan | null>(null)
  const [title, setTitle] = useState('')
  const [saved, setSaved] = useState<string | null>(null)

  const ready = isLocationReady(location)
  const hasSomewhere = Boolean(place) || ready
  const budgetCurrency = plan?.currency ?? context?.place?.currency ?? null
  const origin = place
    ? { latitude: place.latitude, longitude: place.longitude }
    : ready && location.latitude != null && location.longitude != null
      ? { latitude: location.latitude, longitude: location.longitude }
      : null

  const currentInput = (): PlanRequestInput => ({
    ...resolveWindow(windowKey),
    city: place ? undefined : (city ?? undefined),
    latitude: origin?.latitude ?? null,
    longitude: origin?.longitude ?? null,
    budget: freeOnly || budget === '' ? null : Number(budget),
    maxStops,
    freeOnly,
  })

  const build = useMutation({
    mutationFn: () => api.plan(currentInput()),
    onSuccess: (result) => {
      setPlan(result)
      setSaved(null)
      const label = WINDOWS.find((w) => w.key === windowKey)?.label
      const where = placeName(result.citySlug ?? city) ?? place?.label ?? null
      setTitle(
        result.stops.length > 0
          ? [label, where && `in ${where}`].filter(Boolean).join(' ')
          : '',
      )
    },
  })

  const save = useMutation({
    mutationFn: () => api.saveItinerary({ ...currentInput(), title: title.trim() }),
    onSuccess: (itinerary) => {
      setSaved(itinerary.id)
      void queryClient.invalidateQueries({ queryKey: ['itineraries'] })
    },
  })

  const { data: itineraries } = useQuery({
    queryKey: ['itineraries'],
    queryFn: () => api.itineraries(),
    enabled: Boolean(user),
  })

  const remove = useMutation({
    mutationFn: (id: string) => api.deleteItinerary(id),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['itineraries'] }),
  })

  const where =
    place?.label ??
    (context?.resolved ? context.place?.area || context.place?.label : null) ??
    placeName(city)

  const filled = Boolean(plan && plan.stops.length > 0)
  const emptyResult = Boolean(plan && plan.stops.length === 0 && !build.isPending)

  return (
    <div className="mx-auto w-full max-w-6xl px-4 pb-28 pt-6 sm:px-6 lg:px-8">
      <header className="mb-6 flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <h1 className="text-2xl font-semibold tracking-tight text-sand-900 sm:text-3xl">
            Plan an evening
          </h1>
          <p className="mt-1 max-w-xl text-sm text-sand-500">
            {where
              ? `Around ${where}. Say when you are free — Mado works out the order, the travel, and anything with a set time.`
              : 'Say when you are free. Mado works out the order, the travel, and anything with a set time.'}
          </p>
        </div>
        <PlaceFilter />
      </header>

      <div className="grid items-start gap-8 lg:grid-cols-[20rem_minmax(0,1fr)]">
        <aside className="space-y-6 lg:sticky lg:top-20">
          <Card className="p-4 sm:p-5">
            <fieldset>
              <legend className="text-sm font-medium text-sand-800">When</legend>
              <div className="mt-2 grid grid-cols-3 gap-1.5">
                {WINDOWS.map((option) => {
                  const selected = windowKey === option.key
                  return (
                    <button
                      key={option.key}
                      type="button"
                      onClick={() => setWindowKey(option.key)}
                      aria-pressed={selected}
                      className={cn(
                        'rounded-lg border px-2 py-2 text-left transition-colors',
                        selected
                          ? 'border-brand-600 bg-brand-700/15'
                          : 'border-sand-300 hover:bg-sand-50',
                      )}
                    >
                      <option.Icon
                        className={cn('size-3.5', selected ? 'text-brand-600' : 'text-sand-500')}
                        aria-hidden
                      />
                      <span className="mt-1 block text-xs font-medium text-sand-900">
                        {option.label}
                      </span>
                      <span className="mt-0.5 block text-[0.65rem] leading-tight text-sand-500">
                        {windowHint(option.key)}
                      </span>
                    </button>
                  )
                })}
              </div>
            </fieldset>

            <fieldset className="mt-5">
              <legend className="text-sm font-medium text-sand-800">Stops</legend>
              <div className="mt-2 flex gap-1.5">
                {[2, 3, 4, 5].map((count) => (
                  <button
                    key={count}
                    type="button"
                    onClick={() => setMaxStops(count)}
                    aria-pressed={maxStops === count}
                    className={cn(
                      'h-9 flex-1 rounded-lg text-sm font-medium transition-colors',
                      maxStops === count
                        ? 'bg-brand-700 text-white'
                        : 'border border-sand-300 text-sand-700 hover:bg-sand-200',
                    )}
                  >
                    {count}
                  </button>
                ))}
              </div>
            </fieldset>

            <div className="mt-5">
              <label htmlFor="budget" className="text-sm font-medium text-sand-800">
                Budget{budgetCurrency ? ` (${budgetCurrency})` : ''}
                <span className="font-normal text-sand-500">, optional</span>
              </label>
              <Input
                id="budget"
                inputMode="numeric"
                placeholder={freeOnly ? 'Free only' : 'Any'}
                value={budget}
                disabled={freeOnly}
                onChange={(event) => setBudget(event.target.value.replace(/[^0-9]/g, ''))}
                className="mt-2"
              />
              <label className="mt-2 flex items-center gap-2 text-sm text-sand-600">
                <input
                  type="checkbox"
                  checked={freeOnly}
                  onChange={(event) => setFreeOnly(event.target.checked)}
                  className="size-4 rounded border-sand-300"
                />
                Only free things
              </label>
            </div>

            {!hasSomewhere && (
              <div className="mt-4 rounded-lg border border-sand-200 bg-sand-50 px-3 py-2.5">
                <p className="text-sm text-sand-600">
                  Choose a place, or share your location, so there is somewhere to start.
                </p>
                {location.status !== 'denied' && (
                  <Button size="sm" variant="secondary" className="mt-2" onClick={requestLocation}>
                    Use my location
                  </Button>
                )}
              </div>
            )}

            <Button
              className="mt-5 w-full"
              onClick={() => build.mutate()}
              loading={build.isPending}
            >
              <Sparkles className="size-4" aria-hidden />
              {build.isPending ? 'Working it out…' : filled ? 'Plan another' : 'Plan this evening'}
            </Button>
            <button
              type="button"
              onClick={() => openConcierge(true)}
              className="mt-2 w-full text-center text-sm text-sand-500 hover:text-sand-800"
            >
              Or ask Mado in your own words
            </button>

            {build.isError && (
              <p className="mt-3 text-sm text-red-300" role="alert">
                {(build.error as Error).message}
              </p>
            )}
          </Card>

          {user && itineraries && itineraries.length > 0 && (
            <KeptList
              itineraries={itineraries}
              money={money}
              removing={remove.isPending}
              onRemove={(id) => remove.mutate(id)}
            />
          )}
        </aside>

        <section>
          {build.isPending && <BuildingSkeleton />}

          {!build.isPending && !plan && (
            <EmptyState
              icon={<Sparkles className="size-8" />}
              title="Nothing planned yet"
              description="Pick a window on the left and Mado will lay out an evening that fits."
            />
          )}

          {emptyResult && (
            <EmptyState
              icon={<CalendarDays className="size-8" />}
              title="Nothing fitted that window"
              description={plan?.rationale}
            />
          )}

          {!build.isPending && filled && plan && (
            <div className="motion-safe:animate-[planRise_420ms_var(--ease-out-soft)_both]">
              <div className="flex flex-wrap items-end justify-between gap-3">
                <div className="min-w-0">
                  <p className="text-xs font-medium uppercase tracking-wide text-brand-600">
                    Your evening
                  </p>
                  <h2 className="mt-1 text-lg font-semibold tracking-tight text-sand-900">
                    {windowLabel(plan)}
                  </h2>
                </div>
                <PlanSummary
                  stopCount={plan.stops.length}
                  totalTravelMinutes={plan.totalTravelMinutes}
                  totalCost={plan.totalCost}
                  currency={plan.currency}
                />
              </div>

              <p className="mt-3 text-sm leading-relaxed text-sand-600">{plan.rationale}</p>

              {plan.unmet.length > 0 && (
                <ul className="mt-3 space-y-1">
                  {plan.unmet.map((reason) => (
                    <li key={reason} className="flex items-start gap-2 text-sm text-sand-500">
                      <TriangleAlert className="mt-0.5 size-3.5 shrink-0" aria-hidden />
                      {reason}
                    </li>
                  ))}
                </ul>
              )}

              <div className="mt-5">
                <PlanTimeline stops={plan.stops} currency={plan.currency} />
              </div>

              <div className="mt-5 flex flex-wrap items-center gap-3 border-t border-sand-200 pt-4">
                {user ? (
                  saved ? (
                    <p className="flex items-center gap-1.5 text-sm text-brand-600">
                      <Check className="size-4" aria-hidden />
                      Kept —{' '}
                      <Link to={`/plans/${saved}`} className="underline">
                        open it
                      </Link>
                    </p>
                  ) : (
                    <>
                      <Input
                        aria-label="Name this itinerary"
                        value={title}
                        onChange={(event) => setTitle(event.target.value)}
                        placeholder="Name this plan"
                        className="max-w-xs"
                      />
                      <Button
                        variant="secondary"
                        onClick={() => save.mutate()}
                        disabled={save.isPending || title.trim() === ''}
                      >
                        {save.isPending ? 'Saving…' : 'Keep this plan'}
                      </Button>
                    </>
                  )
                ) : (
                  <p className="text-sm text-sand-600">
                    <Link to="/signin" className="underline">
                      Sign in
                    </Link>{' '}
                    to keep this plan.
                  </p>
                )}
              </div>
            </div>
          )}
        </section>
      </div>
    </div>
  )
}

function BuildingSkeleton() {
  return (
    <div aria-busy aria-live="polite">
      <p className="text-sm font-medium text-sand-800">Fitting stops around the window…</p>
      <p className="mt-1 text-sm text-sand-500">Checking times, travel, and anything with a set hour.</p>
      <div className="mt-6 space-y-3">
        {[0, 1, 2].map((row) => (
          <div key={row} className="flex gap-3">
            <Skeleton className="h-4 w-12 shrink-0" />
            <Skeleton className="h-20 flex-1 rounded-xl" />
          </div>
        ))}
      </div>
    </div>
  )
}

function KeptList({
  itineraries,
  money,
  removing,
  onRemove,
}: {
  itineraries: Itinerary[]
  money: (amount: number, currency: string) => string
  removing: boolean
  onRemove: (id: string) => void
}) {
  return (
    <section>
      <h2 className="text-sm font-medium text-sand-800">Kept plans</h2>
      <ul className="mt-2 space-y-1">
        {itineraries.map((itinerary) => {
          const cost =
            itinerary.estimatedCost != null && itinerary.estimatedCost > 0
              ? money(itinerary.estimatedCost, itinerary.currency)
              : null
          return (
            <li key={itinerary.id}>
              <div className="group relative flex items-start gap-2 rounded-lg px-2 py-2 hover:bg-sand-100">
                <Link to={`/plans/${itinerary.id}`} className="min-w-0 flex-1 after:absolute after:inset-0">
                  <p className="truncate text-sm font-medium text-sand-900">{itinerary.title}</p>
                  <p className="mt-0.5 text-xs text-sand-500">
                    {new Date(itinerary.startsAt).toLocaleDateString([], {
                      weekday: 'short',
                      day: 'numeric',
                      month: 'short',
                    })}
                    {' · '}
                    {clockTime(itinerary.startsAt)}
                    {' · '}
                    {itinerary.stops.length} {itinerary.stops.length === 1 ? 'stop' : 'stops'}
                    {cost && ` · ${cost}`}
                  </p>
                </Link>
                <button
                  type="button"
                  aria-label={`Delete ${itinerary.title}`}
                  onClick={() => onRemove(itinerary.id)}
                  disabled={removing}
                  className="relative z-10 rounded-md p-1 text-sand-500 hover:bg-sand-200 hover:text-sand-800 lg:opacity-0 lg:group-hover:opacity-100 disabled:opacity-50"
                >
                  <Trash2 className="size-3.5" aria-hidden />
                </button>
              </div>
            </li>
          )
        })}
      </ul>
    </section>
  )
}
