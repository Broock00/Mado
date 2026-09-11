/**
 * Search and results (spec 57.02).
 *
 * Natural-language queries are supported because the backend interprets intent;
 * the interface exposes no special syntax (spec PRODUCT-04 "Search Principles").
 */

import { Suspense, lazy, useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import {
  AlertTriangle,
  LayoutGrid,
  Map as MapIcon,
  SearchIcon,
  SlidersHorizontal,
} from 'lucide-react'
import { api } from '@/lib/api'
import { useDiscoveryParams, useToggleSave } from '@/app/hooks'
import { useAppStore, isLocationReady } from '@/app/store'
import { PlaceFilter } from '@/features/discover/PlaceFilter'
import { ExperienceCard, ExperienceCardSkeleton } from '@/features/experiences/ExperienceCard'
import { Badge, Button, EmptyState, Input } from '@/design-system/primitives'
import { cn } from '@/lib/utils'
import { useLanguage } from '@/app/language-context'
import { VisualSearchButton, VoiceSearchButton } from './SearchInputs'
import type { VisualLook } from '@/lib/types'

const SUGGESTED_QUERIES = [
  'live music tonight',
  'somewhere quiet to work',
  'free things this weekend',
  'traditional coffee',
  'outdoors with kids',
]

// Loaded on demand: MapLibre is ~200KB gzipped and most sessions never open a map.
const ExperienceMap = lazy(() =>
  import('@/features/map/ExperienceMap').then((m) => ({ default: m.ExperienceMap })),
)

export function SearchPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const initialQuery = searchParams.get('q') ?? ''
  const [input, setInput] = useState(initialQuery)
  const [submitted, setSubmitted] = useState(initialQuery)
  const [freeOnly, setFreeOnly] = useState(searchParams.get('free') === 'true')
  const [view, setView] = useState<'list' | 'map'>('list')
  // What a photograph was read as, when the search came from one. Cleared by
  // any subsequent typed search, so the explanation never outlives the results
  // it explains.
  const [look, setLook] = useState<VisualLook | null>(null)
  const { t } = useLanguage()
  const location = useAppStore((s) => s.location)

  const params = useDiscoveryParams(24)
  const { toggle, requiresAuth } = useToggleSave()

  useEffect(() => {
    setInput(initialQuery)
    setSubmitted(initialQuery)
  }, [initialQuery])

  const { data, isLoading, isError } = useQuery({
    queryKey: ['search', submitted, freeOnly, params],
    queryFn: () => api.search(submitted, { ...params, free: freeOnly }),
    enabled: submitted.trim().length > 0,
  })

  function runSearch(value: string) {
    const next = value.trim()
    setSubmitted(next)
    const updated = new URLSearchParams(searchParams)
    if (next) updated.set('q', next)
    else updated.delete('q')
    if (freeOnly) updated.set('free', 'true')
    else updated.delete('free')
    setSearchParams(updated, { replace: true })
  }

  const mappable = (data?.results ?? []).filter(
    (item) => item.venue?.latitude != null && item.venue?.longitude != null,
  )

  return (
    <div className="mx-auto w-full max-w-7xl px-4 pb-24 pt-6 sm:px-6 lg:px-8">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold tracking-tight text-sand-900">Search</h1>
        <PlaceFilter />
      </div>

      <form
        onSubmit={(event) => {
          event.preventDefault()
          runSearch(input)
        }}
        className="mb-4"
        role="search"
      >
        <div className="relative">
          <SearchIcon
            className="pointer-events-none absolute left-3.5 top-1/2 size-4 -translate-y-1/2 text-sand-400"
            aria-hidden
          />
          <Input
            value={input}
            onChange={(event) => setInput(event.target.value)}
            placeholder="Try: somewhere with live music tonight"
            aria-label="Search experiences"
            className="h-12 pl-10 pr-28"
          />
          <Button type="submit" size="sm" className="absolute right-2 top-1/2 -translate-y-1/2">
            Search
          </Button>
        </div>

        {/* Beside the box rather than inside it: both are alternatives to
            typing, not decorations on the field, and a row of icons crammed
            into the input leaves nowhere to explain what they did. */}
        <div className="mt-2 flex flex-wrap items-start gap-2">
          <VoiceSearchButton
            onHeard={(heard) => {
              // Filled in, not submitted. Recognition mishears, and a query
              // that runs before anybody has read it turns a misheard word
              // into results with no clue what went wrong.
              setInput(heard)
              setLook(null)
            }}
          />
          <VisualSearchButton
            onResult={(result) => {
              setLook(result.look)
              setInput(result.look.terms.join(' '))
              setSubmitted(result.look.unclear ? '' : result.look.terms.join(' '))
            }}
          />
        </div>
      </form>

      {look && (
        <div className="mb-4 rounded-xl border border-sand-200 bg-sand-100 px-4 py-3">
          {look.unclear ? (
            <>
              <p className="text-sm font-medium text-sand-900">{t('search.visual.unclear')}</p>
              <p className="mt-0.5 text-sm text-sand-600">
                {t('search.visual.unclear.detail')}
              </p>
            </>
          ) : (
            <>
              <p className="text-sm text-sand-900">
                {t('search.visual.looksLike', { description: look.description })}
              </p>
              {/* The whole honesty of the feature in one line. People point a
                  camera at a specific building expecting to be told which one
                  it is, and similarity is not identification. */}
              <p className="mt-0.5 text-xs text-sand-500">
                {t('search.visual.notIdentification')}
              </p>
            </>
          )}
        </div>
      )}

      <div className="mb-6 flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={() => {
            setFreeOnly((value) => !value)
            // Re-run immediately: a filter that needs a second click to apply
            // reads as broken.
            setTimeout(() => runSearch(input), 0)
          }}
          aria-pressed={freeOnly}
          className={cn(
            'inline-flex items-center gap-1.5 rounded-pill border px-3 py-1.5 text-sm transition-colors',
            freeOnly
              ? 'border-brand-500 bg-brand-900/40 text-brand-200'
              : 'border-sand-300 bg-sand-100 text-sand-700 hover:bg-sand-200',
          )}
        >
          <SlidersHorizontal className="size-3.5" aria-hidden />
          Free only
        </button>
      </div>

      {!submitted && (
        <div>
          <p className="mb-3 text-sm text-sand-500">Not sure where to start?</p>
          <div className="flex flex-wrap gap-2">
            {SUGGESTED_QUERIES.map((suggestion) => (
              <button
                key={suggestion}
                type="button"
                onClick={() => {
                  setInput(suggestion)
                  runSearch(suggestion)
                }}
                className="rounded-pill border border-sand-300 bg-sand-100 px-3.5 py-1.5 text-sm text-sand-700 transition-colors hover:border-brand-500 hover:bg-brand-900/25"
              >
                {suggestion}
              </button>
            ))}
          </div>
        </div>
      )}

      {isLoading && submitted && (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
          {[0, 1, 2, 3, 4, 5].map((index) => (
            <ExperienceCardSkeleton key={index} />
          ))}
        </div>
      )}

      {isError && (
        <EmptyState
          icon={<AlertTriangle className="size-8" />}
          title="Search is unavailable"
          description="We could not reach search just now. Try again in a moment."
        />
      )}

      {data && (
        <>
          <div className="mb-4 flex flex-wrap items-center gap-3">
            <p className="text-sm text-sand-600">
              {data.meta.total === 0
                ? 'No matches'
                : `${data.meta.total} ${data.meta.total === 1 ? 'result' : 'results'} for "${data.meta.query}"`}
            </p>
            {/* Honesty about degraded relevance, per spec 55.01 s40. */}
            {data.meta.degraded && (
              <Badge tone="accent" icon={<AlertTriangle className="size-3" aria-hidden />}>
                Showing basic results — search index unavailable
              </Badge>
            )}

            {/* Only offered when there is something to plot. A map view that
                opens onto an empty city is worse than no map view. */}
            {mappable.length > 0 && (
              <div className="ml-auto flex rounded-lg border border-sand-300 p-0.5">
                <button
                  type="button"
                  onClick={() => setView('list')}
                  aria-pressed={view === 'list'}
                  className={
                    view === 'list'
                      ? 'flex items-center gap-1.5 rounded-md bg-brand-600 px-3 py-1.5 text-sm font-medium text-white'
                      : 'flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm text-sand-700 hover:bg-sand-200'
                  }
                >
                  <LayoutGrid className="size-4" aria-hidden />
                  List
                </button>
                <button
                  type="button"
                  onClick={() => setView('map')}
                  aria-pressed={view === 'map'}
                  className={
                    view === 'map'
                      ? 'flex items-center gap-1.5 rounded-md bg-brand-600 px-3 py-1.5 text-sm font-medium text-white'
                      : 'flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm text-sand-700 hover:bg-sand-200'
                  }
                >
                  <MapIcon className="size-4" aria-hidden />
                  Map
                </button>
              </div>
            )}
          </div>

          {data.results.length === 0 ? (
            <EmptyState
              icon={<SearchIcon className="size-8" />}
              title="Nothing matched that"
              description="Try fewer words, or ask the concierge to help narrow it down."
            />
          ) : view === 'map' ? (
            <Suspense
              fallback={
                <div className="h-[28rem] w-full animate-pulse rounded-xl bg-sand-200" />
              }
            >
              {/* Same ranked order as the list: the pins are numbered to match,
                  so the two views are one result set shown two ways. */}
              <ExperienceMap
                experiences={mappable}
                origin={
                  isLocationReady(location)
                    ? { latitude: location.latitude!, longitude: location.longitude! }
                    : null
                }
              />
            </Suspense>
          ) : (
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
              {data.results.map((experience) => (
                <ExperienceCard
                  key={experience.id}
                  experience={experience}
                  onToggleSave={requiresAuth ? undefined : toggle}
                />
              ))}
            </div>
          )}
        </>
      )}
    </div>
  )
}
