/**
 * Add Stop search overlay.
 *
 * Surfaces the existing search API as a picker. The explorer types to find a
 * place, event or activity and taps to add it to the active draft. Same /search
 * the rest of Mado uses — no parallel search system.
 *
 * Visual language matches ConciergePanel / CityPicker: sand surface, lifted
 * shadow, not a white sheet that breaks the page.
 */

import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Clock, MapPin, Search, X } from 'lucide-react'

import { api } from '@/lib/api'
import { useAppStore } from '@/app/store'
import type { ExperienceSummary } from '@/lib/types'
import { Input } from '@/design-system/primitives'
import { cn, formatWhen } from '@/lib/utils'

interface Props {
  draftId: string
  existingExperienceIds: string[]
  /** Which trip day to append to (0 for outings). */
  dayIndex?: number
  onStopAdded: () => void
  onClose: () => void
}

export function AddStopSearch({
  draftId,
  existingExperienceIds,
  dayIndex = 0,
  onStopAdded,
  onClose,
}: Props) {
  const [query, setQuery] = useState('')
  const inputRef = useRef<HTMLInputElement>(null)
  const city = useAppStore((s) => s.citySlug)
  const place = useAppStore((s) => s.place)
  const location = useAppStore((s) => s.location)

  useEffect(() => {
    inputRef.current?.focus()
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  const { data: results, isLoading: searching } = useQuery({
    queryKey: ['add-stop-search', query, city],
    queryFn: () =>
      api.search(query, {
        city: place ? undefined : (city ?? undefined),
        lat: location.status === 'ready' ? location.latitude : null,
        lng: location.status === 'ready' ? location.longitude : null,
        limit: 12,
      }),
    enabled: query.length >= 2,
    staleTime: 30_000,
  })

  const addStop = useMutation({
    mutationFn: ({ id, eventId }: { id: string; eventId?: string | null }) =>
      api.appendStop(draftId, id, eventId, dayIndex),
    onSuccess: onStopAdded,
  })

  const experiences: ExperienceSummary[] = results?.results ?? []

  return (
    <>
      <div
        className="fixed inset-0 z-40 bg-sand-900/40 backdrop-blur-[2px]"
        onClick={onClose}
        aria-hidden
      />

      <div
        role="dialog"
        aria-label="Add a stop"
        aria-modal="true"
        className="fixed inset-x-0 bottom-0 z-50 flex max-h-[85vh] flex-col rounded-t-2xl border border-sand-200 bg-sand-100 shadow-lifted sm:inset-auto sm:right-8 sm:top-20 sm:bottom-auto sm:w-[28rem] sm:max-h-[32rem] sm:rounded-2xl"
      >
        <div className="flex items-center justify-between gap-3 border-b border-sand-200 px-4 py-3">
          <div className="min-w-0">
            <p className="text-sm font-semibold text-sand-900">Add a stop</p>
            <p className="text-xs text-sand-500">Search places, events, and activities</p>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="rounded-lg p-1.5 text-sand-500 transition-colors hover:bg-sand-200 hover:text-sand-800"
          >
            <X className="size-4" aria-hidden />
          </button>
        </div>

        <div className="border-b border-sand-200 px-4 py-3">
          <div className="relative">
            <Search
              className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-sand-500"
              aria-hidden
            />
            <Input
              ref={inputRef}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Try café, jazz, museum…"
              aria-label="Search for a stop"
              className="pl-9"
            />
          </div>
        </div>

        <ul className="min-h-0 flex-1 overflow-y-auto px-2 py-2">
          {query.length < 2 ? (
            <li className="px-3 py-8 text-center text-sm text-sand-500">
              Type at least two characters to search.
            </li>
          ) : searching ? (
            Array.from({ length: 3 }).map((_, i) => (
              <li key={i} className="flex gap-3 rounded-xl px-2 py-2">
                <div className="size-12 animate-pulse rounded-lg bg-sand-200" />
                <div className="flex-1 space-y-1.5 py-1">
                  <div className="h-3.5 w-40 animate-pulse rounded bg-sand-200" />
                  <div className="h-3 w-24 animate-pulse rounded bg-sand-200" />
                </div>
              </li>
            ))
          ) : experiences.length === 0 ? (
            <li className="px-3 py-8 text-center text-sm text-sand-500">
              Nothing matched — try a different search.
            </li>
          ) : (
            experiences.map((exp) => {
              const already = existingExperienceIds.includes(exp.id)
              const when = formatWhen(exp.nextEvent?.startTime)

              return (
                <li key={exp.id}>
                  <button
                    type="button"
                    disabled={already || addStop.isPending}
                    onClick={() =>
                      addStop.mutate({
                        id: exp.id,
                        eventId: exp.nextEvent?.id ?? null,
                      })
                    }
                    className={cn(
                      'flex w-full items-center gap-3 rounded-xl px-2 py-2 text-left transition-colors',
                      already
                        ? 'cursor-default opacity-50'
                        : 'hover:bg-sand-200 active:bg-sand-300',
                    )}
                  >
                    {exp.media[0] ? (
                      <img
                        src={exp.media[0].url}
                        alt=""
                        className="size-12 shrink-0 rounded-lg object-cover"
                      />
                    ) : (
                      <span className="grid size-12 shrink-0 place-items-center rounded-lg bg-sand-200 text-sand-500">
                        <MapPin className="size-5" aria-hidden />
                      </span>
                    )}
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-sm font-medium text-sand-900">{exp.title}</p>
                      <p className="mt-0.5 truncate text-xs text-sand-500">
                        {exp.category?.name ?? exp.venue?.name ?? ''}
                        {when && (
                          <span className="ml-1.5 inline-flex items-center gap-0.5 text-brand-700">
                            <Clock className="size-3" aria-hidden />
                            {when}
                          </span>
                        )}
                      </p>
                    </div>
                    {already ? (
                      <span className="shrink-0 text-xs text-sand-500">Added</span>
                    ) : (
                      <span className="shrink-0 rounded-lg bg-brand-700 px-2.5 py-1 text-xs font-medium text-white">
                        Add
                      </span>
                    )}
                  </button>
                </li>
              )
            })
          )}
        </ul>

        {addStop.isError && (
          <p className="border-t border-sand-200 px-4 py-2 text-sm text-red-600" role="alert">
            {(addStop.error as Error).message || 'Could not add that stop. Try again.'}
          </p>
        )}
      </div>
    </>
  )
}
