/**
 * Reviews on an experience.
 *
 * Minimal mode keeps the summary to one line, hides the write form until
 * the explorer taps the stars, and lists reviews as compact rows rather
 * than cards — suited to the booking rail beside the detail page.
 */

import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Star, TriangleAlert } from 'lucide-react'

import { api } from '@/lib/api'
import { useAppStore } from '@/app/store'
import type { ReviewEntry } from '@/lib/types'
import { Button } from '@/design-system/primitives'
import { cn } from '@/lib/utils'

const SCALE = [1, 2, 3, 4, 5] as const

function Stars({ value, size = 'sm' }: { value: number; size?: 'sm' | 'md' }) {
  const dim = size === 'md' ? 'size-4' : 'size-3.5'
  return (
    <span className="inline-flex items-center gap-0.5" aria-label={`${value} out of 5`}>
      {SCALE.map((n) => (
        <Star
          key={n}
          className={cn(dim, n <= value ? 'fill-accent-500 text-accent-500' : 'text-sand-300')}
          aria-hidden
        />
      ))}
    </span>
  )
}

function RatingPicker({ value, onChange }: { value: number; onChange: (n: number) => void }) {
  const [hovered, setHovered] = useState<number | null>(null)
  const shown = hovered ?? value
  return (
    <div className="flex items-center gap-0.5" onMouseLeave={() => setHovered(null)}>
      {SCALE.map((n) => (
        <button
          key={n}
          type="button"
          onClick={() => onChange(n)}
          onMouseEnter={() => setHovered(n)}
          aria-label={`${n} star${n > 1 ? 's' : ''}`}
          aria-pressed={value === n}
          className="rounded p-0.5 transition-transform hover:scale-110"
        >
          <Star
            className={cn('size-6', n <= shown ? 'fill-accent-500 text-accent-500' : 'text-sand-300')}
            aria-hidden
          />
        </button>
      ))}
    </div>
  )
}

function ReviewRow({ review }: { review: ReviewEntry }) {
  return (
    <li className="border-b border-sand-100 py-3 last:border-0">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm font-medium text-sand-900">{review.authorName}</span>
            {review.verifiedAttendance && (
              <span className="text-[0.65rem] font-medium uppercase tracking-wide text-brand-700">
                Went
              </span>
            )}
          </div>
          <Stars value={review.rating} size="sm" />
        </div>
        <span className="shrink-0 text-xs text-sand-400">
          {new Date(review.createdAt).toLocaleDateString([], {
            day: 'numeric',
            month: 'short',
          })}
        </span>
      </div>
      {review.comment && (
        <p className="mt-1.5 text-sm leading-relaxed text-sand-600">{review.comment}</p>
      )}
    </li>
  )
}

export function Reviews({
  experienceId,
  bare = false,
  minimal = false,
}: {
  experienceId: string
  /** Omit the section heading when the parent already labels the block. */
  bare?: boolean
  /** Compact layout for the booking rail — form opens on star click. */
  minimal?: boolean
}) {
  const user = useAppStore((s) => s.user)
  const queryClient = useQueryClient()

  const { data } = useQuery({
    queryKey: ['reviews', experienceId],
    queryFn: () => api.reviews(experienceId),
  })

  const [rating, setRating] = useState(0)
  const [comment, setComment] = useState('')
  const [formOpen, setFormOpen] = useState(false)

  const mine = data?.mine
  const summary = data?.summary

  const openForm = (initialRating = 0) => {
    if (mine) {
      setRating(mine.rating)
      setComment(mine.comment ?? '')
    } else {
      setRating(initialRating)
      setComment('')
    }
    setFormOpen(true)
  }

  const closeForm = () => {
    setFormOpen(false)
    if (!mine) {
      setRating(0)
      setComment('')
    }
  }

  const submit = useMutation({
    mutationFn: () => api.leaveReview(experienceId, rating, comment.trim() || undefined),
    onSuccess: () => {
      closeForm()
      void queryClient.invalidateQueries({ queryKey: ['reviews', experienceId] })
      void queryClient.invalidateQueries({ queryKey: ['experience', experienceId] })
    },
  })

  const withdraw = useMutation({
    mutationFn: () => api.withdrawReview(experienceId),
    onSuccess: () => {
      setRating(0)
      setComment('')
      setFormOpen(false)
      void queryClient.invalidateQueries({ queryKey: ['reviews', experienceId] })
      void queryClient.invalidateQueries({ queryKey: ['experience', experienceId] })
    },
  })

  if (minimal) {
    return (
      <section id="reviews" className="scroll-mt-28">
        <h2 className="text-sm font-semibold text-sand-950">Reviews</h2>

        {/* Summary + tap-to-rate */}
        <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1">
          {summary && summary.count > 0 ? (
            <>
              <span className="text-sm font-semibold tabular-nums text-sand-950">
                {summary.average?.toFixed(1)}
              </span>
              <Stars value={Math.round(summary.average ?? 0)} size="sm" />
              <span className="text-xs text-sand-500">
                {summary.count} {summary.count === 1 ? 'review' : 'reviews'}
              </span>
            </>
          ) : (
            <span className="text-xs text-sand-500">No reviews yet</span>
          )}
        </div>

        {/* Clickable stars to open the form */}
        {user ? (
          mine && !formOpen ? (
            <div className="mt-3 rounded-lg border border-sand-100 bg-sand-50/50 px-3 py-2.5">
              <p className="text-[0.65rem] font-medium uppercase tracking-wide text-sand-400">
                Your review
              </p>
              <button
                type="button"
                onClick={() => openForm()}
                className="mt-1 flex w-full items-center gap-2 text-left"
              >
                <Stars value={mine.rating} size="sm" />
                <span className="text-xs text-brand-700">Edit</span>
              </button>
              {mine.comment && (
                <p className="mt-1.5 text-sm text-sand-600">{mine.comment}</p>
              )}
              {mine.status && mine.status !== 'approved' && (
                <p className="mt-2 flex items-start gap-1.5 text-xs text-sand-600">
                  <TriangleAlert className="mt-0.5 size-3 shrink-0 text-warning" aria-hidden />
                  Waiting on a moderator.
                </p>
              )}
            </div>
          ) : !formOpen ? (
            <div className="mt-3 flex items-center gap-2 rounded-lg border border-dashed border-sand-200 px-3 py-2.5">
              <div className="flex items-center gap-0.5">
                {SCALE.map((n) => (
                  <button
                    key={n}
                    type="button"
                    onClick={() => openForm(n)}
                    aria-label={`Rate ${n} star${n > 1 ? 's' : ''}`}
                    className="rounded p-0.5 transition-transform hover:scale-110"
                  >
                    <Star className="size-4 text-sand-300 hover:text-accent-400" aria-hidden />
                  </button>
                ))}
              </div>
              <span className="text-xs text-sand-500">Tap to rate</span>
            </div>
          ) : null
        ) : (
          <p className="mt-3 text-xs text-sand-500">
            <Link to="/signin" className="font-semibold text-brand-700 hover:underline">
              Sign in
            </Link>{' '}
            to leave a review
          </p>
        )}

        {formOpen && user && (
          <div className="mt-3 space-y-3 rounded-lg border border-sand-200 bg-white p-3">
            <RatingPicker value={rating} onChange={setRating} />
            <textarea
              value={comment}
              onChange={(e) => setComment(e.target.value)}
              rows={2}
              maxLength={2000}
              placeholder="Optional comment"
              aria-label="Your review"
              className="w-full resize-none rounded-lg border border-sand-200 bg-sand-50/50 px-3 py-2 text-sm text-sand-900 placeholder:text-sand-400 focus:border-brand-400 focus:bg-white focus:outline-none focus:ring-2 focus:ring-brand-600/15"
            />
            <div className="flex flex-wrap items-center gap-2">
              <Button
                size="sm"
                onClick={() => submit.mutate()}
                loading={submit.isPending}
                disabled={rating === 0}
              >
                {mine ? 'Update' : 'Post'}
              </Button>
              <Button variant="ghost" size="sm" onClick={closeForm}>
                Cancel
              </Button>
              {mine && (
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => withdraw.mutate()}
                  disabled={withdraw.isPending}
                  className="ml-auto text-sand-500"
                >
                  Remove
                </Button>
              )}
              {submit.isError && (
                <span className="text-xs text-danger" role="alert">
                  {(submit.error as Error).message}
                </span>
              )}
            </div>
          </div>
        )}

        {data && data.reviews.length > 0 && (
          <ul className="mt-3">
            {data.reviews.slice(0, 5).map((review) => (
              <ReviewRow key={review.id} review={review} />
            ))}
          </ul>
        )}
      </section>
    )
  }

  /* ── Full layout (used elsewhere) ── */
  return (
    <section id="reviews" className="scroll-mt-28">
      {!bare && (
        <h2 className="text-xl font-semibold tracking-tight text-sand-950">What people said</h2>
      )}

      {summary && summary.count > 0 ? (
        <div className="mt-3 flex flex-wrap items-center gap-3">
          <span className="text-3xl font-bold tabular-nums text-sand-950">{summary.average}</span>
          <div>
            <Stars value={Math.round(summary.average ?? 0)} size="md" />
            <p className="mt-1 text-xs text-sand-500">
              {summary.count} {summary.count === 1 ? 'review' : 'reviews'}
            </p>
          </div>
        </div>
      ) : (
        !bare && <p className="mt-1.5 text-sm text-sand-500">No reviews yet — be the first.</p>
      )}

      {user ? (
        mine && !formOpen ? (
          <div className="mt-4 rounded-xl border border-brand-100 bg-brand-50/40 p-4">
            <p className="text-xs font-medium uppercase tracking-wide text-brand-700">Your review</p>
            <button type="button" onClick={() => openForm()} className="mt-2 flex items-center gap-2">
              <Stars value={mine.rating} size="md" />
              <span className="text-sm text-brand-700">Edit</span>
            </button>
            {mine.comment && <p className="mt-2 text-sm text-sand-700">{mine.comment}</p>}
          </div>
        ) : !formOpen ? (
          <button
            type="button"
            onClick={() => openForm()}
            className="mt-4 flex items-center gap-2 rounded-xl border border-dashed border-sand-200 px-4 py-3 hover:border-brand-200 hover:bg-brand-50/30"
          >
            <Stars value={0} size="md" />
            <span className="text-sm font-medium text-sand-700">Rate this experience</span>
          </button>
        ) : null
      ) : (
        <p className="mt-4 text-sm text-sand-600">
          <Link to="/signin" className="font-semibold text-brand-700 hover:underline">
            Sign in
          </Link>{' '}
          to leave a review.
        </p>
      )}

      {formOpen && user && (
        <div className="mt-4 space-y-3 rounded-xl border border-sand-200 bg-white p-4">
          <RatingPicker value={rating} onChange={setRating} />
          <textarea
            value={comment}
            onChange={(e) => setComment(e.target.value)}
            rows={3}
            maxLength={2000}
            placeholder="What should someone know before going? (optional)"
            aria-label="Your review"
            className="w-full resize-none rounded-xl border border-sand-200 bg-sand-50/50 px-4 py-3 text-sm text-sand-900 placeholder:text-sand-400 focus:border-brand-400 focus:bg-white focus:outline-none focus:ring-2 focus:ring-brand-600/15"
          />
          <div className="flex flex-wrap items-center gap-2">
            <Button
              size="sm"
              onClick={() => submit.mutate()}
              loading={submit.isPending}
              disabled={rating === 0}
            >
              {mine ? 'Update review' : 'Post review'}
            </Button>
            <Button variant="ghost" size="sm" onClick={closeForm}>
              Cancel
            </Button>
            {submit.isError && (
              <span className="text-sm text-danger" role="alert">
                {(submit.error as Error).message}
              </span>
            )}
          </div>
        </div>
      )}

      {data && data.reviews.length > 0 && (
        <ul className="mt-4 space-y-1">
          {data.reviews.map((review) => (
            <ReviewRow key={review.id} review={review} />
          ))}
        </ul>
      )}
    </section>
  )
}
