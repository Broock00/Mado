/**
 * A business's own photographs and videos.
 *
 * The grid an explorer sees, and the viewer it opens into. Uploading lives in
 * the manage dashboard, not here — this file renders what a business chose to
 * show, and never anything about editing it.
 *
 * **The viewer is a stage, not a single enlarge.** Opening one tile should let
 * somebody walk the whole gallery with arrows, keys and the peeks beside the
 * active card — forcing a close-and-reopen for every picture is what made the
 * old lightbox feel broken even when Escape and arrow keys technically worked.
 * The layout follows the desktop Stories pattern: one tall active card, dimmed
 * neighbours on either side, circular chevrons between them. Neighbours are the
 * previous and next items in gallery order; the ends do not wrap.
 *
 * **Video is muted and does not autoplay.** A profile page that starts making
 * noise when it loads is the behaviour people install blockers to escape, and a
 * grid of six autoplaying clips is six video decoders running for somebody who
 * came to look at the rooms. The first frame is poster enough: browsers render
 * it from `preload="metadata"`, which is also why no poster image is invented
 * here — nothing decodes video server-side, so a thumbnail would have to be a
 * guess, and a wrong still is worse than the real first frame.
 */

import { useCallback, useEffect, useState } from 'react'
import { ChevronLeft, ChevronRight, Play, X } from 'lucide-react'

import type { BusinessMedia } from '@/lib/types'
import { cn } from '@/lib/utils'

/** A still, or a video showing its first frame. Used in the grid and the stage. */
function Frame({
  item,
  mode,
}: {
  item: BusinessMedia
  mode: 'tile' | 'stage' | 'peek'
}) {
  const fill = mode !== 'tile'
  const className =
    mode === 'tile'
      ? 'size-full object-cover'
      : mode === 'stage'
        ? 'size-full object-contain'
        : 'size-full object-cover'

  if (item.kind === 'video') {
    return (
      <video
        src={item.url}
        // Controls only on the stage: a control bar shrunk into a grid cell or
        // a peek is too small to hit and covers a third of the picture.
        controls={mode === 'stage'}
        muted
        playsInline
        preload="metadata"
        className={className}
      >
        Your browser cannot play this video.
      </video>
    )
  }

  return (
    <img
      src={item.url}
      alt={item.caption ?? ''}
      loading={mode === 'tile' ? 'lazy' : 'eager'}
      width={item.width ?? undefined}
      height={item.height ?? undefined}
      className={cn(className, fill && 'bg-black')}
      draggable={false}
    />
  )
}

function Peek({
  item,
  side,
  onOpen,
}: {
  item: BusinessMedia
  side: 'left' | 'right'
  onOpen: () => void
}) {
  return (
    <button
      type="button"
      onClick={onOpen}
      aria-label={
        item.caption ??
        (side === 'left'
          ? item.kind === 'video'
            ? 'Open previous video'
            : 'Open previous photo'
          : item.kind === 'video'
            ? 'Open next video'
            : 'Open next photo')
      }
      className={cn(
        // Hidden on narrow screens: peeks need horizontal room, and the
        // chevrons plus swipe-zones already cover back-and-forth there.
        'relative hidden h-[min(62vh,420px)] w-[min(18vw,140px)] shrink-0 overflow-hidden rounded-2xl',
        'bg-neutral-900 ring-1 ring-white/10 transition-transform hover:scale-[1.02]',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white/70',
        'lg:block',
        side === 'left' ? 'origin-right' : 'origin-left',
      )}
    >
      <Frame item={item} mode="peek" />
      <span className="pointer-events-none absolute inset-0 bg-black/45" aria-hidden />
      {item.kind === 'video' && (
        <span
          className="pointer-events-none absolute inset-0 grid place-items-center text-white/90"
          aria-hidden
        >
          <Play className="size-6 translate-x-px fill-current" />
        </span>
      )}
    </button>
  )
}

/**
 * Gallery order is `sortOrder`, not array insertion. Walking peeks by index
 * into an unsorted list puts the wrong neighbour on each side.
 */
export function ordered(items: BusinessMedia[]): BusinessMedia[] {
  return [...items].sort((a, b) => a.sortOrder - b.sortOrder)
}

/**
 * Full-screen stage over the gallery. Exported so the manage grid can open the
 * same walk-through without reimplementing navigation — two viewers that step
 * differently would be the next bug.
 *
 * Callers must pass items already in gallery order (`ordered`). Peeks and steps
 * walk by index: left is the previous item, right the next, and the ends do not
 * wrap — wrapping made both peeks the same picture on a short gallery.
 */
export function GalleryViewer({
  items,
  index,
  onClose,
  onGoTo,
}: {
  items: BusinessMedia[]
  index: number
  onClose: () => void
  onGoTo: (next: number) => void
}) {
  const item = items[index]
  const count = items.length
  const hasPrev = index > 0
  const hasNext = index < count - 1
  const prev = hasPrev ? items[index - 1] : null
  const next = hasNext ? items[index + 1] : null

  const step = useCallback(
    (delta: number) => {
      const target = index + delta
      if (target < 0 || target >= count) return
      onGoTo(target)
    },
    [count, index, onGoTo],
  )

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
      if (event.key === 'ArrowRight') step(1)
      if (event.key === 'ArrowLeft') step(-1)
    }
    document.addEventListener('keydown', onKey)
    const { overflow } = document.body.style
    document.body.style.overflow = 'hidden'
    return () => {
      document.removeEventListener('keydown', onKey)
      document.body.style.overflow = overflow
    }
  }, [onClose, step])

  if (!item) return null

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={item.caption ?? 'Gallery'}
      className="fixed inset-0 z-50 flex flex-col bg-black"
    >
      <div className="flex items-center justify-end px-3 py-3 sm:px-5">
        <button
          type="button"
          onClick={onClose}
          aria-label="Close"
          className="grid size-11 place-items-center rounded-full text-white/90 transition-colors hover:bg-white/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white/60"
        >
          <X className="size-7" aria-hidden strokeWidth={1.75} />
        </button>
      </div>

      <div className="relative flex min-h-0 flex-1 items-center justify-center gap-3 px-2 pb-8 sm:gap-5 sm:px-6">
        {prev ? (
          <Peek item={prev} side="left" onOpen={() => onGoTo(index - 1)} />
        ) : (
          // Keep the stage centred when there is no earlier neighbour.
          <div className="hidden w-[min(18vw,140px)] shrink-0 lg:block" aria-hidden />
        )}

        {hasPrev ? (
          <button
            type="button"
            onClick={() => step(-1)}
            aria-label="Previous"
            className="absolute left-2 z-20 grid size-11 place-items-center rounded-full bg-white text-neutral-900 shadow-sm transition-transform hover:scale-105 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white/80 sm:left-4 lg:static lg:shrink-0"
          >
            <ChevronLeft className="size-6" aria-hidden />
          </button>
        ) : (
          <div className="hidden size-11 shrink-0 lg:block" aria-hidden />
        )}

        <figure
          key={item.id}
          className="relative flex h-[min(78vh,720px)] w-full max-w-[min(92vw,420px)] flex-col overflow-hidden rounded-2xl bg-neutral-950 ring-1 ring-white/10 motion-safe:animate-[managePanelIn_220ms_var(--ease-out-soft)] sm:max-w-[min(42vw,440px)]"
        >
          {/* One segment per item — position in the gallery, not a Stories
              auto-timer. Filling only the current bar tells somebody how far
              through they are without inventing a countdown nobody asked for. */}
          {count > 1 && (
            <div className="absolute inset-x-0 top-0 z-10 flex gap-1 px-3 pt-3" aria-hidden>
              {items.map((entry, i) => (
                <span
                  key={entry.id}
                  className="h-0.5 flex-1 overflow-hidden rounded-full bg-white/30"
                >
                  <span
                    className={cn(
                      'block h-full rounded-full bg-white transition-all',
                      i === index ? 'w-full' : i < index ? 'w-full opacity-70' : 'w-0',
                    )}
                  />
                </span>
              ))}
            </div>
          )}

          <div className="relative min-h-0 flex-1">
            <Frame item={item} mode="stage" />

            {/* Invisible hit zones on the stage itself — phones have no peeks,
                and a chevron alone is easy to miss with a thumb. Video controls
                sit in the lower band, so the zones stop above them. */}
            {hasPrev && (
              <button
                type="button"
                aria-label="Previous"
                onClick={() => step(-1)}
                className="absolute inset-y-10 left-0 w-1/3 lg:hidden"
              />
            )}
            {hasNext && (
              <button
                type="button"
                aria-label="Next"
                onClick={() => step(1)}
                className="absolute inset-y-10 right-0 w-1/3 lg:hidden"
              />
            )}
          </div>

          {(item.caption || count > 1) && (
            <figcaption className="space-y-1 border-t border-white/10 px-4 py-3">
              {item.caption && (
                <p className="text-sm leading-snug text-white/90">{item.caption}</p>
              )}
              {count > 1 && (
                <p className="text-xs text-white/50">
                  {index + 1} of {count}
                </p>
              )}
            </figcaption>
          )}
        </figure>

        {hasNext ? (
          <button
            type="button"
            onClick={() => step(1)}
            aria-label="Next"
            className="absolute right-2 z-20 grid size-11 place-items-center rounded-full bg-white text-neutral-900 shadow-sm transition-transform hover:scale-105 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white/80 sm:right-4 lg:static lg:shrink-0"
          >
            <ChevronRight className="size-6" aria-hidden />
          </button>
        ) : (
          <div className="hidden size-11 shrink-0 lg:block" aria-hidden />
        )}

        {next ? (
          <Peek item={next} side="right" onOpen={() => onGoTo(index + 1)} />
        ) : (
          <div className="hidden w-[min(18vw,140px)] shrink-0 lg:block" aria-hidden />
        )}
      </div>
    </div>
  )
}

export function BusinessGallery({ items }: { items: BusinessMedia[] }) {
  const gallery = ordered(items)
  const [openIndex, setOpenIndex] = useState<number | null>(null)

  const close = useCallback(() => setOpenIndex(null), [])
  const goTo = useCallback((next: number) => setOpenIndex(next), [])

  return (
    <>
      <ul className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4">
        {gallery.map((item, index) => (
          <li key={item.id}>
            <button
              type="button"
              onClick={() => setOpenIndex(index)}
              className="group relative block aspect-square w-full overflow-hidden rounded-xl bg-sand-200 ring-1 ring-sand-200 transition-transform hover:scale-[1.01] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500"
              aria-label={item.caption ?? (item.kind === 'video' ? 'Play video' : 'View photo')}
            >
              <Frame item={item} mode="tile" />

              {item.kind === 'video' && (
                <span
                  className="pointer-events-none absolute inset-0 grid place-items-center bg-black/20"
                  aria-hidden
                >
                  <span className="grid size-11 place-items-center rounded-full bg-black/55 text-white backdrop-blur-sm">
                    <Play className="size-5 translate-x-px fill-current" />
                  </span>
                </span>
              )}

              {item.caption && (
                <span className="pointer-events-none absolute inset-x-0 bottom-0 line-clamp-2 bg-gradient-to-t from-black/75 to-transparent px-2.5 pb-2 pt-6 text-left text-xs text-white">
                  {item.caption}
                </span>
              )}
            </button>
          </li>
        ))}
      </ul>

      {openIndex !== null && gallery[openIndex] && (
        <GalleryViewer
          items={gallery}
          index={openIndex}
          onClose={close}
          onGoTo={goTo}
        />
      )}
    </>
  )
}
