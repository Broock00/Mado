/**
 * Search and results (spec 57.02).
 *
 * Natural-language queries are supported because the backend interprets intent;
 * the interface exposes no special syntax (spec PRODUCT-04 "Search Principles").
 */

import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { AlertTriangle, SearchIcon, SlidersHorizontal } from 'lucide-react'
import { api } from '@/lib/api'
import { useDiscoveryParams, useToggleSave } from '@/app/hooks'
import { ExperienceCard, ExperienceCardSkeleton } from '@/features/experiences/ExperienceCard'
import { Badge, Button, EmptyState, Input } from '@/design-system/primitives'
import { cn } from '@/lib/utils'

const SUGGESTED_QUERIES = [
  'live music tonight',
  'somewhere quiet to work',
  'free things this weekend',
  'traditional coffee',
  'outdoors with kids',
]

export function SearchPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const initialQuery = searchParams.get('q') ?? ''
  const [input, setInput] = useState(initialQuery)
  const [submitted, setSubmitted] = useState(initialQuery)
  const [freeOnly, setFreeOnly] = useState(searchParams.get('free') === 'true')

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

  return (
    <div className="mx-auto w-full max-w-7xl px-4 pb-24 pt-6 sm:px-6 lg:px-8">
      <h1 className="mb-4 text-2xl font-semibold tracking-tight text-sand-900">Search</h1>

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
      </form>

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
              ? 'border-brand-600 bg-brand-100 text-brand-800'
              : 'border-sand-300 bg-white text-sand-700 hover:bg-sand-100',
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
                className="rounded-pill border border-sand-300 bg-white px-3.5 py-1.5 text-sm text-sand-700 transition-colors hover:border-brand-400 hover:bg-brand-50"
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
          </div>

          {data.results.length === 0 ? (
            <EmptyState
              icon={<SearchIcon className="size-8" />}
              title="Nothing matched that"
              description="Try fewer words, or ask the concierge to help narrow it down."
            />
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
