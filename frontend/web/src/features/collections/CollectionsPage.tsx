/**
 * Collections (spec EXP-003).
 *
 * Two lists on one page, in the order that matches how people arrive: yours
 * first when you are signed in, because you came to find something you made;
 * public ones below, because that is browsing.
 *
 * The visibility of each is on its card. A curator needs to know at a glance
 * which of their lists is public - discovering that a private list was shared
 * by opening it one at a time is exactly the wrong way round.
 */

import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Globe, Link2, Lock, Plus } from 'lucide-react'

import { api } from '@/lib/api'
import { useAppStore } from '@/app/store'
import type { CollectionCard, CollectionVisibility } from '@/lib/types'
import { Badge, Button, Card, EmptyState, Input, SectionHeading } from '@/design-system/primitives'

const VISIBILITY_ICON = {
  private: Lock,
  unlisted: Link2,
  public: Globe,
} as const

const VISIBILITY_LABEL: Record<CollectionVisibility, string> = {
  private: 'Only you',
  unlisted: 'Anyone with the link',
  public: 'Public',
}

export function CollectionCardTile({ collection }: { collection: CollectionCard }) {
  const Icon = VISIBILITY_ICON[collection.visibility] ?? Lock
  const previews = collection.previewImageUrls.slice(0, 4)
  const withheld = collection.isMine && collection.moderationStatus === 'flagged'

  return (
    <Link to={`/collections/${collection.id}`} className="group block">
      <Card className="overflow-hidden transition-shadow hover:shadow-lifted">
        <div className="grid h-28 grid-cols-4 gap-px bg-sand-200">
          {previews.length > 0 ? (
            previews.map((url, index) => (
              <img
                key={`${url}-${index}`}
                src={url}
                alt=""
                loading="lazy"
                className="size-full object-cover"
              />
            ))
          ) : (
            <div className="col-span-4 grid place-items-center bg-sand-100 text-sm text-sand-500">
              Nothing in it yet
            </div>
          )}
        </div>

        <div className="p-4">
          <p className="font-medium text-sand-900 group-hover:underline">{collection.title}</p>
          {collection.description && (
            <p className="mt-0.5 line-clamp-2 text-sm text-sand-600">{collection.description}</p>
          )}
          <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-sand-500">
            <span className="inline-flex items-center gap-1">
              <Icon className="size-3.5" aria-hidden />
              {VISIBILITY_LABEL[collection.visibility] ?? collection.visibility}
            </span>
            <span>·</span>
            <span>
              {collection.itemCount} {collection.itemCount === 1 ? 'place' : 'places'}
            </span>
            {/* Shown only to the owner, and only when it changes what they can
                expect: their public list is not appearing in the directory. */}
            {withheld && <Badge tone="neutral">Awaiting review</Badge>}
          </div>
        </div>
      </Card>
    </Link>
  )
}

function NewCollection() {
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(false)
  const [title, setTitle] = useState('')

  const create = useMutation({
    mutationFn: () => api.createCollection({ title: title.trim(), citySlug: 'addis-ababa' }),
    onSuccess: () => {
      setTitle('')
      setOpen(false)
      void queryClient.invalidateQueries({ queryKey: ['my-collections'] })
    },
  })

  if (!open) {
    return (
      <Button onClick={() => setOpen(true)}>
        <Plus className="size-4" aria-hidden />
        New collection
      </Button>
    )
  }

  return (
    <form
      className="flex flex-wrap items-center gap-2"
      onSubmit={(event) => {
        event.preventDefault()
        if (title.trim()) create.mutate()
      }}
    >
      <Input
        autoFocus
        aria-label="Collection name"
        placeholder="Best coffee, rainy-day ideas…"
        value={title}
        onChange={(event) => setTitle(event.target.value)}
        className="min-w-[14rem]"
      />
      <Button type="submit" disabled={create.isPending || !title.trim()}>
        {create.isPending ? 'Creating…' : 'Create'}
      </Button>
      <Button variant="ghost" onClick={() => setOpen(false)}>
        Cancel
      </Button>
      {create.isError && (
        <p className="w-full text-sm text-red-700" role="alert">
          {(create.error as Error).message}
        </p>
      )}
    </form>
  )
}

export function CollectionsPage() {
  const user = useAppStore((s) => s.user)

  const { data: mine } = useQuery({
    queryKey: ['my-collections'],
    queryFn: () => api.myCollections(),
    enabled: Boolean(user),
  })

  const { data: publicOnes, isLoading } = useQuery({
    queryKey: ['public-collections'],
    queryFn: () => api.publicCollections('addis-ababa'),
  })

  return (
    <div className="mx-auto w-full max-w-5xl px-4 pb-24 pt-6 sm:px-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-sand-900">Collections</h1>
          <p className="mt-1 text-sand-600">
            Themed lists of places. Yours are private until you say otherwise.
          </p>
        </div>
        {user && <NewCollection />}
      </div>

      {user && (
        <section className="mt-8">
          <SectionHeading title="Yours" subtitle="Only you can see these unless you share them." />
          {mine && mine.length > 0 ? (
            <div className="mt-3 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {mine.map((collection) => (
                <CollectionCardTile key={collection.id} collection={collection} />
              ))}
            </div>
          ) : (
            <Card className="mt-3 p-5">
              <EmptyState
                icon={<Plus className="size-8" />}
                title="No collections yet"
                description="Group places around a theme - the coffee shops you actually go to, or what you would show a visitor."
              />
            </Card>
          )}
        </section>
      )}

      <section className="mt-10">
        <SectionHeading
          title="From other explorers"
          subtitle="Collections people have chosen to share publicly."
        />
        {isLoading && <Card className="mt-3 p-5 text-sm text-sand-500">Loading…</Card>}
        {publicOnes && publicOnes.length > 0 ? (
          <div className="mt-3 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {publicOnes.map((collection) => (
              <CollectionCardTile key={collection.id} collection={collection} />
            ))}
          </div>
        ) : (
          !isLoading && (
            <Card className="mt-3 p-5">
              <EmptyState
                icon={<Globe className="size-8" />}
                title="Nothing shared yet"
                description="Public collections from other explorers will appear here."
              />
            </Card>
          )
        )}
      </section>

      {!user && (
        <p className="mt-8 text-center text-sm text-sand-600">
          <Link to="/signin" className="text-brand-700 underline">
            Sign in
          </Link>{' '}
          to start your own.
        </p>
      )}
    </div>
  )
}
