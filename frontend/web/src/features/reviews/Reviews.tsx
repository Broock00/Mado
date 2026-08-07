/**
 * Reviews on an experience.
 *
 * The third content inflow (spec BUSINESS-03): the people who turned up saying
 * whether it was worth it. Until this existed explorers could only consume -
 * and every rating in the system was seeded fiction feeding a live ranking
 * signal.
 *
 * Two things the design is careful about:
 *
 * - **The spread is shown, not just the average.** Five 1s and five 5s average
 *   to the same 3 as ten 3s, and they describe completely different places. An
 *   average alone hides exactly the thing worth knowing.
 * - **A withheld review is explained to its author.** Someone whose words are
 *   not appearing should be told why, rather than left to conclude the platform
 *   lost them.
 */

import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Star, TriangleAlert } from 'lucide-react'

import { api } from '@/lib/api'
import { useAppStore } from '@/app/store'
import type { ReviewEntry } from '@/lib/types'
import { Button, Card, SectionHeading } from '@/design-system/primitives'
import { cn } from '@/lib/utils'

const SCALE = [1, 2, 3, 4, 5] as const

function Stars({ value, size = 'sm' }: { value: number; size?: 'sm' | 'lg' }) {
  return (
    <span className="inline-flex items-center gap-0.5" aria-label={`${value} out of 5`}>
      {SCALE.map((n) => (
        <Star
          key={n}
          className={cn(
            size === 'lg' ? 'size-5' : 'size-3.5',
            n <= value ? 'fill-amber-400 text-amber-400' : 'text-sand-300',
          )}
          aria-hidden
        />
      ))}
    </span>
  )
}

function RatingPicker({
  value,
  onChange,
}: {
  value: number
  onChange: (next: number) => void
}) {
  const [hovered, setHovered] = useState<number | null>(null)
  const shown = hovered ?? value

  return (
    <div className="flex items-center gap-1" onMouseLeave={() => setHovered(null)}>
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
            className={cn('size-7', n <= shown ? 'fill-amber-400 text-amber-400' : 'text-sand-300')}
            aria-hidden
          />
        </button>
      ))}
    </div>
  )
}

/** The spread behind the average - what an average on its own conceals. */
function Distribution({
  distribution,
  count,
}: {
  distribution: Record<number, number>
  count: number
}) {
  return (
    <div className="space-y-1">
      {[5, 4, 3, 2, 1].map((n) => {
        const share = count > 0 ? ((distribution[n] ?? 0) / count) * 100 : 0
        return (
          <div key={n} className="flex items-center gap-2 text-xs text-sand-600">
            <span className="w-3 tabular-nums">{n}</span>
            <span className="h-1.5 flex-1 overflow-hidden rounded-full bg-sand-200">
              <span
                className="block h-full rounded-full bg-amber-400"
                style={{ width: `${share}%` }}
              />
            </span>
            <span className="w-6 tabular-nums text-right">{distribution[n] ?? 0}</span>
          </div>
        )
      })}
    </div>
  )
}

function ReviewRow({ review }: { review: ReviewEntry }) {
  return (
    <li className="border-t border-sand-200 py-3 first:border-t-0">
      <div className="flex flex-wrap items-center gap-2">
        <Stars value={review.rating} />
        <span className="text-sm font-medium text-sand-900">{review.authorName}</span>
        {review.verifiedAttendance && (
          <span className="rounded-full bg-brand-100 px-2 py-0.5 text-[0.7rem] font-medium text-brand-800">
            went
          </span>
        )}
        <span className="text-xs text-sand-500">
          {new Date(review.createdAt).toLocaleDateString([], {
            day: 'numeric',
            month: 'short',
            year: 'numeric',
          })}
        </span>
      </div>
      {review.comment && <p className="mt-1 text-sm text-sand-700">{review.comment}</p>}
    </li>
  )
}

export function Reviews({ experienceId }: { experienceId: string }) {
  const user = useAppStore((s) => s.user)
  const queryClient = useQueryClient()

  const { data } = useQuery({
    queryKey: ['reviews', experienceId],
    queryFn: () => api.reviews(experienceId),
  })

  const [rating, setRating] = useState(0)
  const [comment, setComment] = useState('')
  const [editing, setEditing] = useState(false)

  const mine = data?.mine
  const showForm = editing || !mine

  const submit = useMutation({
    mutationFn: () => api.leaveReview(experienceId, rating, comment.trim() || undefined),
    onSuccess: () => {
      setEditing(false)
      void queryClient.invalidateQueries({ queryKey: ['reviews', experienceId] })
      // The rating feeds the quality ranking signal, so the card is now stale.
      void queryClient.invalidateQueries({ queryKey: ['experience', experienceId] })
    },
  })

  const withdraw = useMutation({
    mutationFn: () => api.withdrawReview(experienceId),
    onSuccess: () => {
      setRating(0)
      setComment('')
      void queryClient.invalidateQueries({ queryKey: ['reviews', experienceId] })
      void queryClient.invalidateQueries({ queryKey: ['experience', experienceId] })
    },
  })

  const summary = data?.summary

  return (
    <section className="mt-10">
      <SectionHeading
        title="What people said"
        subtitle={
          summary?.count
            ? `${summary.average} average from ${summary.count} ${
                summary.count === 1 ? 'review' : 'reviews'
              }`
            : 'No reviews yet'
        }
      />

      {summary && summary.count > 0 && (
        <Card className="mb-4 flex flex-wrap items-center gap-6 p-4">
          <div className="text-center">
            <p className="text-3xl font-semibold tabular-nums text-sand-900">{summary.average}</p>
            <Stars value={Math.round(summary.average ?? 0)} size="lg" />
          </div>
          <div className="min-w-[12rem] flex-1">
            <Distribution distribution={summary.distribution} count={summary.count} />
          </div>
        </Card>
      )}

      {/* The author's own, whatever its status. */}
      {mine && !editing && (
        <Card className="mb-4 p-4">
          <p className="text-xs font-medium uppercase tracking-wide text-sand-500">Your review</p>
          <div className="mt-1.5 flex flex-wrap items-center gap-2">
            <Stars value={mine.rating} />
            <span className="text-xs text-sand-500">
              {new Date(mine.createdAt).toLocaleDateString()}
            </span>
          </div>
          {mine.comment && <p className="mt-1 text-sm text-sand-700">{mine.comment}</p>}

          {/* Withheld reviews are explained rather than silently disappearing. */}
          {mine.status && mine.status !== 'approved' && (
            <p className="mt-2 flex items-start gap-1.5 rounded-lg bg-sand-100 px-3 py-2 text-xs text-sand-700">
              <TriangleAlert className="mt-0.5 size-3.5 shrink-0" aria-hidden />
              <span>
                This is waiting on a moderator, so it is not shown to others yet.
                {mine.moderationNotes && ` ${mine.moderationNotes}`}
              </span>
            </p>
          )}

          <div className="mt-3 flex gap-2">
            <Button
              variant="secondary"
              size="sm"
              onClick={() => {
                setRating(mine.rating)
                setComment(mine.comment ?? '')
                setEditing(true)
              }}
            >
              Edit
            </Button>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => withdraw.mutate()}
              disabled={withdraw.isPending}
            >
              Withdraw
            </Button>
          </div>
        </Card>
      )}

      {!user ? (
        <Card className="mb-4 p-4 text-sm text-sand-600">
          <Link to="/signin" className="underline">
            Sign in
          </Link>{' '}
          to leave a review.
        </Card>
      ) : (
        showForm && (
          <Card className="mb-4 space-y-3 p-4">
            <p className="text-sm font-medium text-sand-700">
              {mine ? 'Update your review' : 'Been here? Say how it was.'}
            </p>
            <RatingPicker value={rating} onChange={setRating} />
            <textarea
              value={comment}
              onChange={(event) => setComment(event.target.value)}
              rows={3}
              maxLength={2000}
              placeholder="What should someone know before going? (optional)"
              aria-label="Your review"
              className="w-full rounded-lg border border-sand-300 bg-white px-3 py-2 text-sm text-sand-900 placeholder:text-sand-400 focus:border-brand-500 focus:outline-none"
            />
            <div className="flex flex-wrap items-center gap-2">
              <Button
                size="sm"
                onClick={() => submit.mutate()}
                loading={submit.isPending}
                disabled={rating === 0}
              >
                {mine ? 'Update' : 'Post review'}
              </Button>
              {editing && (
                <Button variant="ghost" size="sm" onClick={() => setEditing(false)}>
                  Cancel
                </Button>
              )}
              {submit.isError && (
                <span className="text-sm text-red-700" role="alert">
                  {(submit.error as Error).message}
                </span>
              )}
            </div>
          </Card>
        )
      )}

      {data && data.reviews.length > 0 && (
        <Card className="px-4 py-1">
          <ul>
            {data.reviews.map((review) => (
              <ReviewRow key={review.id} review={review} />
            ))}
          </ul>
        </Card>
      )}
    </section>
  )
}
