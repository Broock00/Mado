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
import { Badge, EmptyState } from '@/design-system/primitives'
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

  function toggleFreeOnly() {
    setFreeOnly((value) => {
      const next = !value
      // Re-run immediately: a filter that needs a second click to apply
      // reads as broken. Defer so state has committed before we read URL.
      setTimeout(() => {
        const updated = new URLSearchParams(searchParams)
        if (input.trim()) updated.set('q', input.trim())
        else updated.delete('q')
        if (next) updated.set('free', 'true')
        else updated.delete('free')
        setSearchParams(updated, { replace: true })
        setSubmitted(input.trim())
      }, 0)
      return next
    })
  }

  const mappable = (data?.results ?? []).filter(
    (item) => item.venue?.latitude != null && item.venue?.longitude != null,
  )

  return (
    <div className="mx-auto w-full max-w-7xl px-4 pb-24 pt-6 sm:px-6 lg:px-8">
      <div className="mb-5 flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold tracking-tight text-sand-900">Search</h1>
        <PlaceFilter />
      </div>

      <form
        onSubmit={(event) => {
          event.preventDefault()
          runSearch(input)
        }}
        className="mb-5"
        role="search"
      >
        {/*
          One surface, not a row of separate controls. The field is the product;
          voice, camera, free-only and submit are actions on that field. Mobile
          keeps the same shell and moves submit + free-only onto a footer strip
          inside it so thumbs still reach them without a button junk drawer.
        */}
        <div
          className={cn(
            'overflow-hidden rounded-2xl border border-sand-300 bg-white',
            'shadow-[0_1px_2px_rgba(28,25,23,0.04)]',
            'transition-[border-color,box-shadow] focus-within:border-brand-600',
            'focus-within:shadow-[0_0_0_3px_rgba(234,88,12,0.15)]',
          )}
        >
          <div className="flex items-center gap-1 pl-3.5 pr-1.5 sm:pr-2">
            <SearchIcon className="size-4 shrink-0 text-black" aria-hidden />
            <input
              value={input}
              onChange={(event) => setInput(event.target.value)}
              placeholder="Try: somewhere with live music tonight"
              aria-label="Search experiences"
              className="h-12 min-w-0 flex-1 bg-transparent text-[15px] text-black outline-none placeholder:text-neutral-400"
            />
            <div className="flex shrink-0 items-center gap-0.5">
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
              <span className="mx-1 hidden h-5 w-px bg-neutral-200 sm:block" aria-hidden />
              <button
                type="button"
                onClick={toggleFreeOnly}
                aria-pressed={freeOnly}
                className={cn(
                  'hidden h-9 items-center gap-1.5 rounded-full px-3 text-sm transition-colors sm:inline-flex',
                  freeOnly
                    ? 'bg-brand-700 text-white'
                    : 'text-black hover:bg-black/5',
                )}
              >
                <SlidersHorizontal className="size-3.5" aria-hidden />
                Free
              </button>
              <button
                type="submit"
                className="ml-0.5 hidden h-9 items-center rounded-full bg-brand-700 px-4 text-sm font-medium text-white transition-colors hover:bg-brand-800 sm:inline-flex"
              >
                Search
              </button>
            </div>
          </div>

          <div className="flex items-center gap-2 border-t border-sand-200 px-2.5 py-1.5 sm:hidden">
            <button
              type="button"
              onClick={toggleFreeOnly}
              aria-pressed={freeOnly}
              className={cn(
                'inline-flex h-8 flex-1 items-center justify-center gap-1 rounded-lg text-xs font-medium transition-colors',
                freeOnly
                  ? 'bg-brand-700 text-white'
                  : 'bg-neutral-100 text-black',
              )}
            >
              <SlidersHorizontal className="size-3" aria-hidden />
              Free only
            </button>
            <button
              type="submit"
              className="inline-flex h-8 flex-1 items-center justify-center rounded-lg bg-brand-700 text-xs font-medium text-white"
            >
              Search
            </button>
          </div>
        </div>
      </form>

      {look && (
        <div className="mb-4 rounded-xl border border-sand-200 bg-sand-50 px-4 py-3">
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
