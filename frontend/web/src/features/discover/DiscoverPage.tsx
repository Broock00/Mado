/**
 * The Discovery Canvas (spec 10.01.03).
 *
 * Not a feed. Independently-ranked modules, each answering a different version of
 * "what should I do next?" - and any module with nothing eligible is omitted by
 * the API rather than rendered empty.
 *
 * Colour palette follows the Mado logo: near-black surface, white type, and the
 * logo-orange used sparingly for the anchor actions the eye should reach first.
 */

import { Link } from 'react-router-dom'
import { Compass, MapPin, Sparkles } from 'lucide-react'
import { useCanvas, useLocationContext, useRequestLocation, useToggleSave } from '@/app/hooks'
import { PlaceFilter } from './PlaceFilter'
import { useAppStore } from '@/app/store'
import { ExperienceCard, ExperienceCardSkeleton } from '@/features/experiences/ExperienceCard'
import { Button, EmptyState } from '@/design-system/primitives'
import type { FeedModule } from '@/lib/types'
import { cn } from '@/lib/utils'

export function DiscoverPage() {
  const { data, isLoading, isError, refetch } = useCanvas()
  const chosen = useAppStore((s) => s.place)
  const location = useAppStore((s) => s.location)
  const requestLocation = useRequestLocation()
  const { data: context } = useLocationContext()

  const hasSomewhere = Boolean(chosen) || location.granted

  const where =
    chosen?.label ??
    (context?.resolved ? context.place?.area || context.place?.label : null) ??
    data?.areaLabel ??
    null
  const { toggle, requiresAuth } = useToggleSave()

  return (
    /* Full-bleed dark canvas. The negative margins pull edge-to-edge past the
       shell's own padding; matching padding is re-applied inside. */
    <div className="min-h-screen bg-sand-50">
      <div className="mx-auto w-full max-w-7xl px-4 pb-28 pt-8 sm:px-6 lg:px-8">

        {/* Page header */}
        <header className="mb-8 flex flex-wrap items-start justify-between gap-4">
          <div>
            <h1 className="text-3xl font-bold tracking-tight text-white sm:text-4xl">
              What should you do next?
            </h1>
            <p className="mt-2 text-sm text-sand-500">
              {where
                ? `Discover what is happening around ${where} right now.`
                : 'Discover what is happening around you right now.'}
            </p>
          </div>
          <PlaceFilter />
        </header>

        {/* Location prompt */}
        {!location.granted && !location.denied && (
          <div className="mb-8 flex flex-col gap-3 rounded-xl border border-sand-200 bg-sand-100 p-4 sm:flex-row sm:items-center sm:justify-between">
            <div className="flex items-start gap-3">
              <MapPin className="mt-0.5 size-5 shrink-0 text-brand-700" aria-hidden />
              <div>
                <p className="text-sm font-medium text-white">Show me what is close by</p>
                <p className="text-sm text-sand-500">
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
            icon={<Compass className="size-8 text-sand-500" />}
            title="We could not load your discoveries"
            description="Something went wrong reaching the platform. Your saved items are unaffected."
            action={<Button onClick={() => refetch()}>Try again</Button>}
          />
        )}

        {data && !hasSomewhere && (
          <EmptyState
            icon={<Compass className="size-8 text-sand-500" />}
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
            icon={<Compass className="size-8 text-sand-500" />}
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
      {/* Section heading — white title, orange "See all" */}
      <div className="mb-4 flex items-end justify-between gap-4">
        <div className="min-w-0">
          <h2
            id={`rail-${module.key}`}
            className="text-lg font-semibold tracking-tight text-white"
          >
            {module.title}
          </h2>
          {module.subtitle && (
            <p className="mt-0.5 text-sm text-sand-500">{module.subtitle}</p>
          )}
        </div>
        <Link
          to={`/search?rail=${module.key}`}
          className="shrink-0 text-sm font-medium text-brand-700 transition-colors hover:text-brand-600"
        >
          See all
        </Link>
      </div>

      {/* Horizontal scroll with snap points (spec 11.04). */}
      <div className="scrollbar-none -mx-4 flex snap-x snap-mandatory gap-3 overflow-x-auto px-4 pb-2 sm:mx-0 sm:px-0">
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
          <div className="mb-4 h-5 w-36 animate-pulse rounded bg-sand-200" />
          <div className="flex gap-3 overflow-hidden">
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
    <div className={cn(
      'flex items-center gap-2 rounded-lg px-3 py-2 text-sm',
      'bg-[var(--color-ai-soft)] text-[var(--color-ai)]',
    )}>
      <Sparkles className="size-4" aria-hidden />
      AI-picked from your interests
    </div>
  )
}
