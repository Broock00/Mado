/**
 * A business's own photographs and videos.
 *
 * The grid an explorer sees, and the lightbox it opens into. Uploading lives in
 * the manage dashboard, not here — this file renders what a business chose to
 * show, and never anything about editing it.
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
import { Play, X } from 'lucide-react'

import type { BusinessMedia } from '@/lib/types'

/** A still, or a video showing its first frame. Used in the grid and enlarged. */
function Frame({ item, enlarged }: { item: BusinessMedia; enlarged?: boolean }) {
  if (item.kind === 'video') {
    return (
      <video
        src={item.url}
        // Controls only when enlarged: a control bar shrunk into a grid cell is
        // too small to hit and covers a third of the picture.
        controls={enlarged}
        muted
        playsInline
        preload="metadata"
        className={enlarged ? 'max-h-[80vh] w-auto max-w-full rounded-xl' : 'size-full object-cover'}
      >
        {/* Named rather than left to a broken player. The one thing worse than
            not seeing the video is not being told there was one. */}
        Your browser cannot play this video.
      </video>
    )
  }

  return (
    <img
      src={item.url}
      // The caption doubles as alternative text. Empty rather than invented when
      // there is none: a screen reader announcing a filename is noise, and
      // `alt=""` correctly marks it as decorative.
      alt={item.caption ?? ''}
      loading="lazy"
      // Only when both are known. A video has neither, and asserting a ratio for
      // one would reserve the wrong space and make it jump on load.
      width={item.width ?? undefined}
      height={item.height ?? undefined}
      className={
        enlarged ? 'max-h-[80vh] w-auto max-w-full rounded-xl' : 'size-full object-cover'
      }
    />
  )
}

function Lightbox({
  item,
  onClose,
  onStep,
}: {
  item: BusinessMedia
  onClose: () => void
  onStep: (delta: number) => void
}) {
  // Escape closes and the arrows move. Bound on the document rather than the
  // dialog because focus lands on the video element once it has controls, and a
  // handler on a wrapper never sees the key from there.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
      if (event.key === 'ArrowRight') onStep(1)
      if (event.key === 'ArrowLeft') onStep(-1)
    }
    document.addEventListener('keydown', onKey)
    // The page behind must not scroll while this is over it — on a phone the
    // drag that should move between pictures otherwise scrolls the profile.
    const { overflow } = document.body.style
    document.body.style.overflow = 'hidden'
    return () => {
      document.removeEventListener('keydown', onKey)
      document.body.style.overflow = overflow
    }
  }, [onClose, onStep])

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={item.caption ?? 'Enlarged media'}
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/85 p-4"
      onClick={onClose}
    >
      <button
        type="button"
        onClick={onClose}
        aria-label="Close"
        className="absolute right-4 top-4 grid size-10 place-items-center rounded-full bg-white/10 text-white transition-colors hover:bg-white/20 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white/60"
      >
        <X className="size-5" aria-hidden />
      </button>

      {/* Stops a click on the picture itself from closing the dialog — and, for
          a video, from swallowing every press of its own play button. */}
      <figure className="max-w-5xl" onClick={(event) => event.stopPropagation()}>
        <Frame item={item} enlarged />
        {item.caption && (
          <figcaption className="mt-3 text-center text-sm text-white/80">
            {item.caption}
          </figcaption>
        )}
      </figure>
    </div>
  )
}

export function BusinessGallery({ items }: { items: BusinessMedia[] }) {
  const [openIndex, setOpenIndex] = useState<number | null>(null)

  const step = useCallback(
    (delta: number) => {
      setOpenIndex((current) => {
        if (current === null) return current
        // Wraps. At the end of a gallery the useful next thing is the start of
        // it, not a dead arrow key.
        return (current + delta + items.length) % items.length
      })
    },
    [items.length],
  )

  const close = useCallback(() => setOpenIndex(null), [])
  const open = openIndex === null ? null : items[openIndex]

  return (
    <>
      <ul className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4">
        {items.map((item, index) => (
          <li key={item.id}>
            <button
              type="button"
              onClick={() => setOpenIndex(index)}
              className="group relative block aspect-square w-full overflow-hidden rounded-xl bg-sand-200 ring-1 ring-sand-200 transition-transform hover:scale-[1.01] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500"
              aria-label={item.caption ?? (item.kind === 'video' ? 'Play video' : 'View photo')}
            >
              <Frame item={item} />

              {/* Marks a video as one before it is opened. A first frame alone
                  is indistinguishable from a photograph, so somebody looking for
                  the walkthrough would have to click every tile to find it. */}
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

      {open && <Lightbox item={open} onClose={close} onStep={step} />}
    </>
  )
}
