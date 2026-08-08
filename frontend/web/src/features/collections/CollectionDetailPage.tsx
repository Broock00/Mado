/**
 * One collection (spec EXP-003).
 *
 * The same page for the curator and for whoever they sent the link to. The
 * difference is only what is editable, which keeps the shared view honest: a
 * curator sees what their reader sees, plus controls.
 *
 * The curator's note on each item is given real prominence. A bare list of
 * places is something search can produce; "go on a Tuesday, the upstairs room
 * is empty" is the thing a person adds, and it is the reason a shared
 * collection is worth opening at all.
 */

import { useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowDown, ArrowUp, Check, Globe, Link2, Lock, Trash2 } from 'lucide-react'

import { api } from '@/lib/api'
import type { CollectionDetail, CollectionVisibility } from '@/lib/types'
import { ExperienceCard } from '@/features/experiences/ExperienceCard'
import { Button, Card, EmptyState, Input } from '@/design-system/primitives'

const VISIBILITY_OPTIONS: {
  value: CollectionVisibility
  label: string
  description: string
  icon: typeof Lock
}[] = [
  {
    value: 'private',
    label: 'Only you',
    description: 'Nobody else can open it.',
    icon: Lock,
  },
  {
    value: 'unlisted',
    label: 'Anyone with the link',
    description: 'Not listed anywhere. Share the link and it works.',
    icon: Link2,
  },
  {
    value: 'public',
    label: 'Public',
    description: 'Listed for other explorers to find. Checked before it appears.',
    icon: Globe,
  },
]

function ShareBar({ collection }: { collection: CollectionDetail }) {
  const queryClient = useQueryClient()
  const [copied, setCopied] = useState(false)

  const setVisibility = useMutation({
    mutationFn: (visibility: CollectionVisibility) =>
      api.updateCollection(collection.id, { visibility }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['collection', collection.id] }),
  })

  const shareUrl = `${window.location.origin}/collections/${collection.id}`

  return (
    <Card className="mt-5 p-5">
      <p className="font-medium text-sand-900">Who can see this</p>
      <div className="mt-3 space-y-2">
        {VISIBILITY_OPTIONS.map((option) => {
          const Icon = option.icon
          const active = collection.visibility === option.value
          return (
            <label
              key={option.value}
              className="flex cursor-pointer items-start gap-3 rounded-lg px-2 py-2 hover:bg-sand-100"
            >
              <input
                type="radio"
                name="visibility"
                checked={active}
                disabled={setVisibility.isPending}
                onChange={() => setVisibility.mutate(option.value)}
                className="mt-1 accent-brand-600"
              />
              <span className="min-w-0">
                <span className="flex items-center gap-1.5 font-medium text-sand-900">
                  <Icon className="size-4" aria-hidden />
                  {option.label}
                </span>
                <span className="block text-sm text-sand-600">{option.description}</span>
              </span>
            </label>
          )
        })}
      </div>

      {setVisibility.isError && (
        <p className="mt-2 text-sm text-red-700" role="alert">
          {(setVisibility.error as Error).message}
        </p>
      )}

      {collection.visibility !== 'private' && (
        <div className="mt-4 flex flex-wrap items-center gap-2">
          <Input readOnly value={shareUrl} aria-label="Share link" className="min-w-[16rem] flex-1" />
          <Button
            variant="secondary"
            onClick={() => {
              void navigator.clipboard.writeText(shareUrl)
              setCopied(true)
              window.setTimeout(() => setCopied(false), 2000)
            }}
          >
            {copied ? <Check className="size-4" aria-hidden /> : null}
            {copied ? 'Copied' : 'Copy link'}
          </Button>
        </div>
      )}

      {collection.visibility === 'public' && collection.moderationStatus === 'flagged' && (
        <p className="mt-3 rounded-lg bg-sand-100 px-3 py-2 text-sm text-sand-700">
          This is waiting on a moderator before it appears in the public list. The
          link still works for anyone you send it to.
        </p>
      )}
    </Card>
  )
}

export function CollectionDetailPage() {
  const { collectionId = '' } = useParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [confirmingDelete, setConfirmingDelete] = useState(false)

  const { data: collection, isLoading, isError, error } = useQuery({
    queryKey: ['collection', collectionId],
    queryFn: () => api.collection(collectionId),
    enabled: Boolean(collectionId),
    retry: false,
  })

  const invalidate = () =>
    void queryClient.invalidateQueries({ queryKey: ['collection', collectionId] })

  const remove = useMutation({
    mutationFn: (experienceId: string) => api.removeFromCollection(collectionId, experienceId),
    onSuccess: invalidate,
  })

  const reorder = useMutation({
    mutationFn: (experienceIds: string[]) => api.reorderCollection(collectionId, experienceIds),
    onSuccess: invalidate,
  })

  const deleteCollection = useMutation({
    mutationFn: () => api.deleteCollection(collectionId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['my-collections'] })
      navigate('/collections', { replace: true })
    },
  })

  if (isLoading) {
    return <div className="mx-auto max-w-4xl px-4 py-16 text-sand-500">Loading…</div>
  }

  if (isError || !collection) {
    return (
      <div className="mx-auto max-w-lg px-4 py-16">
        <EmptyState
          icon={<Lock className="size-8" />}
          title="Not found"
          description={
            (error as Error | undefined)?.message ??
            'This collection does not exist, or it is private.'
          }
          action={
            <Link to="/collections">
              <Button variant="secondary">Browse collections</Button>
            </Link>
          }
        />
      </div>
    )
  }

  const ids = collection.experiences.map((e) => e.id)

  // The list below is a grid, not a column. ExperienceCard sizes its image to
  // the card width, so one per row makes every entry a full screen of
  // photograph and turns a ten-place list into ten scrolls.

  // "Earlier" and "later" rather than "up" and "down": in a wrapping grid the
  // previous item is often to the left, not above.
  function move(index: number, delta: number) {
    const next = [...ids]
    const target = index + delta
    if (target < 0 || target >= next.length) return
    ;[next[index], next[target]] = [next[target], next[index]]
    reorder.mutate(next)
  }

  return (
    <div className="mx-auto w-full max-w-4xl px-4 pb-24 pt-6 sm:px-6">
      <h1 className="text-2xl font-semibold tracking-tight text-sand-900">{collection.title}</h1>
      {collection.description && <p className="mt-1 text-sand-600">{collection.description}</p>}
      <p className="mt-1 text-sm text-sand-500">
        {collection.itemCount} {collection.itemCount === 1 ? 'place' : 'places'}
      </p>

      {collection.isMine && <ShareBar collection={collection} />}

      {collection.experiences.length === 0 ? (
        <Card className="mt-6 p-5">
          <EmptyState
            icon={<Globe className="size-8" />}
            title="Nothing in it yet"
            description={
              collection.isMine
                ? 'Open any place and add it to this collection.'
                : 'The curator has not added anything yet.'
            }
            action={
              collection.isMine ? (
                <Link to="/">
                  <Button>Find something</Button>
                </Link>
              ) : undefined
            }
          />
        </Card>
      ) : (
        <ul className="mt-6 grid gap-5 sm:grid-cols-2">
          {collection.experiences.map((experience, index) => (
            <li key={experience.id}>
              <ExperienceCard experience={experience} />

              {collection.notes[experience.id] && (
                <p className="mt-2 border-l-2 border-brand-300 pl-3 text-sm italic text-sand-700">
                  {collection.notes[experience.id]}
                </p>
              )}

              {collection.isMine && (
                <div className="mt-2 flex items-center gap-1">
                  <Button
                    variant="ghost"
                    size="sm"
                    aria-label={`Move ${experience.title} earlier`}
                    disabled={index === 0 || reorder.isPending}
                    onClick={() => move(index, -1)}
                  >
                    <ArrowUp className="size-4" aria-hidden />
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    aria-label={`Move ${experience.title} later`}
                    disabled={index === ids.length - 1 || reorder.isPending}
                    onClick={() => move(index, 1)}
                  >
                    <ArrowDown className="size-4" aria-hidden />
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    aria-label={`Remove ${experience.title}`}
                    disabled={remove.isPending}
                    onClick={() => remove.mutate(experience.id)}
                  >
                    <Trash2 className="size-4" aria-hidden />
                  </Button>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}

      {collection.isMine && (
        <div className="mt-10 flex flex-wrap items-center gap-3 border-t border-sand-200 pt-6">
          {confirmingDelete ? (
            <>
              <span className="text-sm text-sand-700">
                Delete this collection? Any link you shared will stop working.
              </span>
              <Button
                variant="danger"
                size="sm"
                disabled={deleteCollection.isPending}
                onClick={() => deleteCollection.mutate()}
              >
                {deleteCollection.isPending ? 'Deleting…' : 'Delete'}
              </Button>
              <Button variant="ghost" size="sm" onClick={() => setConfirmingDelete(false)}>
                Cancel
              </Button>
            </>
          ) : (
            <Button variant="ghost" size="sm" onClick={() => setConfirmingDelete(true)}>
              <Trash2 className="size-4" aria-hidden />
              Delete collection
            </Button>
          )}
        </div>
      )}
    </div>
  )
}
