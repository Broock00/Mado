/**
 * Buying a promoted slot for one post.
 *
 * Opened from the post it promotes, because that is the thing being decided
 * about — a list of every post on a settings page asks somebody to find the one
 * they were just looking at.
 *
 * **The limits are on the form, above the price.** What Mado sells here is
 * narrower than "sponsored" usually means: it never takes first place, it only
 * lifts a post the explorer's own search already found, and it is withheld from
 * anybody whose access or dietary requirement the post has not answered. A
 * publisher who learns that after paying feels cheated; one who reads it first
 * is being sold something honest.
 *
 * **The price comes from the server**, per currency, and the total is shown
 * before the button — a price somebody first sees on the payment page is a price
 * they never agreed to.
 */

import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Megaphone, X } from 'lucide-react'

import { ApiError, api } from '@/lib/api'
import type { OwnPost } from '@/lib/types'
import { money } from '@/lib/money'
import { Button, Card } from '@/design-system/primitives'

const DAY_CHOICES = [3, 7, 14, 30]

export function PromoteDialog({
  post,
  publisherId,
  onClose,
}: {
  post: OwnPost
  publisherId: string
  onClose: () => void
}) {
  const queryClient = useQueryClient()
  const [days, setDays] = useState(7)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const { data: pricing } = useQuery({
    queryKey: ['promotion-pricing', publisherId],
    // The business's own city decides, so a promotion and a plan never quote
    // the same account in different money.
    queryFn: () => api.promotionPricing(publisherId),
  })

  const buy = useMutation({
    mutationFn: () =>
      api.startPromotion(publisherId, {
        experienceId: post.id,
        days,
        currency: pricing?.currency ?? 'ETB',
        // Where the post is. Promoting a place somewhere it is not would be
        // selling reach that cannot convert into anybody walking in.
        citySlug: post.citySlug ?? undefined,
      }),
    onSuccess: (promotion) => {
      void queryClient.invalidateQueries({ queryKey: ['promotions', publisherId] })
      if (promotion.checkoutUrl) window.location.href = promotion.checkoutUrl
    },
    onError: (caught: unknown) =>
      setError(caught instanceof ApiError ? caught.message : 'That did not work. Try again.'),
  })

  const total =
    pricing?.dailyMinor != null ? pricing.dailyMinor * days : null

  return (
    <div className="fixed inset-0 z-50 grid place-items-center p-4" role="dialog" aria-modal="true">
      <button
        type="button"
        className="absolute inset-0 bg-sand-950/30 backdrop-blur-[2px]"
        onClick={onClose}
        aria-label="Close"
      />

      <Card className="relative w-full max-w-md p-5">
        <div className="mb-4 flex items-start justify-between gap-4">
          <div>
            <h2 className="flex items-center gap-2 text-base font-semibold text-sand-900">
              <Megaphone className="size-4" aria-hidden />
              Promote this post
            </h2>
            <p className="mt-1 line-clamp-1 text-sm text-sand-500">{post.title}</p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="grid size-8 place-items-center rounded-full text-sand-500 hover:bg-sand-200"
            aria-label="Close"
          >
            <X className="size-4" aria-hidden />
          </button>
        </div>

        <div className="space-y-4">
          <div>
            <span className="mb-1.5 block text-sm text-sand-700">For how long</span>
            <div className="flex flex-wrap gap-2">
              {DAY_CHOICES.map((choice) => (
                <button
                  key={choice}
                  type="button"
                  onClick={() => setDays(choice)}
                  aria-pressed={days === choice}
                  className={`rounded-lg border px-3 py-1.5 text-sm ${
                    days === choice
                      ? 'border-brand-500 bg-brand-50/40 font-medium text-brand-700'
                      : 'border-sand-300 text-sand-700 hover:border-sand-400'
                  }`}
                >
                  {choice} days
                </button>
              ))}
            </div>
          </div>

          <div className="rounded-xl bg-sand-50 p-4 text-sm">
            <div className="flex items-baseline justify-between gap-3">
              <span className="text-sand-600">Total</span>
              <span className="text-lg font-semibold text-sand-900">
                {total != null && pricing ? money(total, pricing.currency) : '—'}
              </span>
            </div>
            {post.citySlug && (
              <p className="mt-1 text-sand-500">Shown to people looking in {post.citySlug}.</p>
            )}
          </div>

          {/* Before the money, deliberately. */}
          <ul className="space-y-1 text-xs text-sand-500">
            <li>Never takes the top result — your post appears just below it, labelled.</li>
            <li>Only lifts your post in searches that already found it.</li>
            <li>
              Not shown to someone whose access or dietary requirement your post has not
              answered.
            </li>
          </ul>

          {error && <p className="text-sm text-rust-600">{error}</p>}

          <div className="flex items-center gap-2">
            <Button
              onClick={() => buy.mutate()}
              disabled={buy.isPending || total == null}
            >
              {buy.isPending ? 'Opening checkout…' : 'Continue to payment'}
            </Button>
            <Button variant="ghost" onClick={onClose}>
              Cancel
            </Button>
          </div>

          {pricing?.dailyMinor == null && (
            <p className="text-xs text-sand-500">
              Promotions are not sold in your currency yet.
            </p>
          )}
        </div>
      </Card>
    </div>
  )
}
