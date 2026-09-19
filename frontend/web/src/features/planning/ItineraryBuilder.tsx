/**
 * The itinerary builder workspace.
 *
 * Receives an active draft and lets the explorer:
 *   – Add stops via search or from discovery surfaces
 *   – Reorder stops (drag-and-drop)
 *   – Remove or open any stop
 *   – Edit arrival/departure times
 *   – View as timeline or map
 *   – Check My Plan (conflicts + gaps)
 *   – Fill This Gap (proposals)
 *   – Optimize (2-opt proposal, confirm before apply)
 *   – Ask Mado (concierge with draft context)
 *   – Keep the plan (promote to saved itinerary)
 *
 * The explorer is always in control. Nothing changes without explicit confirmation.
 */

import { useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle,
  CheckCircle,
  ChevronDown,
  ChevronUp,
  Clock,
  GripVertical,
  Map as MapIcon,
  Pencil,
  Plus,
  RotateCcw,
  Search,
  Sparkles,
  X,
} from 'lucide-react'

import { api } from '@/lib/api'
import { useAppStore } from '@/app/store'
import { useLanguage } from '@/app/language-context'
import type { Itinerary, PlanAnalysis, PlanGap, PlanStop, StopSpec } from '@/lib/types'
import { Button, Input, Skeleton } from '@/design-system/primitives'
import { RouteMap } from '@/features/map/RouteMap'
import { cn } from '@/lib/utils'
import { clockTime, duration } from './timeline-format'
import { useStopDetails } from './useStopDetails'
import { AddStopSearch } from './AddStopSearch'
import { ConflictBanner } from './ConflictBanner'
import { FillGapCard } from './FillGapCard'
import { OptimizePreview } from './OptimizePreview'
import { PlanShareBar } from './PlanShareBar'

interface Props {
  draftId: string
  draft: Itinerary | null
  addExperienceId?: string | null
  onKept: (itinerary: Itinerary) => void
}

export function ItineraryBuilder({ draftId, draft, addExperienceId, onKept }: Props) {
  const queryClient = useQueryClient()
  const openConcierge = useAppStore((s) => s.toggleConcierge)
  const { money } = useLanguage()

  const [view, setView] = useState<'timeline' | 'map'>('timeline')
  const [addStopOpen, setAddStopOpen] = useState(false)
  const [analysis, setAnalysis] = useState<PlanAnalysis | null>(null)
  const [fillGap, setFillGap] = useState<PlanGap | null>(null)
  const [optimizeProposal, setOptimizeProposal] = useState<import('@/lib/types').Plan | null>(null)
  const [editingTitle, setEditingTitle] = useState(false)
  const [titleValue, setTitleValue] = useState(draft?.title ?? 'Untitled plan')
  const [keepTitle, setKeepTitle] = useState('')
  const [showKeepInput, setShowKeepInput] = useState(false)
  const [activeDay, setActiveDay] = useState(0)
  const titleInputRef = useRef<HTMLInputElement>(null)

  // Drag state (indices are within the active day's stop list)
  const [dragIndex, setDragIndex] = useState<number | null>(null)
  const [dropIndex, setDropIndex] = useState<number | null>(null)

  const stops = useMemo(
    () =>
      [...(draft?.stops ?? [])].sort(
        (a, b) => (a.dayIndex ?? 0) - (b.dayIndex ?? 0) || a.arriveAt.localeCompare(b.arriveAt),
      ),
    [draft?.stops],
  )
  const stopDetails = useStopDetails(stops)

  const dayCount = useMemo(() => tripDayCount(draft, stops), [draft, stops])
  const isTrip = dayCount > 1 || draft?.kind === 'trip'

  useEffect(() => {
    if (activeDay >= dayCount) setActiveDay(Math.max(0, dayCount - 1))
  }, [activeDay, dayCount])

  const dayStops = useMemo(
    () => stops.filter((s) => (s.dayIndex ?? 0) === activeDay),
    [stops, activeDay],
  )

  // Auto-add a stop when arriving with ?addExperience=
  const didAutoAdd = useRef(false)
  const appendStop = useMutation({
    mutationFn: ({ id, eventId, day }: { id: string; eventId?: string | null; day?: number }) =>
      api.appendStop(draftId, id, eventId, day ?? activeDay),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['itinerary', draftId] })
      void queryClient.invalidateQueries({ queryKey: ['itinerary-drafts'] })
    },
  })

  useEffect(() => {
    if (addExperienceId && draft && !didAutoAdd.current) {
      didAutoAdd.current = true
      appendStop.mutate({ id: addExperienceId, day: activeDay })
    }
  }, [addExperienceId, draft, appendStop, activeDay])

  const replaceStops = useMutation({
    mutationFn: (specs: StopSpec[]) => api.replaceStops(draftId, specs),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['itinerary', draftId] })
      void queryClient.invalidateQueries({ queryKey: ['itinerary-drafts'] })
      void queryClient.invalidateQueries({ queryKey: ['itineraries'] })
      setAnalysis(null)
    },
  })

  const removeStop = useMutation({
    mutationFn: (stopId: string) => api.removeStop(draftId, stopId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['itinerary', draftId] })
      void queryClient.invalidateQueries({ queryKey: ['itinerary-drafts'] })
      void queryClient.invalidateQueries({ queryKey: ['itineraries'] })
      setAnalysis(null)
    },
  })

  const checkPlan = useMutation({
    mutationFn: () => api.checkItinerary(draftId),
    onSuccess: (result) => setAnalysis(result),
  })

  const optimizePlan = useMutation({
    mutationFn: () => api.optimizeItinerary(draftId),
    onSuccess: (proposal) => setOptimizeProposal(proposal),
  })

  const fillGapMutation = useMutation({
    mutationFn: (gap: PlanGap) =>
      api.fillGap(draftId, gap.afterIndex, gap.startsAt, gap.endsAt),
  })

  const patchTitle = useMutation({
    mutationFn: (title: string) => api.patchDraft(draftId, { title }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['itinerary', draftId] })
      void queryClient.invalidateQueries({ queryKey: ['itinerary-drafts'] })
      void queryClient.invalidateQueries({ queryKey: ['itineraries'] })
      setEditingTitle(false)
    },
  })

  const patchWindow = useMutation({
    mutationFn: (endsAt: string) => api.patchDraft(draftId, { endsAt }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['itinerary', draftId] })
      void queryClient.invalidateQueries({ queryKey: ['itinerary-drafts'] })
    },
  })

  const keepDraft = useMutation({
    mutationFn: (title: string) => api.keepDraft(draftId, title || undefined),
    onSuccess: (itinerary) => {
      onKept(itinerary)
    },
  })

  function specsFrom(list: PlanStop[]): StopSpec[] {
    return list.map((s) => ({
      experienceId: s.experienceId,
      eventInstanceId: s.eventInstanceId ?? null,
      isFixedTime: s.isFixedTime,
      arriveAt: s.arriveAt,
      departAt: s.departAt,
      note: s.note,
      dayIndex: s.dayIndex ?? 0,
    }))
  }

  /** Rebuild the full stop list after reordering within the active day. */
  function mergeDayOrder(reorderedDay: PlanStop[]): PlanStop[] {
    const byDay = new Map<number, PlanStop[]>()
    for (const s of stops) {
      const d = s.dayIndex ?? 0
      if (d === activeDay) continue
      const bucket = byDay.get(d) ?? []
      bucket.push(s)
      byDay.set(d, bucket)
    }
    byDay.set(activeDay, reorderedDay)
    const days = [...byDay.keys()].sort((a, b) => a - b)
    return days.flatMap((d) => byDay.get(d) ?? [])
  }

  function handleRemove(localIndex: number) {
    const stop = dayStops[localIndex]
    if (stop?.id) {
      removeStop.mutate(stop.id)
      return
    }
    replaceStops.mutate(
      specsFrom(mergeDayOrder(dayStops.filter((_, i) => i !== localIndex))),
    )
  }

  function moveStop(from: number, to: number) {
    if (to < 0 || to >= dayStops.length || from === to) return
    const reordered = [...dayStops]
    const [moved] = reordered.splice(from, 1)
    reordered.splice(to, 0, moved)
    replaceStops.mutate(specsFrom(mergeDayOrder(reordered)))
  }

  // Drag-and-drop — dataTransfer.setData is required for drop to fire in Firefox.
  function onDragStart(e: React.DragEvent, index: number) {
    e.dataTransfer.setData('text/plain', String(index))
    e.dataTransfer.effectAllowed = 'move'
    setDragIndex(index)
  }

  function onDragOver(e: React.DragEvent, index: number) {
    e.preventDefault()
    e.dataTransfer.dropEffect = 'move'
    setDropIndex(index)
  }

  function onDrop(e: React.DragEvent) {
    e.preventDefault()
    const from = Number(e.dataTransfer.getData('text/plain'))
    const to = dropIndex
    if (Number.isNaN(from) || to === null || from === to) {
      setDragIndex(null)
      setDropIndex(null)
      return
    }
    moveStop(from, to)
    setDragIndex(null)
    setDropIndex(null)
  }

  function onDragEnd() {
    setDragIndex(null)
    setDropIndex(null)
  }

  function acceptOptimize() {
    if (!optimizeProposal) return
    replaceStops.mutate(specsFrom(optimizeProposal.stops))
    setOptimizeProposal(null)
  }

  function acceptFillGap(stopId: string) {
    if (!fillGap) return
    const chosen = fillGapMutation.data?.stops.find((s) => s.experienceId === stopId)
    if (!chosen) return
    const dayIndex =
      fillGap.afterIndex >= 0
        ? (stops[fillGap.afterIndex]?.dayIndex ?? activeDay)
        : activeDay
    const updated = [...stops]
    updated.splice(fillGap.afterIndex + 1, 0, { ...chosen, dayIndex })
    replaceStops.mutate(specsFrom(updated))
    setFillGap(null)
    fillGapMutation.reset()
  }

  function addDay() {
    if (!draft || dayCount >= 14) return
    const end = new Date(draft.endsAt)
    end.setDate(end.getDate() + 1)
    end.setHours(22, 0, 0, 0)
    patchWindow.mutate(end.toISOString(), {
      onSuccess: () => setActiveDay(dayCount),
    })
  }

  const mutationError =
    (replaceStops.error as Error | null)?.message ||
    (removeStop.error as Error | null)?.message ||
    (keepDraft.error as Error | null)?.message ||
    (appendStop.error as Error | null)?.message ||
    (patchWindow.error as Error | null)?.message ||
    null

  if (!draft) {
    return (
      <div className="flex flex-col gap-3 py-10">
        <Skeleton className="h-8 w-48" />
        <Skeleton className="h-5 w-72" />
        <div className="mt-4 space-y-3">
          {[0, 1, 2].map((i) => (
            <Skeleton key={i} className="h-20 w-full rounded-xl" />
          ))}
        </div>
      </div>
    )
  }

  const hasStops = stops.length > 0
  const conflictCount = analysis?.conflicts.length ?? 0
  const globalConflictIndices = analysis?.conflicts.flatMap((c) => c.stopIndices) ?? []
  // Map global indices → local day indices for highlighting.
  const dayConflictIndices = dayStops
    .map((s, local) => {
      const global = stops.indexOf(s)
      return globalConflictIndices.includes(global) ? local : -1
    })
    .filter((i) => i >= 0)

  const dayLabels = Array.from({ length: dayCount }, (_, i) => dayTabLabel(draft, i))

  return (
    <div className="space-y-4">
      {/* ----- Header ----- */}
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          {editingTitle ? (
            <form
              onSubmit={(e) => {
                e.preventDefault()
                patchTitle.mutate(titleValue.trim() || 'Untitled plan')
              }}
              className="flex items-center gap-2"
            >
              <Input
                ref={titleInputRef}
                value={titleValue}
                onChange={(e) => setTitleValue(e.target.value)}
                aria-label="Plan title"
                className="text-lg font-semibold"
                autoFocus
              />
              <Button size="sm" type="submit" loading={patchTitle.isPending}>
                Save
              </Button>
              <button
                type="button"
                onClick={() => {
                  setEditingTitle(false)
                  setTitleValue(draft.title)
                }}
                className="p-1 text-sand-400 hover:text-sand-700"
              >
                <X className="size-4" aria-hidden />
              </button>
            </form>
          ) : (
            <button
              type="button"
              onClick={() => {
                setTitleValue(draft.title)
                setEditingTitle(true)
              }}
              className="group flex items-center gap-2 text-2xl font-semibold tracking-tight text-sand-900 hover:text-sand-700"
            >
              {draft.title}
              <Pencil className="size-4 opacity-0 transition-opacity group-hover:opacity-60" aria-hidden />
            </button>
          )}
          <p className="mt-1 text-sm text-sand-500">
            {isTrip
              ? `${dayLabels[0] ?? 'Day 1'} – ${dayLabels[dayCount - 1] ?? `Day ${dayCount}`}`
              : `${clockTime(draft.startsAt)} – ${clockTime(draft.endsAt)}`}
            {draft.citySlug && ` · ${draft.citySlug.replace(/-/g, ' ')}`}
            {' · '}
            {stops.length} {stops.length === 1 ? 'stop' : 'stops'}
            {isTrip && ` · ${dayCount} days`}
          </p>
        </div>

        {/* Keep / status */}
        <div className="flex shrink-0 flex-wrap items-center gap-2">
          {draft.status === 'kept' ? (
            <span className="inline-flex items-center gap-1.5 rounded-lg border border-brand-200 bg-brand-50 px-3 py-1.5 text-sm font-medium text-brand-800">
              <CheckCircle className="size-4" aria-hidden />
              Kept — edits save as you go
            </span>
          ) : showKeepInput ? (
            <form
              className="flex items-center gap-2"
              onSubmit={(e) => {
                e.preventDefault()
                keepDraft.mutate(keepTitle || draft.title)
              }}
            >
              <Input
                autoFocus
                value={keepTitle}
                onChange={(e) => setKeepTitle(e.target.value)}
                placeholder={draft.title}
                aria-label="Name this plan"
                className="w-48"
              />
              <Button
                type="submit"
                loading={keepDraft.isPending}
                disabled={!hasStops}
              >
                <CheckCircle className="size-4" aria-hidden />
                Keep
              </Button>
              <button
                type="button"
                onClick={() => setShowKeepInput(false)}
                className="text-sand-400 hover:text-sand-700"
              >
                <X className="size-4" aria-hidden />
              </button>
            </form>
          ) : (
            <Button
              onClick={() => setShowKeepInput(true)}
              disabled={!hasStops}
              title={hasStops ? undefined : 'Add stops before keeping this plan'}
            >
              <CheckCircle className="size-4" aria-hidden />
              Keep this plan
            </Button>
          )}
        </div>
      </header>

      {mutationError && (
        <p className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700" role="alert">
          {mutationError}
        </p>
      )}

      {/* ----- Day tabs (trips) ----- */}
      {isTrip && (
        <div className="flex flex-wrap items-center gap-1.5">
          {dayLabels.map((label, i) => {
            const count = stops.filter((s) => (s.dayIndex ?? 0) === i).length
            return (
              <button
                key={i}
                type="button"
                onClick={() => setActiveDay(i)}
                aria-pressed={activeDay === i}
                className={cn(
                  'rounded-lg px-3 py-1.5 text-sm font-medium transition-colors',
                  activeDay === i
                    ? 'bg-sand-800 text-sand-50'
                    : 'bg-sand-100 text-sand-700 hover:bg-sand-200',
                )}
              >
                {label}
                {count > 0 && (
                  <span
                    className={cn(
                      'ml-1.5 text-xs',
                      activeDay === i ? 'text-sand-300' : 'text-sand-400',
                    )}
                  >
                    {count}
                  </span>
                )}
              </button>
            )
          })}
          {dayCount < 14 && (
            <button
              type="button"
              onClick={addDay}
              disabled={patchWindow.isPending}
              className="rounded-lg border border-dashed border-sand-300 px-2.5 py-1.5 text-sm text-sand-600 hover:border-sand-400 hover:text-sand-800"
            >
              + Day
            </button>
          )}
        </div>
      )}

      {/* ----- Assist toolbar ----- */}
      <div className="flex flex-wrap items-center gap-2 rounded-xl border border-sand-200 bg-sand-50 px-3 py-2.5">
        <Button
          size="sm"
          variant="secondary"
          onClick={() => checkPlan.mutate()}
          loading={checkPlan.isPending}
          disabled={!hasStops}
          title="Validate your plan for conflicts and gaps"
        >
          <AlertTriangle className="size-3.5" aria-hidden />
          Check My Plan
          {conflictCount > 0 && (
            <span className="ml-1 rounded-full bg-red-500 px-1.5 text-[0.65rem] font-bold text-white">
              {conflictCount}
            </span>
          )}
        </Button>
        <Button
          size="sm"
          variant="secondary"
          onClick={() => optimizePlan.mutate()}
          loading={optimizePlan.isPending}
          disabled={stops.length < 2}
          title="Propose a more efficient stop order"
        >
          <RotateCcw className="size-3.5" aria-hidden />
          Optimize
        </Button>
        <Button
          size="sm"
          variant="secondary"
          onClick={() => openConcierge(true)}
          title="Ask Mado to help with this plan"
        >
          <Sparkles className="size-3.5" aria-hidden />
          Ask Mado
        </Button>

        {/* View toggle */}
        <div className="ml-auto flex items-center gap-1 rounded-lg border border-sand-200 bg-white p-0.5">
          <button
            type="button"
            onClick={() => setView('timeline')}
            aria-pressed={view === 'timeline'}
            className={cn(
              'rounded-md px-2.5 py-1 text-xs font-medium transition-colors',
              view === 'timeline' ? 'bg-sand-200 text-sand-900' : 'text-sand-500 hover:text-sand-800',
            )}
          >
            Timeline
          </button>
          <button
            type="button"
            onClick={() => setView('map')}
            aria-pressed={view === 'map'}
            className={cn(
              'rounded-md px-2.5 py-1 text-xs font-medium transition-colors',
              view === 'map' ? 'bg-sand-200 text-sand-900' : 'text-sand-500 hover:text-sand-800',
            )}
          >
            <MapIcon className="mr-1 inline size-3" aria-hidden />
            Map
          </button>
        </div>
      </div>

      {/* ----- Conflict results ----- */}
      {analysis && (
        <ConflictBanner
          analysis={analysis}
          onFillGap={(gap) => {
            setFillGap(gap)
            fillGapMutation.mutate(gap)
          }}
          onDismiss={() => setAnalysis(null)}
        />
      )}

      {/* ----- Optimize proposal ----- */}
      {optimizeProposal && (
        <OptimizePreview
          current={stops}
          proposal={optimizeProposal}
          onAccept={acceptOptimize}
          onReject={() => setOptimizeProposal(null)}
        />
      )}

      {/* ----- Fill gap proposals ----- */}
      {fillGap && (
        <FillGapCard
          gap={fillGap}
          loading={fillGapMutation.isPending}
          proposals={fillGapMutation.data?.stops ?? null}
          currency={draft.currency}
          onAccept={acceptFillGap}
          onDismiss={() => {
            setFillGap(null)
            fillGapMutation.reset()
          }}
        />
      )}

      {/* ----- Timeline / Map ----- */}
      {view === 'timeline' ? (
        <div>
          {dayStops.length > 0 ? (
            <ol className="mt-2 space-y-0">
              {dayStops.map((stop, index) => (
                <DraggableStop
                  key={stop.id ?? `${stop.experienceId}-${activeDay}-${index}`}
                  stop={stop}
                  index={index}
                  total={dayStops.length}
                  isDragging={dragIndex === index}
                  isDropTarget={dropIndex === index}
                  busy={replaceStops.isPending || removeStop.isPending}
                  detail={stopDetails.get(stop.experienceId)}
                  currency={draft.currency}
                  money={money}
                  conflictIndices={dayConflictIndices}
                  onRemove={() => handleRemove(index)}
                  onMoveUp={() => moveStop(index, index - 1)}
                  onMoveDown={() => moveStop(index, index + 1)}
                  onDragStart={(e) => onDragStart(e, index)}
                  onDragOver={(e) => onDragOver(e, index)}
                  onDrop={onDrop}
                  onDragEnd={onDragEnd}
                />
              ))}
            </ol>
          ) : (
            <div className="mt-6 rounded-xl border border-dashed border-sand-300 bg-sand-50 px-6 py-10 text-center">
              <Plus className="mx-auto size-8 text-sand-400" aria-hidden />
              <p className="mt-2 text-sm font-medium text-sand-700">
                {isTrip ? `No stops on ${dayLabels[activeDay] ?? `day ${activeDay + 1}`}` : 'No stops yet'}
              </p>
              <p className="mt-1 text-sm text-sand-400">
                Search for a place, event or activity to add.
              </p>
            </div>
          )}

          {/* Add Stop button */}
          <button
            type="button"
            onClick={() => setAddStopOpen(true)}
            className={cn(
              'mt-3 flex w-full items-center justify-center gap-2 rounded-xl border border-dashed border-brand-400 bg-brand-50 px-4 py-3 text-sm font-medium text-brand-700 transition-colors hover:bg-brand-100',
            )}
          >
            <Search className="size-4" aria-hidden />
            {isTrip ? `Add a stop to ${dayLabels[activeDay] ?? `day ${activeDay + 1}`}` : 'Add a stop'}
          </button>
        </div>
      ) : (
        <DraftMap draft={draft} stops={stops} />
      )}

      {/* ----- Share (kept plans only) ----- */}
      {draft.status === 'kept' && <PlanShareBar itinerary={draft} />}

      {/* ----- Add Stop overlay ----- */}
      {addStopOpen && (
        <AddStopSearch
          draftId={draftId}
          dayIndex={activeDay}
          existingExperienceIds={stops.map((s) => s.experienceId)}
          onStopAdded={() => {
            setAddStopOpen(false)
            void queryClient.invalidateQueries({ queryKey: ['itinerary', draftId] })
          }}
          onClose={() => setAddStopOpen(false)}
        />
      )}
    </div>
  )
}

// ---- Draggable stop row -------------------------------------------------------

function DraggableStop({
  stop,
  index,
  total,
  isDragging,
  isDropTarget,
  busy,
  detail,
  currency,
  money,
  conflictIndices,
  onRemove,
  onMoveUp,
  onMoveDown,
  onDragStart,
  onDragOver,
  onDrop,
  onDragEnd,
}: {
  stop: PlanStop
  index: number
  total: number
  isDragging: boolean
  isDropTarget: boolean
  busy: boolean
  detail: { media: Array<{ url: string; altText?: string | null }>; venue?: { name?: string | null } | null } | undefined
  currency: string
  money: (amount: number, currency: string) => string
  conflictIndices: number[]
  onRemove: () => void
  onMoveUp: () => void
  onMoveDown: () => void
  onDragStart: (e: React.DragEvent) => void
  onDragOver: (e: React.DragEvent) => void
  onDrop: (e: React.DragEvent) => void
  onDragEnd: () => void
}) {
  const hasConflict = conflictIndices.includes(index)
  const isLast = index === total - 1

  return (
    <li
      onDragOver={onDragOver}
      onDrop={onDrop}
      className={cn(
        'relative flex gap-3 transition-all duration-150',
        isDragging && 'opacity-40',
        isDropTarget && 'rounded-xl ring-2 ring-brand-400 ring-offset-1',
      )}
    >
      <div className="w-14 shrink-0 pt-3 text-right">
        <time dateTime={stop.arriveAt} className="text-sm font-semibold tabular-nums text-sand-700">
          {clockTime(stop.arriveAt)}
        </time>
      </div>

      <div className="flex flex-col items-center">
        <span
          className={cn(
            'mt-3 grid size-6 shrink-0 place-items-center rounded-full text-[0.7rem] font-semibold',
            hasConflict
              ? 'bg-red-500 text-white'
              : stop.isFixedTime
                ? 'bg-brand-600 text-white'
                : 'border border-sand-300 bg-sand-100 text-sand-700',
          )}
        >
          {index + 1}
        </span>
        {!isLast && <span className="mt-1 w-px flex-1 bg-sand-300" aria-hidden />}
      </div>

      <article
        className={cn(
          'mb-2 flex min-w-0 flex-1 gap-3 rounded-xl border bg-sand-100 p-2.5 transition-colors',
          hasConflict ? 'border-red-200 bg-red-50' : 'border-sand-200 hover:border-sand-300',
        )}
      >
        {/* Drag handle only — whole-row drag breaks remove clicks and Firefox drop. */}
        <span
          role="button"
          tabIndex={0}
          draggable
          onDragStart={onDragStart}
          onDragEnd={onDragEnd}
          aria-label={`Drag to reorder ${stop.title}`}
          className="mt-1 shrink-0 cursor-grab touch-none text-sand-400 active:cursor-grabbing"
        >
          <GripVertical className="size-4" aria-hidden />
        </span>

        {detail?.media[0] && (
          <img
            src={detail.media[0].url}
            alt={detail.media[0].altText ?? ''}
            className="size-14 shrink-0 rounded-lg object-cover"
          />
        )}

        <div className="min-w-0 flex-1">
          <div className="flex items-baseline justify-between gap-2">
            <a
              href={`/experiences/${stop.experienceId}`}
              target="_blank"
              rel="noopener noreferrer"
              className="truncate font-medium text-sand-900 hover:underline"
            >
              {stop.title}
            </a>
            {stop.isFixedTime && (
              <span className="shrink-0 rounded-full bg-accent-100 px-2 py-0.5 text-[0.65rem] font-medium text-brand-700">
                Set time
              </span>
            )}
          </div>
          <p className="mt-0.5 flex flex-wrap gap-x-2 text-sm text-sand-500">
            <span className="inline-flex items-center gap-1">
              <Clock className="size-3.5" aria-hidden />
              until {clockTime(stop.departAt)} ({duration(stop.dwellMinutes)})
            </span>
            {stop.estimatedCost > 0 && <span>{money(stop.estimatedCost, currency)}</span>}
            {stop.travelMinutes > 0 && (
              <span className="text-sand-400">{duration(stop.travelMinutes)} travel</span>
            )}
          </p>
          {hasConflict && (
            <p className="mt-1 flex items-center gap-1 text-xs text-red-600">
              <AlertTriangle className="size-3.5 shrink-0" aria-hidden />
              Scheduling conflict — check plan for details
            </p>
          )}
        </div>

        <div className="flex shrink-0 flex-col items-center gap-0.5">
          <button
            type="button"
            aria-label={`Move ${stop.title} up`}
            disabled={index === 0 || busy}
            onClick={onMoveUp}
            className="rounded p-0.5 text-sand-400 hover:bg-sand-200 hover:text-sand-800 disabled:opacity-30"
          >
            <ChevronUp className="size-4" aria-hidden />
          </button>
          <button
            type="button"
            aria-label={`Move ${stop.title} down`}
            disabled={isLast || busy}
            onClick={onMoveDown}
            className="rounded p-0.5 text-sand-400 hover:bg-sand-200 hover:text-sand-800 disabled:opacity-30"
          >
            <ChevronDown className="size-4" aria-hidden />
          </button>
          <button
            type="button"
            aria-label={`Remove ${stop.title}`}
            disabled={busy}
            onClick={(e) => {
              e.preventDefault()
              e.stopPropagation()
              onRemove()
            }}
            className="mt-1 rounded p-0.5 text-sand-400 hover:bg-sand-200 hover:text-red-500 disabled:opacity-30"
          >
            <X className="size-4" aria-hidden />
          </button>
        </div>
      </article>
    </li>
  )
}

// ---- Draft map ------------------------------------------------------------

function DraftMap({ draft, stops }: { draft: Itinerary; stops: PlanStop[] }) {
  // Same contract as RouteGuidance: fetch the routed geometry, then hand it to
  // RouteMap. require() is not available in the Vite ESM browser bundle — that
  // is what crashed the Map tab.
  const { data: route, isLoading, isError } = useQuery({
    queryKey: ['itinerary-route', draft.id],
    queryFn: () => api.itineraryRoute(draft.id),
    enabled: stops.length > 1,
    retry: false,
  })
  const titles = useMemo(() => stops.map((s) => s.title), [stops])

  if (stops.length < 2) {
    return (
      <div className="flex h-48 items-center justify-center rounded-xl border border-sand-200 bg-sand-100 text-sm text-sand-400">
        Add at least two stops to see the map.
      </div>
    )
  }

  if (isLoading) {
    return (
      <div className="flex h-48 items-center justify-center rounded-xl border border-sand-200 bg-sand-100 text-sm text-sand-400">
        Loading map…
      </div>
    )
  }

  if (isError || !route) {
    return (
      <div className="flex h-48 items-center justify-center rounded-xl border border-sand-200 bg-sand-100 text-sm text-sand-400">
        Could not load the route for this plan.
      </div>
    )
  }

  return (
    <div className="h-80 overflow-hidden rounded-xl border border-sand-200">
      <RouteMap route={route} titles={titles} className="h-full w-full" />
    </div>
  )
}

/** How many day tabs to show for this plan. */
function tripDayCount(draft: Itinerary | null | undefined, stops: PlanStop[]): number {
  const fromStops = Math.max(0, ...stops.map((s) => s.dayIndex ?? 0)) + (stops.length ? 1 : 0)
  if (!draft) return Math.max(1, fromStops)
  const start = new Date(draft.startsAt)
  const end = new Date(draft.endsAt)
  // Calendar span in the explorer's local zone (window was created that way).
  const startDay = Date.UTC(start.getFullYear(), start.getMonth(), start.getDate())
  const endDay = Date.UTC(end.getFullYear(), end.getMonth(), end.getDate())
  const fromWindow = Math.floor((endDay - startDay) / 86_400_000) + 1
  const kindTrip = draft.kind === 'trip'
  if (kindTrip || fromWindow > 1 || fromStops > 1) {
    return Math.min(14, Math.max(1, fromWindow, fromStops, kindTrip ? 2 : 1))
  }
  return 1
}

function dayTabLabel(draft: Itinerary, dayIndex: number): string {
  const start = new Date(draft.startsAt)
  const d = new Date(start)
  d.setDate(d.getDate() + dayIndex)
  return d.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' })
}
