/**
 * Uploading photos and videos to a business profile.
 *
 * The reordering-and-removing half of the gallery; `BusinessGallery` is the
 * read-only half an explorer sees. Separate files because they share nothing
 * but the type — this one is a management grid, that one is a lightbox.
 *
 * Uploading itself is `AddMedia`, which this shares with the gallery tab on the
 * profile. Somebody looking at their pictures reaches for "add" right there,
 * and sending them to another page to do it is the friction that made this
 * feature look like it had no upload at all.
 *
 * **Tiles open the same stage the public page uses.** A manage grid that only
 * deleted and never showed meant owners checked their upload by leaving manage
 * for the public tab — and still could not walk between pictures once there.
 */

import { useCallback, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Film, ImagePlus, Trash2 } from 'lucide-react'

import { api } from '@/lib/api'
import type { BusinessMedia } from '@/lib/types'
import { Button, Card, EmptyState } from '@/design-system/primitives'
import { AddMedia, MEDIA_HINT } from './AddMedia'
import { GalleryViewer, ordered } from './BusinessGallery'

function Tile({
  item,
  businessId,
  slug,
  onOpen,
}: {
  item: BusinessMedia
  businessId: string
  slug: string
  onOpen: () => void
}) {
  const queryClient = useQueryClient()
  const [confirming, setConfirming] = useState(false)

  const remove = useMutation({
    mutationFn: () => api.removeBusinessMedia(businessId, item.id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['business-gallery', businessId] })
      // The public page holds its own copy of the gallery, fetched with the
      // profile. Without this the owner deletes a photo and still sees it on
      // their own profile until something else happens to refetch.
      void queryClient.invalidateQueries({ queryKey: ['business', slug] })
    },
  })

  return (
    <li className="group relative aspect-square overflow-hidden rounded-xl bg-sand-200 ring-1 ring-sand-200">
      <button
        type="button"
        onClick={onOpen}
        className="size-full focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-brand-500"
        aria-label={item.caption ?? (item.kind === 'video' ? 'View video' : 'View photo')}
      >
        {item.kind === 'video' ? (
          <video
            src={item.url}
            muted
            playsInline
            preload="metadata"
            className="size-full object-cover"
          />
        ) : (
          <img
            src={item.url}
            alt={item.caption ?? ''}
            loading="lazy"
            className="size-full object-cover"
          />
        )}
      </button>

      {item.kind === 'video' && (
        <span
          className="pointer-events-none absolute left-2 top-2 grid size-7 place-items-center rounded-full bg-black/60 text-white"
          aria-hidden
        >
          <Film className="size-3.5" />
        </span>
      )}

      {/* Two presses to delete, and the second one says what it does. There is
          no undo — the row is gone and the ordering closes over it — so a
          single mis-tap on a phone should not be able to spend it. */}
      <div className="absolute inset-x-0 bottom-0 z-10 flex justify-end p-2">
        {confirming ? (
          <div className="flex w-full gap-1.5">
            <Button
              size="sm"
              variant="danger"
              className="flex-1"
              disabled={remove.isPending}
              onClick={() => remove.mutate()}
            >
              {remove.isPending ? 'Removing…' : 'Remove'}
            </Button>
            <Button size="sm" variant="secondary" onClick={() => setConfirming(false)}>
              Keep
            </Button>
          </div>
        ) : (
          <button
            type="button"
            onClick={(event) => {
              event.stopPropagation()
              setConfirming(true)
            }}
            aria-label={`Remove ${item.caption ?? (item.kind === 'video' ? 'video' : 'photo')}`}
            className="grid size-8 place-items-center rounded-full bg-black/60 text-white opacity-0 transition-opacity hover:bg-black/80 focus-visible:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white/70 group-hover:opacity-100"
          >
            <Trash2 className="size-4" aria-hidden />
          </button>
        )}
      </div>
    </li>
  )
}

export function BusinessGallerySection({
  businessId,
  slug,
}: {
  businessId: string
  slug: string
}) {
  const { data: items = [], isLoading } = useQuery({
    queryKey: ['business-gallery', businessId],
    queryFn: () => api.businessGallery(businessId),
    enabled: Boolean(businessId),
  })
  const gallery = ordered(items)
  const [openIndex, setOpenIndex] = useState<number | null>(null)

  const close = useCallback(() => setOpenIndex(null), [])
  const goTo = useCallback((next: number) => setOpenIndex(next), [])

  // If the open item was deleted while the stage is up, close rather than
  // pointing at a neighbour the owner did not ask to see.
  const openItem = openIndex !== null ? gallery[openIndex] : null

  return (
    <Card className="space-y-5 p-5 sm:p-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold tracking-tight text-sand-900">
            Photos &amp; videos
          </h2>
          <p className="mt-0.5 text-sm text-sand-500">
            The place itself — the rooms, the food, the view. These sit in their own tab
            on your profile, and stay there whether or not you have anything on.
          </p>
        </div>

        <AddMedia businessId={businessId} slug={slug} label="Add" />
      </div>

      {isLoading ? (
        <div className="grid grid-cols-3 gap-2 sm:grid-cols-4">
          {Array.from({ length: 4 }).map((_, index) => (
            <div key={index} className="aspect-square animate-pulse rounded-xl bg-sand-200" />
          ))}
        </div>
      ) : gallery.length === 0 ? (
        <EmptyState
          icon={<ImagePlus className="size-8" />}
          title="No photos yet"
          description={MEDIA_HINT}
        />
      ) : (
        <ul className="grid grid-cols-3 gap-2 sm:grid-cols-4">
          {gallery.map((item, index) => (
            <Tile
              key={item.id}
              item={item}
              businessId={businessId}
              slug={slug}
              onOpen={() => setOpenIndex(index)}
            />
          ))}
        </ul>
      )}

      {openItem && openIndex !== null && (
        <GalleryViewer items={gallery} index={openIndex} onClose={close} onGoTo={goTo} />
      )}
    </Card>
  )
}
