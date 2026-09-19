/**
 * "Add to plan" action — available from any experience surface.
 *
 * Mirrors AddToCollection in shape: a button that opens a menu listing the
 * explorer's active drafts, plus a "Start a plan" option when none exist.
 * Adding immediately appends the stop and closes — no intermediate form.
 *
 * When the explorer has exactly one active draft (the common case) the button
 * is direct: one tap, one stop added, no menu needed.
 */

import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { CalendarDays, Check, Plus } from 'lucide-react'

import { api } from '@/lib/api'
import { useAppStore } from '@/app/store'
import { Button } from '@/design-system/primitives'
import { dayLabel } from './timeline-format'

interface Props {
  experienceId: string
  eventInstanceId?: string | null
  size?: 'sm' | 'md'
}

export function AddToPlan({ experienceId, eventInstanceId, size = 'md' }: Props) {
  const user = useAppStore((s) => s.user)
  const activeDraftId = useAppStore((s) => s.activeDraftId)
  const setActiveDraftId = useAppStore((s) => s.setActiveDraftId)
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const [open, setOpen] = useState(false)
  const [added, setAdded] = useState<string | null>(null) // draft id just added to
  const panelRef = useRef<HTMLDivElement>(null)

  const { data: drafts } = useQuery({
    queryKey: ['itinerary-drafts'],
    queryFn: () => api.drafts(),
    enabled: Boolean(user) && open,
  })

  const appendStop = useMutation({
    mutationFn: ({ draftId }: { draftId: string }) =>
      api.appendStop(draftId, experienceId, eventInstanceId),
    onSuccess: (_, { draftId }) => {
      setAdded(draftId)
      setActiveDraftId(draftId)
      void queryClient.invalidateQueries({ queryKey: ['itinerary', draftId] })
      void queryClient.invalidateQueries({ queryKey: ['itinerary-drafts'] })
      setTimeout(() => {
        setOpen(false)
        setAdded(null)
      }, 1200)
    },
  })

  const createAndAdd = useMutation({
    mutationFn: async () => {
      const now = new Date()
      const start = new Date(now)
      const end = new Date(now)
      if (now.getHours() < 9) start.setHours(9, 0, 0, 0)
      end.setHours(22, 0, 0, 0)
      if (end <= start) {
        start.setDate(start.getDate() + 1)
        end.setDate(end.getDate() + 1)
      }
      const draft = await api.createDraft({
        title: 'New plan',
        startsAt: start.toISOString(),
        endsAt: end.toISOString(),
      })
      return api.appendStop(draft.id, experienceId, eventInstanceId).then(() => draft)
    },
    onSuccess: (draft) => {
      setActiveDraftId(draft.id)
      void queryClient.invalidateQueries({ queryKey: ['itinerary-drafts'] })
      void queryClient.invalidateQueries({ queryKey: ['itinerary', draft.id] })
      navigate('/plans')
    },
  })

  // Close on outside click / Escape
  useEffect(() => {
    if (!open) return
    function onPointerDown(e: PointerEvent) {
      if (!panelRef.current?.contains(e.target as Node)) setOpen(false)
    }
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('pointerdown', onPointerDown)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [open])

  if (!user) return null // only signed-in explorers can plan

  // One active draft — direct action, no menu
  if (activeDraftId && !open) {
    return (
      <Button
        size={size}
        variant="secondary"
        onClick={() => appendStop.mutate({ draftId: activeDraftId })}
        loading={appendStop.isPending}
        title="Add to your active plan"
      >
        {appendStop.isSuccess ? (
          <Check className="size-3.5" aria-hidden />
        ) : (
          <CalendarDays className="size-3.5" aria-hidden />
        )}
        {appendStop.isSuccess ? 'Added' : 'Add to plan'}
      </Button>
    )
  }

  return (
    <div className="relative" ref={panelRef}>
      <Button
        size={size}
        variant="secondary"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        aria-haspopup="listbox"
      >
        <CalendarDays className="size-3.5" aria-hidden />
        Add to plan
      </Button>

      {open && (
        <div
          role="listbox"
          aria-label="Choose a plan"
          className="absolute right-0 top-full z-50 mt-1 w-64 rounded-xl border border-sand-200 bg-white shadow-xl"
        >
          {(!drafts || drafts.length === 0) ? (
            <div className="p-3">
              <p className="text-sm text-sand-600">You don't have a plan in progress.</p>
              <Button
                size="sm"
                className="mt-2 w-full"
                onClick={() => createAndAdd.mutate()}
                loading={createAndAdd.isPending}
              >
                <Plus className="size-3.5" aria-hidden />
                Start a plan with this
              </Button>
            </div>
          ) : (
            <>
              <p className="border-b border-sand-100 px-3 py-2 text-xs font-medium text-sand-500">
                Add to a plan in progress
              </p>
              <ul className="py-1">
                {drafts.map((draft) => (
                  <li key={draft.id}>
                    <button
                      type="button"
                      role="option"
                      aria-selected={added === draft.id}
                      disabled={appendStop.isPending}
                      onClick={() => appendStop.mutate({ draftId: draft.id })}
                      className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm hover:bg-sand-50 disabled:opacity-50"
                    >
                      {added === draft.id ? (
                        <Check className="size-4 shrink-0 text-brand-600" aria-hidden />
                      ) : (
                        <CalendarDays className="size-4 shrink-0 text-sand-400" aria-hidden />
                      )}
                      <div className="min-w-0">
                        <p className="truncate font-medium text-sand-900">{draft.title}</p>
                        <p className="text-xs text-sand-400">
                          {dayLabel(draft.startsAt)} · {draft.stops.length} stops
                        </p>
                      </div>
                    </button>
                  </li>
                ))}
              </ul>
              <div className="border-t border-sand-100 p-2">
                <Button
                  size="sm"
                  variant="secondary"
                  className="w-full"
                  onClick={() => createAndAdd.mutate()}
                  loading={createAndAdd.isPending}
                >
                  <Plus className="size-3.5" aria-hidden />
                  New plan with this
                </Button>
              </div>
            </>
          )}
        </div>
      )}
    </div>
  )
}
