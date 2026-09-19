/**
 * The planning workspace (spec 10.01.04, rebuilt as visual itinerary builder).
 *
 * The old page was a form: fill in constraints, Mado solves, you keep. This one
 * is a builder: you choose stops, Mado connects and validates, you keep.
 *
 * Layout: left sidebar (draft list + kept plans) / main area (active draft).
 * The sidebar persists across views; the main area swaps between timeline and map.
 *
 * Nothing is committed until "Keep this plan" — drafts are workspaces.
 */

import { useEffect } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  ClipboardList,
  Plus,
  Trash2,
} from 'lucide-react'

import { api } from '@/lib/api'
import { useAppStore } from '@/app/store'
import { useLanguage } from '@/app/language-context'
import type { Itinerary } from '@/lib/types'
import { Button, EmptyState, Skeleton } from '@/design-system/primitives'
import { cn } from '@/lib/utils'
import { clockTime, dayLabel } from './timeline-format'
import { ItineraryBuilder } from './ItineraryBuilder'

export function PlanPage() {
  const user = useAppStore((s) => s.user)
  const activeDraftId = useAppStore((s) => s.activeDraftId)
  const setActiveDraftId = useAppStore((s) => s.setActiveDraftId)
  const queryClient = useQueryClient()
  const [searchParams] = useSearchParams()
  const { money } = useLanguage()

  // When arriving with ?addExperience=<id>, the builder will auto-append it.
  const addExperienceId = searchParams.get('addExperience')

  // Fetch drafts (only when signed in — anonymous drafts stay in the active id)
  const { data: drafts, isLoading: draftsLoading } = useQuery({
    queryKey: ['itinerary-drafts'],
    queryFn: () => api.drafts(),
    enabled: Boolean(user),
  })

  // Fetch kept plans
  const { data: kept, isLoading: keptLoading } = useQuery({
    queryKey: ['itineraries'],
    queryFn: () => api.itineraries(),
    enabled: Boolean(user),
  })

  // Fetch the active plan document (draft or kept)
  const { data: activeDraft } = useQuery({
    queryKey: ['itinerary', activeDraftId],
    queryFn: () => api.itinerary(activeDraftId!),
    enabled: Boolean(activeDraftId),
  })

  const createDraft = useMutation({
    mutationFn: (opts?: { kind?: 'outing' | 'trip'; days?: number }) => {
      const now = new Date()
      const start = new Date(now)
      const end = new Date(now)
      const kind = opts?.kind ?? 'outing'
      const days = Math.max(1, opts?.days ?? 1)
      if (now.getHours() < 9) {
        start.setHours(9, 0, 0, 0)
      }
      if (kind === 'trip') {
        start.setHours(9, 0, 0, 0)
        end.setTime(start.getTime())
        end.setDate(end.getDate() + (days - 1))
        end.setHours(22, 0, 0, 0)
      } else {
        end.setHours(22, 0, 0, 0)
        if (end <= start) {
          end.setDate(end.getDate() + 1)
        }
      }
      return api.createDraft({
        title: kind === 'trip' ? 'Untitled trip' : 'Untitled plan',
        startsAt: start.toISOString(),
        endsAt: end.toISOString(),
        kind,
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC',
      })
    },
    onSuccess: (draft) => {
      setActiveDraftId(draft.id)
      void queryClient.invalidateQueries({ queryKey: ['itinerary-drafts'] })
    },
  })

  const deleteDraft = useMutation({
    mutationFn: (id: string) => api.deleteItinerary(id),
    onSuccess: (_, id) => {
      if (activeDraftId === id) setActiveDraftId(null)
      void queryClient.invalidateQueries({ queryKey: ['itinerary-drafts'] })
    },
  })

  const deleteKept = useMutation({
    mutationFn: (id: string) => api.deleteItinerary(id),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['itineraries'] }),
  })

  // Pick the first draft on load if none is active.
  useEffect(() => {
    if (!activeDraftId && drafts && drafts.length > 0) {
      setActiveDraftId(drafts[0].id)
    }
  }, [drafts, activeDraftId, setActiveDraftId])

  const isBuilding = Boolean(activeDraftId)

  return (
    <div className="mx-auto w-full max-w-7xl px-4 pb-28 pt-6 sm:px-6 lg:px-8">
      <div className="grid gap-8 lg:grid-cols-[17rem_minmax(0,1fr)]">
        {/* ---- Left sidebar ---- */}
        <aside className="space-y-6 lg:sticky lg:top-20 lg:max-h-[calc(100vh-6rem)] lg:overflow-y-auto">
          {/* Start a new draft */}
          <div>
            <Button
              className="w-full"
              onClick={() => createDraft.mutate({ kind: 'outing' })}
              loading={createDraft.isPending}
            >
              <Plus className="size-4" aria-hidden />
              New plan
            </Button>
            <button
              type="button"
              className="mt-2 w-full rounded-lg border border-sand-200 bg-sand-50 px-3 py-2 text-sm font-medium text-sand-800 hover:bg-sand-100"
              onClick={() => createDraft.mutate({ kind: 'trip', days: 3 })}
              disabled={createDraft.isPending}
            >
              New multi-day trip
            </button>
            <p className="mt-2 text-center text-xs text-sand-500">
              Or{' '}
              <button
                type="button"
                className="underline hover:text-sand-800"
                onClick={() => useAppStore.getState().toggleConcierge(true)}
              >
                ask Mado
              </button>{' '}
              to build one for you
            </p>
          </div>

          {/* Active drafts */}
          {user && (
            <DraftSection
              title="In progress"
              items={drafts ?? []}
              loading={draftsLoading}
              activeId={activeDraftId}
              onSelect={setActiveDraftId}
              onDelete={(id) => deleteDraft.mutate(id)}
              money={money}
              emptyText="No plans in progress."
            />
          )}

          {/* Kept plans — open in the same builder so they remain editable */}
          {user && (
            <KeptSection
              items={kept ?? []}
              loading={keptLoading}
              activeId={activeDraftId}
              onSelect={setActiveDraftId}
              onDelete={(id) => deleteKept.mutate(id)}
              money={money}
            />
          )}

          {!user && (
            <p className="rounded-lg border border-sand-200 bg-sand-50 px-3 py-3 text-sm text-sand-600">
              <Link to="/signin" className="font-medium underline">
                Sign in
              </Link>{' '}
              to save drafts across devices and keep plans.
            </p>
          )}
        </aside>

        {/* ---- Main workspace ---- */}
        <main>
          {isBuilding ? (
            <ItineraryBuilder
              draftId={activeDraftId!}
              draft={activeDraft ?? null}
              addExperienceId={addExperienceId}
              onKept={(itinerary) => {
                setActiveDraftId(itinerary.id)
                void queryClient.invalidateQueries({ queryKey: ['itineraries'] })
                void queryClient.invalidateQueries({ queryKey: ['itinerary-drafts'] })
                void queryClient.invalidateQueries({ queryKey: ['itinerary', itinerary.id] })
              }}
            />
          ) : (
            <EmptyState
              icon={<ClipboardList className="size-8" />}
              title="No plan selected"
              description="Start a new plan or pick one from the list to continue building."
              action={
                <Button onClick={() => createDraft.mutate({ kind: 'outing' })} loading={createDraft.isPending}>
                  <Plus className="size-4" aria-hidden />
                  Start a plan
                </Button>
              }
            />
          )}
        </main>
      </div>
    </div>
  )
}

// ---- Sidebar sections -------------------------------------------------------

function DraftSection({
  title,
  items,
  loading,
  activeId,
  onSelect,
  onDelete,
  money,
  emptyText,
}: {
  title: string
  items: Itinerary[]
  loading: boolean
  activeId: string | null
  onSelect: (id: string) => void
  onDelete: (id: string) => void
  money: (amount: number, currency: string) => string
  emptyText: string
}) {
  if (loading) {
    return (
      <section>
        <p className="text-xs font-medium uppercase tracking-wide text-sand-500">{title}</p>
        <div className="mt-2 space-y-1">
          <Skeleton className="h-12 w-full rounded-lg" />
          <Skeleton className="h-12 w-full rounded-lg" />
        </div>
      </section>
    )
  }

  return (
    <section>
      <p className="text-xs font-medium uppercase tracking-wide text-sand-500">{title}</p>
      {items.length === 0 ? (
        <p className="mt-2 text-sm text-sand-400">{emptyText}</p>
      ) : (
        <ul className="mt-2 space-y-1">
          {items.map((item) => (
            <DraftListItem
              key={item.id}
              item={item}
              active={item.id === activeId}
              onSelect={() => onSelect(item.id)}
              onDelete={() => onDelete(item.id)}
              money={money}
            />
          ))}
        </ul>
      )}
    </section>
  )
}

function DraftListItem({
  item,
  active,
  onSelect,
  onDelete,
  money,
}: {
  item: Itinerary
  active: boolean
  onSelect: () => void
  onDelete: () => void
  money: (amount: number, currency: string) => string
}) {
  const cost =
    item.estimatedCost != null && item.estimatedCost > 0
      ? money(item.estimatedCost, item.currency)
      : null

  return (
    <li>
      <div
        className={cn(
          'group relative flex items-start gap-2 rounded-lg px-2 py-2 cursor-pointer transition-colors',
          active
            ? 'bg-brand-700/10 border border-brand-600/20'
            : 'hover:bg-sand-100 border border-transparent',
        )}
      >
        <button
          type="button"
          className="min-w-0 flex-1 text-left after:absolute after:inset-0"
          onClick={onSelect}
          aria-current={active ? 'true' : undefined}
        >
          <p className="truncate text-sm font-medium text-sand-900">{item.title}</p>
          <p className="mt-0.5 text-xs text-sand-500">
            {item.kind === 'trip' ? 'Trip · ' : ''}
            {dayLabel(item.startsAt)}
            {' · '}
            {clockTime(item.startsAt)}
            {' · '}
            {item.stops.length} {item.stops.length === 1 ? 'stop' : 'stops'}
            {cost && ` · ${cost}`}
          </p>
        </button>
        <button
          type="button"
          aria-label={`Delete ${item.title}`}
          onClick={(e) => {
            e.stopPropagation()
            onDelete()
          }}
          className="relative z-10 rounded-md p-1 text-sand-400 hover:bg-sand-200 hover:text-red-500 lg:opacity-0 lg:group-hover:opacity-100"
        >
          <Trash2 className="size-3.5" aria-hidden />
        </button>
      </div>
    </li>
  )
}

function KeptSection({
  items,
  loading,
  activeId,
  onSelect,
  onDelete,
  money,
}: {
  items: Itinerary[]
  loading: boolean
  activeId: string | null
  onSelect: (id: string) => void
  onDelete: (id: string) => void
  money: (amount: number, currency: string) => string
}) {
  if (loading) {
    return (
      <section>
        <p className="text-xs font-medium uppercase tracking-wide text-sand-500">Kept plans</p>
        <div className="mt-2 space-y-1">
          <Skeleton className="h-10 w-full rounded-lg" />
        </div>
      </section>
    )
  }

  if (items.length === 0) return null

  return (
    <section>
      <p className="text-xs font-medium uppercase tracking-wide text-sand-500">Kept plans</p>
      <ul className="mt-2 space-y-1">
        {items.map((item) => (
          <DraftListItem
            key={item.id}
            item={item}
            active={item.id === activeId}
            onSelect={() => onSelect(item.id)}
            onDelete={() => onDelete(item.id)}
            money={money}
          />
        ))}
      </ul>
    </section>
  )
}
