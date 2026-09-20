/**
 * The planning workspace (spec 10.01.04, rebuilt as visual itinerary builder).
 *
 * The old page was a form: fill in constraints, Mado solves, you keep. This one
 * is a builder: you choose stops, Mado connects and validates, you keep.
 *
 * Desktop: left sidebar (draft list + kept plans) beside the active draft.
 * Mobile: list first; opening a plan (or starting a new one) swaps to a full-
 * width builder with a back control. The two panes are never stacked on a
 * phone — that made both unreadable.
 *
 * Nothing is committed until "Keep this plan" — drafts are workspaces.
 */

import { useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  ArrowLeft,
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

/** Matches Tailwind `lg` — sidebar + builder sit side by side from here up. */
const DESKTOP_MQ = '(min-width: 1024px)'

export function PlanPage() {
  const user = useAppStore((s) => s.user)
  const activeDraftId = useAppStore((s) => s.activeDraftId)
  const setActiveDraftId = useAppStore((s) => s.setActiveDraftId)
  const queryClient = useQueryClient()
  const [searchParams] = useSearchParams()
  const { money } = useLanguage()

  // When arriving with ?addExperience=<id>, open the builder immediately.
  const addExperienceId = searchParams.get('addExperience')

  // Mobile master/detail. Desktop ignores this and always shows both panes.
  const [mobilePane, setMobilePane] = useState<'list' | 'detail'>(() =>
    addExperienceId ? 'detail' : 'list',
  )

  const { data: drafts, isLoading: draftsLoading } = useQuery({
    queryKey: ['itinerary-drafts'],
    queryFn: () => api.drafts(),
    enabled: Boolean(user),
  })

  const { data: kept, isLoading: keptLoading } = useQuery({
    queryKey: ['itineraries'],
    queryFn: () => api.itineraries(),
    enabled: Boolean(user),
  })

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
      setMobilePane('detail')
      void queryClient.invalidateQueries({ queryKey: ['itinerary-drafts'] })
    },
  })

  const deleteDraft = useMutation({
    mutationFn: (id: string) => api.deleteItinerary(id),
    onSuccess: (_, id) => {
      if (activeDraftId === id) {
        setActiveDraftId(null)
        setMobilePane('list')
      }
      void queryClient.invalidateQueries({ queryKey: ['itinerary-drafts'] })
    },
  })

  const deleteKept = useMutation({
    mutationFn: (id: string) => api.deleteItinerary(id),
    onSuccess: (_, id) => {
      if (activeDraftId === id) {
        setActiveDraftId(null)
        setMobilePane('list')
      }
      void queryClient.invalidateQueries({ queryKey: ['itineraries'] })
    },
  })

  // Deep-link into the builder when Add to Plan lands here with an experience.
  useEffect(() => {
    if (addExperienceId) setMobilePane('detail')
  }, [addExperienceId])

  // On desktop only, open the first draft so the right pane is not empty.
  // Doing this on a phone would skip the list the explorer came to see.
  useEffect(() => {
    if (!drafts || drafts.length === 0) return
    const firstId = drafts[0].id
    const mq = window.matchMedia(DESKTOP_MQ)
    function pickIfDesktop() {
      if (mq.matches && !useAppStore.getState().activeDraftId) {
        setActiveDraftId(firstId)
      }
    }
    pickIfDesktop()
    mq.addEventListener('change', pickIfDesktop)
    return () => mq.removeEventListener('change', pickIfDesktop)
  }, [drafts, setActiveDraftId])

  function openPlan(id: string) {
    setActiveDraftId(id)
    setMobilePane('detail')
  }

  function backToList() {
    setMobilePane('list')
  }

  const isBuilding = Boolean(activeDraftId)
  const showList = mobilePane === 'list'
  const showDetail = mobilePane === 'detail'

  return (
    <div className="mx-auto w-full max-w-7xl px-4 pb-28 pt-6 sm:px-6 lg:px-8">
      {/* min-w-0: a grid item defaults to min-width:auto and will blow past the
          viewport when the builder has a long title, URL, or timeline rail. */}
      <div className="grid min-w-0 gap-8 lg:grid-cols-[17rem_minmax(0,1fr)]">
        {/* ---- List pane (always on desktop; alone on mobile until a plan opens) ---- */}
        <aside
          className={cn(
            'min-w-0 space-y-6 lg:sticky lg:top-20 lg:max-h-[calc(100vh-6rem)] lg:overflow-y-auto',
            showDetail && 'hidden lg:block',
          )}
        >
          <div className="lg:hidden">
            <h1 className="text-2xl font-semibold tracking-tight text-sand-900">Plans</h1>
            <p className="mt-1 text-sm text-sand-500">
              Pick a plan to edit, or start a new one.
            </p>
          </div>

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
              className="mt-2 w-full rounded-lg border border-sand-200 bg-sand-50 px-3 py-2.5 text-sm font-medium text-sand-800 hover:bg-sand-100"
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

          {user && (
            <DraftSection
              title="In progress"
              items={drafts ?? []}
              loading={draftsLoading}
              activeId={activeDraftId}
              onSelect={openPlan}
              onDelete={(id) => deleteDraft.mutate(id)}
              money={money}
              emptyText="No plans in progress."
            />
          )}

          {user && (
            <KeptSection
              items={kept ?? []}
              loading={keptLoading}
              activeId={activeDraftId}
              onSelect={openPlan}
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

          {/* Mobile empty: no drafts yet and signed in — nudge is already in the CTAs. */}
          {user &&
            !draftsLoading &&
            !keptLoading &&
            (drafts?.length ?? 0) === 0 &&
            (kept?.length ?? 0) === 0 && (
              <p className="text-center text-sm text-sand-400 lg:hidden">
                Your plans will show up here.
              </p>
            )}
        </aside>

        {/* ---- Detail pane (always on desktop; alone on mobile when a plan is open) ---- */}
        <main className={cn('min-w-0 overflow-x-clip', showList && 'hidden lg:block')}>
          {showDetail && (
            <button
              type="button"
              onClick={backToList}
              className="mb-3 inline-flex items-center gap-1.5 text-sm font-medium text-sand-600 hover:text-sand-900 lg:hidden"
            >
              <ArrowLeft className="size-4" aria-hidden />
              All plans
            </button>
          )}

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
                <Button
                  onClick={() => createDraft.mutate({ kind: 'outing' })}
                  loading={createDraft.isPending}
                >
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
          <Skeleton className="h-14 w-full rounded-lg" />
          <Skeleton className="h-14 w-full rounded-lg" />
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
          'group relative flex items-start gap-2 rounded-xl px-3 py-3 cursor-pointer transition-colors lg:rounded-lg lg:px-2 lg:py-2',
          active
            ? 'border border-brand-600/20 bg-brand-700/10'
            : 'border border-transparent hover:bg-sand-100',
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
          className="relative z-10 rounded-md p-1.5 text-sand-400 hover:bg-sand-200 hover:text-red-500 lg:opacity-0 lg:group-hover:opacity-100"
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
          <Skeleton className="h-14 w-full rounded-lg" />
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
