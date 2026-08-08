/**
 * "Add to collection", from a place's own page.
 *
 * This is where curating actually happens: you are looking at somewhere, and
 * you decide it belongs with other things. Making people go to a collection and
 * search back for the place would mean nobody ever did it.
 *
 * Creating a new collection is in the same menu as adding to an existing one,
 * because the first time anyone uses this they have no collections, and sending
 * them elsewhere to make one loses the thing they were looking at.
 */

import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, FolderPlus, Plus } from 'lucide-react'

import { api } from '@/lib/api'
import { useAppStore } from '@/app/store'
import { Button, Input } from '@/design-system/primitives'
import { cn } from '@/lib/utils'

export function AddToCollection({
  experienceId,
  citySlug,
}: {
  experienceId: string
  citySlug?: string | null
}) {
  const user = useAppStore((s) => s.user)
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(false)
  const [creating, setCreating] = useState(false)
  const [title, setTitle] = useState('')
  const panelRef = useRef<HTMLDivElement>(null)

  const { data: collections } = useQuery({
    queryKey: ['my-collections'],
    queryFn: () => api.myCollections(),
    enabled: Boolean(user) && open,
  })

  const add = useMutation({
    mutationFn: (collectionId: string) => api.addToCollection(collectionId, experienceId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['my-collections'] })
      setOpen(false)
    },
  })

  const createAndAdd = useMutation({
    mutationFn: async () => {
      const created = await api.createCollection({
        title: title.trim(),
        citySlug: citySlug ?? undefined,
      })
      return api.addToCollection(created.id, experienceId)
    },
    onSuccess: () => {
      setTitle('')
      setCreating(false)
      setOpen(false)
      void queryClient.invalidateQueries({ queryKey: ['my-collections'] })
    },
  })

  useEffect(() => {
    if (!open) return
    function onPointerDown(event: PointerEvent) {
      if (!panelRef.current?.contains(event.target as Node)) setOpen(false)
    }
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') setOpen(false)
    }
    document.addEventListener('pointerdown', onPointerDown)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [open])

  if (!user) return null

  const failure = (add.error ?? createAndAdd.error) as Error | null

  return (
    <div className="relative" ref={panelRef}>
      <Button variant="secondary" onClick={() => setOpen((current) => !current)}>
        <FolderPlus className="size-4" aria-hidden />
        Add to collection
      </Button>

      {open && (
        <div className="absolute right-0 z-40 mt-2 w-72 overflow-hidden rounded-xl border border-sand-200 bg-white shadow-lifted">
          <ul className="max-h-64 overflow-y-auto">
            {(collections ?? []).map((collection) => {
              // Whether this place is already in it is not on the card payload,
              // so the API answers with a conflict and the menu says so rather
              // than pretending the click did something.
              const already =
                add.isError &&
                (add.error as { code?: string } | null)?.code === 'ALREADY_IN_COLLECTION' &&
                add.variables === collection.id
              return (
                <li key={collection.id}>
                  <button
                    type="button"
                    disabled={add.isPending}
                    onClick={() => add.mutate(collection.id)}
                    className={cn(
                      'flex w-full items-center justify-between gap-2 px-4 py-2.5 text-left text-sm hover:bg-sand-100',
                      already && 'text-sand-500',
                    )}
                  >
                    <span className="truncate">{collection.title}</span>
                    {already ? (
                      <span className="shrink-0 text-xs">already in</span>
                    ) : (
                      <Check className="size-4 shrink-0 opacity-0" aria-hidden />
                    )}
                  </button>
                </li>
              )
            })}
            {collections && collections.length === 0 && !creating && (
              <li className="px-4 py-3 text-sm text-sand-500">No collections yet.</li>
            )}
          </ul>

          <div className="border-t border-sand-200 p-2">
            {creating ? (
              <form
                className="space-y-2"
                onSubmit={(event) => {
                  event.preventDefault()
                  if (title.trim()) createAndAdd.mutate()
                }}
              >
                <Input
                  autoFocus
                  aria-label="New collection name"
                  placeholder="Name it"
                  value={title}
                  onChange={(event) => setTitle(event.target.value)}
                />
                <div className="flex gap-2">
                  <Button
                    type="submit"
                    size="sm"
                    disabled={createAndAdd.isPending || !title.trim()}
                  >
                    {createAndAdd.isPending ? 'Adding…' : 'Create and add'}
                  </Button>
                  <Button variant="ghost" size="sm" onClick={() => setCreating(false)}>
                    Cancel
                  </Button>
                </div>
              </form>
            ) : (
              <button
                type="button"
                onClick={() => setCreating(true)}
                className="flex w-full items-center gap-2 rounded-lg px-2 py-2 text-left text-sm text-sand-700 hover:bg-sand-100"
              >
                <Plus className="size-4" aria-hidden />
                New collection
              </button>
            )}
          </div>

          {failure && (
            <p className="border-t border-sand-200 px-4 py-2 text-xs text-red-700" role="alert">
              {failure.message}
            </p>
          )}
        </div>
      )}
    </div>
  )
}
