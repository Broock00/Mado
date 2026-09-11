/**
 * The Journey Planner (spec 10.01.04).
 *
 * Asks for the few things the planner genuinely needs - a window, a budget, how
 * many stops - and nothing else. Every additional field is one more thing an
 * explorer has to decide before they get any value, and the planner already
 * infers the rest from their location and what it knows about them.
 *
 * A plan is computed but not saved. Most are looked at once and discarded, so
 * saving is a deliberate second step rather than a side effect of asking.
 */

import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { CalendarClock, Route, Sparkles, Trash2, TriangleAlert } from 'lucide-react'

import { api } from '@/lib/api'
import { isLocationReady, useAppStore } from '@/app/store'
import type { Plan, PlanRequestInput } from '@/lib/types'
import { Button, Card, EmptyState, Input, SectionHeading } from '@/design-system/primitives'
import { PlanSummary, PlanTimeline } from './PlanTimeline'
import { clockTime } from './timeline-format'

/** Presets covering how people actually describe an outing. */
const WINDOWS = [
  { key: 'tonight', label: 'Tonight' },
  { key: 'tomorrow', label: 'Tomorrow' },
  { key: 'weekend', label: 'This weekend' },
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
    // Already out? Start now. Otherwise the evening starts at five.
    if (now.getHours() < 17) start.setHours(17, 0, 0, 0)
    end.setHours(23, 30, 0, 0)
    // An evening that has already run past 23:30 rolls into the small hours.
    if (end <= start) end.setDate(end.getDate() + 1)
  } else if (key === 'tomorrow') {
    start.setDate(start.getDate() + 1)
    start.setHours(10, 0, 0, 0)
    end.setDate(end.getDate() + 1)
    end.setHours(22, 0, 0, 0)
  } else {
    // Saturday, or today if it is already the weekend.
    const daysUntilSaturday = (6 - now.getDay() + 7) % 7
    start.setDate(start.getDate() + daysUntilSaturday)
    start.setHours(10, 0, 0, 0)
    end.setDate(end.getDate() + daysUntilSaturday)
    end.setHours(22, 0, 0, 0)
  }

  return { startsAt: start.toISOString(), endsAt: end.toISOString() }
}

function windowLabel(plan: Plan): string | null {
  if (plan.stops.length === 0) return null
  const first = plan.stops[0]
  const last = plan.stops[plan.stops.length - 1]
  const day = new Date(first.arriveAt).toLocaleDateString([], {
    weekday: 'long',
    day: 'numeric',
    month: 'long',
  })
  return `${day}, ${clockTime(first.arriveAt)} – ${clockTime(last.departAt)}`
}

export function PlanPage() {
  const city = useAppStore((s) => s.citySlug)
  const location = useAppStore((s) => s.location)
  const user = useAppStore((s) => s.user)
  const openConcierge = useAppStore((s) => s.toggleConcierge)
  const queryClient = useQueryClient()

  const [windowKey, setWindowKey] = useState<WindowKey>('tonight')
  const [maxStops, setMaxStops] = useState(3)
  const [budget, setBudget] = useState('')
  const [freeOnly, setFreeOnly] = useState(false)
  const [plan, setPlan] = useState<Plan | null>(null)
  const [title, setTitle] = useState('')
  const [saved, setSaved] = useState<string | null>(null)

  const currentInput = (): PlanRequestInput => ({
    ...resolveWindow(windowKey),
    city: city ?? undefined,
    latitude: isLocationReady(location) ? location.latitude : null,
    longitude: isLocationReady(location) ? location.longitude : null,
    budget: freeOnly || budget === '' ? null : Number(budget),
    maxStops,
    freeOnly,
  })

  const build = useMutation({
    mutationFn: () => api.plan(currentInput()),
    onSuccess: (result) => {
      setPlan(result)
      setSaved(null)
      // The plan knows where it ended up even when the request did not say -
      // the server resolved a city from the coordinates, and its stops carry
      // the answer. Naming it from the request would leave the title blank for
      // exactly the explorers who never chose a city.
      const label = WINDOWS.find((w) => w.key === windowKey)?.label
      const where = (result.citySlug ?? city)?.replace(/-/g, ' ')
      setTitle(result.stops.length > 0 ? [label, where && `in ${where}`].filter(Boolean).join(' ') : '')
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

  return (
    <div className="mx-auto w-full max-w-4xl px-4 pb-24 pt-6 sm:px-6 lg:px-8">
      <header className="mb-6">
        <h1 className="text-2xl font-semibold tracking-tight text-sand-900">Your plans</h1>
        <p className="mt-1 text-sand-600">
          Tell Mado when you are free and it works out an evening that fits — the
          order, the travel between stops, and anything that starts at a set time.
          Plans you keep end up here.
        </p>
      </header>

      {/* The primary way in. Planning belongs in the conversation, because an
          explorer saying "I'm free on Saturday" has already said everything the
          planner needs - asking them to restate it as a window, a budget and a
          stop count is asking for the same thing again in a worse notation. */}
      <Card className="flex flex-wrap items-center justify-between gap-4 border-brand-700/50 bg-brand-900/25 p-5">
        <div className="min-w-0">
          <p className="font-medium text-sand-900">Ask Mado to plan something</p>
          <p className="mt-0.5 text-sm text-sand-600">
            &ldquo;I&rsquo;m free this evening&rdquo; is enough to start.
          </p>
        </div>
        <Button onClick={() => openConcierge(true)}>
          <Sparkles className="size-4" aria-hidden />
          Plan with Mado
        </Button>
      </Card>

      {user && itineraries && itineraries.length > 0 && (
        <section className="mt-8">
          <SectionHeading title="Kept plans" />
          <ul className="mt-3 space-y-3">
            {itineraries.map((itinerary) => (
              <li key={itinerary.id}>
                <Card className="flex flex-wrap items-center justify-between gap-3 p-4">
                  <div className="min-w-0">
                    <p className="font-medium text-sand-900">{itinerary.title}</p>
                    <p className="text-sm text-sand-600">
                      {itinerary.stops.length} stops ·{' '}
                      {new Date(itinerary.startsAt).toLocaleDateString([], {
                        weekday: 'short',
                        day: 'numeric',
                        month: 'short',
                      })}
                      {itinerary.estimatedCost != null &&
                        itinerary.estimatedCost > 0 &&
                        ` · ${itinerary.estimatedCost.toFixed(0)} ${itinerary.currency}`}
                    </p>
                  </div>
                  <div className="flex items-center gap-2">
                    <Link to={`/plans/${itinerary.id}`}>
                      <Button variant="ghost" size="sm">
                        <Route className="size-4" aria-hidden />
                        Open
                      </Button>
                    </Link>
                    <Button
                      variant="ghost"
                      size="sm"
                      aria-label={`Delete ${itinerary.title}`}
                      onClick={() => remove.mutate(itinerary.id)}
                      disabled={remove.isPending}
                    >
                      <Trash2 className="size-4" aria-hidden />
                    </Button>
                  </div>
                </Card>
              </li>
            ))}
          </ul>
        </section>
      )}

      {user && itineraries && itineraries.length === 0 && (
        <Card className="mt-6 p-5">
          <EmptyState
            icon={<Route className="size-8" />}
            title="No plans kept yet"
            description="Ask Mado for an evening and keep the one you like. It will be here afterwards."
          />
        </Card>
      )}

      {/* Kept because some people would rather set the dials themselves, and
          because it is the only way in when the assistant is unavailable. Folded
          away, because it is not how most people will do this. */}
      <details className="mt-8 group">
        <summary className="cursor-pointer text-sm font-medium text-sand-700 hover:text-sand-900">
          Or build one yourself
        </summary>

      <Card className="mt-3 p-5">
        <fieldset>
          <legend className="text-sm font-medium text-sand-700">When</legend>
          <div className="mt-2 flex flex-wrap gap-2">
            {WINDOWS.map((option) => (
              <button
                key={option.key}
                type="button"
                onClick={() => setWindowKey(option.key)}
                aria-pressed={windowKey === option.key}
                className={
                  windowKey === option.key
                    ? 'rounded-full bg-brand-700 px-4 py-1.5 text-sm font-medium text-white'
                    : 'rounded-full border border-sand-300 px-4 py-1.5 text-sm text-sand-700 hover:bg-sand-200'
                }
              >
                {option.label}
              </button>
            ))}
          </div>
        </fieldset>

        <div className="mt-5 grid gap-5 sm:grid-cols-2">
          <div>
            <label htmlFor="stops" className="text-sm font-medium text-sand-700">
              How many stops
            </label>
            <div className="mt-2 flex gap-2">
              {[2, 3, 4, 5].map((count) => (
                <button
                  key={count}
                  type="button"
                  onClick={() => setMaxStops(count)}
                  aria-pressed={maxStops === count}
                  className={
                    maxStops === count
                      ? 'size-9 rounded-lg bg-brand-700 text-sm font-medium text-white'
                      : 'size-9 rounded-lg border border-sand-300 text-sm text-sand-700 hover:bg-sand-200'
                  }
                >
                  {count}
                </button>
              ))}
            </div>
          </div>

          <div>
            <label htmlFor="budget" className="text-sm font-medium text-sand-700">
              Budget (ETB, optional)
            </label>
            <Input
              id="budget"
              inputMode="numeric"
              placeholder="e.g. 600"
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
        </div>

        {/* Only a ready (accuracy-gated) fix counts - a too-vague network
            estimate must not look like consent was already given. */}
        {!isLocationReady(location) && (
          <p className="mt-4 text-sm text-sand-500">
            Sharing your precise location lets the planner keep the stops close together.
          </p>
        )}

        <Button
          className="mt-5 w-full sm:w-auto"
          onClick={() => build.mutate()}
          disabled={build.isPending}
        >
          <Sparkles className="size-4" aria-hidden />
          {build.isPending ? 'Working it out…' : 'Plan it'}
        </Button>

        {build.isError && (
          <p className="mt-3 text-sm text-red-300" role="alert">
            {(build.error as Error).message}
          </p>
        )}
      </Card>

      {plan && plan.stops.length === 0 && (
        <Card className="mt-6 p-5">
          <EmptyState
            icon={<CalendarClock className="size-8" />}
            title="Nothing fitted that window"
            description={plan.rationale}
          />
        </Card>
      )}

      {plan && plan.stops.length > 0 && (
        <Card className="mt-6 p-5">
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div>
              <h2 className="text-lg font-semibold text-sand-900">Your plan</h2>
              <p className="text-sm text-sand-600">{windowLabel(plan)}</p>
            </div>
            <PlanSummary
              stopCount={plan.stops.length}
              totalTravelMinutes={plan.totalTravelMinutes}
              totalCost={plan.totalCost}
              currency={plan.currency}
            />
          </div>

          <p className="mt-4 text-sm text-sand-600">{plan.rationale}</p>

          {/* Anything the planner could not satisfy is shown, not hidden. An
              explorer who asked for four stops and got two deserves to know
              which limit bit. */}
          {plan.unmet.length > 0 && (
            <ul className="mt-3 space-y-1">
              {plan.unmet.map((reason) => (
                <li
                  key={reason}
                  className="flex items-start gap-2 text-sm text-sand-500"
                >
                  <TriangleAlert className="mt-0.5 size-3.5 shrink-0" aria-hidden />
                  {reason}
                </li>
              ))}
            </ul>
          )}

          <div className="mt-6 border-t border-sand-200 pt-4">
            <PlanTimeline stops={plan.stops} />
          </div>

          {user ? (
            <div className="mt-2 flex flex-wrap items-center gap-3 border-t border-sand-200 pt-4">
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
                disabled={save.isPending || title.trim() === '' || saved !== null}
              >
                {saved ? 'Saved' : save.isPending ? 'Saving…' : 'Save itinerary'}
              </Button>
              {saved && (
                <span className="text-sm text-sand-600">
                  Kept in{' '}
                  <Link to="/plans" className="underline">
                    your itineraries
                  </Link>
                  .
                </span>
              )}
            </div>
          ) : (
            <p className="mt-4 border-t border-sand-200 pt-4 text-sm text-sand-600">
              <Link to="/signin" className="underline">
                Sign in
              </Link>{' '}
              to keep this plan.
            </p>
          )}
        </Card>
      )}

      </details>
    </div>
  )
}
