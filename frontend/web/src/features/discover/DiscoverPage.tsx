/**
 * The Discovery Canvas (spec 10.01.03).
 *
 * Not a feed. Independently-ranked modules, each answering a different version of
 * "what should I do next?" - and any module with nothing eligible is omitted by
 * the API rather than rendered empty.
 */

import { Link } from 'react-router-dom'
import { Compass, MapPin, Sparkles } from 'lucide-react'
import { useCanvas, useLocationContext, useRequestLocation, useToggleSave } from '@/app/hooks'
import { PlaceFilter } from './PlaceFilter'
import { useAppStore } from '@/app/store'
import { ExperienceCard, ExperienceCardSkeleton } from '@/features/experiences/ExperienceCard'
import { Button, EmptyState, SectionHeading } from '@/design-system/primitives'
import type { FeedModule } from '@/lib/types'

export function DiscoverPage() {
  const { data, isLoading, isError, refetch } = useCanvas()
  const chosen = useAppStore((s) => s.place)
  const location = useAppStore((s) => s.location)
  const requestLocation = useRequestLocation()
  const { data: context } = useLocationContext()

  // Whether the interface knows where it is looking at all. A chosen place
  // always does; otherwise it needs a granted location.
  const hasSomewhere = Boolean(chosen) || location.granted

  // Named from where we are actually looking: the place that was chosen, or the
  // one the explorer's coordinates resolved to. Never from a constant - this
  // line used to say "Addis Ababa" to everybody on earth.
  const where =
    chosen?.label ??
    (context?.resolved ? context.place?.area || context.place?.label : null) ??
    data?.areaLabel ??
    null
  const { toggle, requiresAuth } = useToggleSave()

  return (
    <div className="mx-auto w-full max-w-7xl px-4 pb-24 pt-6 sm:px-6 lg:px-8">
      <header className="mb-6 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-sand-900 sm:text-3xl">
            What should you do next?
          </h1>
          {/* Named from what the server resolved, never from a constant. This
              line used to say "Addis Ababa" to every explorer on earth. */}
          <p className="mt-1.5 text-sand-500">
            {where
              ? `Discover what is happening around ${where} right now.`
              : 'Discover what is happening around you right now.'}
          </p>
        </div>
        <PlaceFilter />
      </header>

      {/* Location is requested in context, at the point where it visibly improves
          results - not with a modal on first load (spec 10.01.02). */}
      {!location.granted && !location.denied && (
        <div className="mb-6 flex flex-col gap-3 rounded-card border border-brand-200 bg-brand-50 p-4 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex items-start gap-3">
            <MapPin className="mt-0.5 size-5 shrink-0 text-brand-700" aria-hidden />
            <div>
              <p className="text-sm font-medium text-brand-900">Show me what is close by</p>
              <p className="text-sm text-brand-800/80">
                Share your location and we will sort by walking distance.
              </p>
            </div>
          </div>
          <Button size="sm" onClick={requestLocation} className="shrink-0">
            Use my location
          </Button>
        </div>
      )}

      {isLoading && <CanvasSkeleton />}

      {isError && (
        <EmptyState
          icon={<Compass className="size-8" />}
          title="We could not load your discoveries"
          description="Something went wrong reaching the platform. Your saved items are unaffected."
          action={<Button onClick={() => refetch()}>Try again</Button>}
        />
      )}

      {/*
        Two empty states, because they have different remedies. Not knowing
        where to look is answered by sharing a location or searching; knowing
        and finding nothing is answered by looking somewhere else or coming
        back. Collapsing them is what made "nothing here" read as a broken page.

        The condition is whether *we* have somewhere to look, not whether the
        server matched a city row. Searching Brooklyn and being told "we do not
        know where you are" was the version of this that shipped for one
        screenshot: the header said Brooklyn and the body said we had no idea.
      */}
      {data && !hasSomewhere && (
        <EmptyState
          icon={<Compass className="size-8" />}
          title="We do not know where you are yet"
          description="Share your location and Mado will show what is on around you, or search for anywhere above."
          action={
            !location.granted ? (
              <Button onClick={requestLocation}>Use my location</Button>
            ) : undefined
          }
        />
      )}

      {data && hasSomewhere && data.modules.length === 0 && (
        <EmptyState
          icon={<Compass className="size-8" />}
          title="Nothing published around here yet"
          description="Nobody has posted anything near this spot. Search for somewhere else above, or check back soon."
        />
      )}

      <div className="space-y-10">
        {data?.modules.map((module) => (
          <Rail
            key={module.key}
            module={module}
            onToggleSave={requiresAuth ? undefined : toggle}
          />
        ))}
      </div>
    </div>
  )
}

function Rail({
  module,
  onToggleSave,
}: {
  module: FeedModule
  onToggleSave?: (experience: import('@/lib/types').ExperienceSummary) => void
}) {
  return (
    <section aria-labelledby={`rail-${module.key}`}>
      <SectionHeading
        title={module.title}
        subtitle={module.subtitle}
        action={
          <Link
            to={`/search?rail=${module.key}`}
            className="shrink-0 text-sm font-medium text-brand-700 hover:text-brand-800"
          >
            See all
          </Link>
        }
      />
      {/* Horizontal scroll with snap points. The clipped final card is the
          affordance that more exists - spec 11.04 interaction patterns. */}
      <div className="scrollbar-none -mx-4 flex snap-x snap-mandatory gap-4 overflow-x-auto px-4 pb-2 sm:mx-0 sm:px-0">
        {module.items.map((experience) => (
          <div key={experience.id} className="snap-start">
            <ExperienceCard experience={experience} onToggleSave={onToggleSave} fixedWidth />
          </div>
        ))}
      </div>
    </section>
  )
}

function CanvasSkeleton() {
  return (
    <div className="space-y-10">
      {[0, 1, 2].map((row) => (
        <div key={row}>
          <div className="mb-3 h-6 w-40 animate-pulse rounded bg-sand-200" />
          <div className="flex gap-4 overflow-hidden">
            {[0, 1, 2, 3].map((card) => (
              <ExperienceCardSkeleton key={card} fixedWidth />
            ))}
          </div>
        </div>
      ))}
    </div>
  )
}

export function AiPicksBanner() {
  return (
    <div className="flex items-center gap-2 rounded-lg bg-[var(--color-ai-soft)] px-3 py-2 text-sm text-[var(--color-ai)]">
      <Sparkles className="size-4" aria-hidden />
      AI-picked from your interests
    </div>
  )
}
