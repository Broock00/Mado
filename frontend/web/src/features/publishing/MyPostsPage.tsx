/**
 * "Your posts" — the author's own view.
 *
 * Shows editorial state the public payload deliberately omits: whether something
 * is a draft, whether moderation is holding it, and how many people have reported
 * it. Spec BUSINESS-07 requires moderation to be explainable to the person
 * affected, not only to the moderator.
 */

import { Link, useSearchParams } from 'react-router-dom'
import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle,
  Archive,
  BarChart3,
  Eye,
  EyeOff,
  FileText,
  Megaphone,
  PencilLine,
  Plus,
  Repeat2,
  ShieldAlert,
  Ticket,
  Trash2,
} from 'lucide-react'
import { ApiError, api } from '@/lib/api'
import { useAppStore } from '@/app/store'
import { Button, EmptyState, Skeleton } from '@/design-system/primitives'
import { cn, formatPrice, formatWhen } from '@/lib/utils'
import type { BusinessPromotion, OwnPost, PostStatus } from '@/lib/types'
import { PromoteDialog } from '@/features/business/PromoteDialog'
import { VerificationCard } from './VerificationCard'

type Filter = 'all' | 'published' | 'draft' | 'archived' | 'review'

const FILTERS: { id: Filter; label: string }[] = [
  { id: 'all', label: 'All' },
  { id: 'published', label: 'Live' },
  { id: 'draft', label: 'Drafts' },
  { id: 'review', label: 'In review' },
  { id: 'archived', label: 'Archived' },
]

const STATUS_LABEL: Record<PostStatus, string> = {
  published: 'Live',
  draft: 'Draft',
  review: 'In review',
  archived: 'Archived',
}

export function MyPostsPage() {
  const user = useAppStore((s) => s.user)
  const [searchParams] = useSearchParams()
  const justPublished = searchParams.get('published')
  const queryClient = useQueryClient()
  const [filter, setFilter] = useState<Filter>('all')

  const { data: posts, isLoading } = useQuery({
    queryKey: ['my-posts'],
    queryFn: () => api.myPosts(),
    enabled: Boolean(user),
  })

  const [refusal, setRefusal] = useState<string | null>(null)
  const [promoting, setPromoting] = useState<OwnPost | null>(null)

  // An account publishes under one identity (`publisher_for` on the server), so
  // one lookup covers every card rather than one per post.
  const publisherId = posts?.[0]?.publisher?.id

  // Whether this account may spend the publisher's money. The button is hidden
  // rather than shown failing for an editor who cannot buy — the server checks
  // again either way, so this is a courtesy and never the gate.
  const { data: held } = useQuery({
    queryKey: ['business-permissions', publisherId],
    queryFn: () => api.businessPermissions(publisherId!),
    enabled: Boolean(publisherId),
  })
  const mayPromote = (held ?? []).includes('finance:view')

  // What is already running, so a card can say so instead of offering to sell
  // the same slot twice.
  const { data: promotions } = useQuery({
    queryKey: ['promotions', publisherId],
    queryFn: () => api.businessPromotions(publisherId!),
    enabled: Boolean(publisherId) && mayPromote,
  })
  const promotionFor = (postId: string) =>
    (promotions ?? []).find(
      (row) =>
        row.experienceId === postId &&
        (row.status === 'active' || row.status === 'pending_payment'),
    )

  // Kept apart from the ordinary actions because it is the one that cannot be
  // undone, and because it is the one that can be refused - a listing somebody
  // holds a ticket for is not the publisher's alone to remove.
  const remove = useMutation({
    mutationFn: (id: string) => api.deletePost(id),
    onSuccess: () => {
      setRefusal(null)
      void queryClient.invalidateQueries({ queryKey: ['my-posts'] })
      void queryClient.invalidateQueries({ queryKey: ['canvas'] })
    },
    onError: (caught) =>
      setRefusal(
        caught instanceof ApiError ? caught.message : 'That post could not be deleted.',
      ),
  })

  const action = useMutation({
    mutationFn: ({ id, act }: { id: string; act: 'publish' | 'unpublish' | 'archive' | 'restore' }) =>
      api.postAction(id, act),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['my-posts'] })
      queryClient.invalidateQueries({ queryKey: ['canvas'] })
    },
  })

  const counts = useMemo(() => {
    const list = posts ?? []
    return {
      all: list.length,
      published: list.filter((p) => p.status === 'published').length,
      draft: list.filter((p) => p.status === 'draft').length,
      archived: list.filter((p) => p.status === 'archived').length,
      review: list.filter(
        (p) =>
          p.status === 'review' ||
          p.moderationStatus === 'pending' ||
          p.moderationStatus === 'flagged' ||
          p.moderationStatus === 'rejected',
      ).length,
    }
  }, [posts])

  const visible = useMemo(() => {
    if (!posts) return []
    if (filter === 'all') return posts
    if (filter === 'review') {
      return posts.filter(
        (p) =>
          p.status === 'review' ||
          p.moderationStatus === 'pending' ||
          p.moderationStatus === 'flagged' ||
          p.moderationStatus === 'rejected',
      )
    }
    return posts.filter((p) => p.status === filter)
  }, [posts, filter])

  if (!user) {
    return (
      <div className="mx-auto max-w-3xl px-4 py-16">
        <EmptyState
          icon={<Plus className="size-8" />}
          title="Sign in to share something"
          description="Anyone with an account can post what is happening around them."
          action={
            <Link to="/signin">
              <Button>Sign in</Button>
            </Link>
          }
        />
      </div>
    )
  }

  return (
    <div className="mx-auto w-full max-w-4xl px-4 pb-24 pt-8 sm:px-6">
      {/* Header */}
      <div className="mb-8 flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-3xl font-bold tracking-tight text-sand-900">Your posts</h1>
          <p className="mt-1.5 text-sm text-sand-500">
            Drafts stay private. Live posts appear in discovery for everyone nearby.
          </p>
        </div>
        <div className="flex shrink-0 flex-wrap gap-2">
          <Link to="/posts/analytics">
            <Button variant="secondary" size="sm">
              <BarChart3 className="size-4" aria-hidden />
              Analytics
            </Button>
          </Link>
          <Link to="/compose">
            <Button size="sm">
              <Plus className="size-4" aria-hidden />
              New post
            </Button>
          </Link>
        </div>
      </div>

      {/* Only once they have something published. A publisher row can outlive
          its listings - archived, or withheld by moderation - and offering
          "get verified" directly above "you have not posted anything yet"
          reads as a broken page rather than an invitation. */}
      {posts && posts.length > 0 && <VerificationCard />}

      {justPublished && (
        <div className="mb-5 rounded-xl border border-brand-700/40 bg-brand-900/25 px-4 py-3 text-sm text-brand-200">
          Published. It is now discoverable by anyone exploring the city.
        </div>
      )}

      {refusal && (
        <p
          role="alert"
          className="mb-4 rounded-xl bg-accent-100/80 px-3 py-2 text-sm text-accent-300"
        >
          {refusal}
        </p>
      )}

      {/* Filters */}
      {posts && posts.length > 0 && (
        <div className="mb-5 flex gap-1.5 overflow-x-auto pb-1">
          {FILTERS.map((item) => {
            const count = counts[item.id]
            if (item.id !== 'all' && count === 0) return null
            const active = filter === item.id
            return (
              <button
                key={item.id}
                type="button"
                onClick={() => setFilter(item.id)}
                className={cn(
                  'shrink-0 rounded-full px-3.5 py-1.5 text-xs font-medium transition-colors',
                  active
                    ? 'bg-[#2a2a2a] text-zinc-100'
                    : 'bg-[#1a1a1a] text-zinc-400 hover:bg-[#222] hover:text-zinc-200',
                )}
              >
                {item.label}
                <span className={cn('ml-1.5 tabular-nums', active ? 'text-zinc-400' : 'text-zinc-600')}>
                  {count}
                </span>
              </button>
            )
          })}
        </div>
      )}

      {isLoading && (
        <div className="space-y-3">
          {[0, 1, 2].map((i) => (
            <Skeleton key={i} className="h-36 w-full rounded-2xl" />
          ))}
        </div>
      )}

      {posts && posts.length === 0 && (
        <EmptyState
          icon={<FileText className="size-8" />}
          title="You have not posted anything yet"
          description="Know a good spot, or something happening this week? Share it."
          action={
            <Link to="/compose">
              <Button>Share something</Button>
            </Link>
          }
        />
      )}

      {posts && posts.length > 0 && visible.length === 0 && (
        <EmptyState
          icon={<Archive className="size-8" />}
          title="Nothing in this view"
          description="Try another filter, or create a new post."
        />
      )}

      <ul className="space-y-3">
        {visible.map((post) => (
          <li key={post.id}>
            <PostCard
              post={post}
              busy={action.isPending || remove.isPending}
              onAction={(act) => action.mutate({ id: post.id, act })}
              onDelete={() => remove.mutate(post.id)}
              promotion={promotionFor(post.id)}
              onPromote={mayPromote ? () => setPromoting(post) : undefined}
            />
          </li>
        ))}
      </ul>

      {promoting && publisherId && (
        <PromoteDialog
          post={promoting}
          publisherId={publisherId}
          onClose={() => setPromoting(null)}
        />
      )}
    </div>
  )
}

/**
 * What a running promotion has actually done, in one line.
 *
 * Clicks and the rate beside them, not just "N shown". An impression count on
 * its own is the weakest possible proof of value — it says the platform served
 * the card and nothing about whether it worked — and a business with no way to
 * tell a slot that earned attention from one that did not has no basis on which
 * to buy a second.
 *
 * The rate is withheld until something has been served, rather than shown as
 * 0%: a campaign that has had no impressions yet has no rate, and a zero there
 * reads as a verdict on it.
 */
function promotionResult(promotion: BusinessPromotion): string {
  const { impressions, clicks } = promotion
  if (!impressions) return 'Promoted · not shown yet'
  const rate = ((clicks / impressions) * 100).toFixed(clicks / impressions >= 0.1 ? 0 : 1)
  return `Promoted · ${impressions} shown · ${clicks} opened (${rate}%)`
}

function PostCard({
  post,
  busy,
  onAction,
  onDelete,
  promotion,
  onPromote,
}: {
  post: OwnPost
  busy: boolean
  onAction: (act: 'publish' | 'unpublish' | 'archive' | 'restore') => void
  onDelete: () => void
  promotion?: BusinessPromotion
  /** Absent when this account may not spend the publisher's money. */
  onPromote?: () => void
}) {
  // Two taps, in place. A confirm() blocks the whole page and a modal for one
  // sentence is more ceremony than this needs - but a single tap that destroys
  // something is not a thing to offer either.
  const [confirming, setConfirming] = useState(false)
  const image = post.media[0]
  const withheld = post.moderationStatus === 'flagged' || post.moderationStatus === 'rejected'
  const pending = post.moderationStatus === 'pending'
  const isDraft = post.status === 'draft'
  const readinessLeft = post.readinessProblems.length

  return (
    <article
      className={cn(
        'overflow-hidden rounded-2xl border border-sand-200 bg-sand-100 shadow-card',
        'transition-colors hover:border-sand-300',
        isDraft && 'border-dashed',
      )}
    >
      <div className="flex flex-col sm:flex-row">
        {/* Cover */}
        <div className="relative aspect-[16/10] shrink-0 overflow-hidden bg-sand-200 sm:aspect-auto sm:h-auto sm:w-36 lg:w-44">
          {image ? (
            <img
              src={image.url}
              alt=""
              className="size-full object-cover sm:absolute sm:inset-0"
              loading="lazy"
            />
          ) : (
            <div className="flex size-full min-h-[7.5rem] items-center justify-center text-sand-400 sm:absolute sm:inset-0">
              <FileText className="size-7" aria-hidden />
            </div>
          )}
          {/* Status sits on the image so it is legible at a glance in a long list. */}
          <div className="absolute left-2.5 top-2.5 flex flex-wrap gap-1.5">
            <span
              className={cn(
                'rounded-full px-2 py-0.5 text-[0.65rem] font-semibold uppercase tracking-wide',
                post.status === 'published' && 'bg-brand-700 text-white',
                post.status === 'draft' && 'bg-black/70 text-zinc-200 backdrop-blur-sm',
                post.status === 'archived' && 'bg-black/70 text-zinc-400 backdrop-blur-sm',
                post.status === 'review' && 'bg-accent-100 text-accent-300',
              )}
            >
              {STATUS_LABEL[post.status] ?? post.status}
            </span>
            {withheld && (
              <span className="inline-flex items-center gap-1 rounded-full bg-red-950 px-2 py-0.5 text-[0.65rem] font-semibold uppercase tracking-wide text-red-300">
                <ShieldAlert className="size-3" aria-hidden />
                Withheld
              </span>
            )}
            {pending && !withheld && (
              <span className="inline-flex items-center gap-1 rounded-full bg-accent-100 px-2 py-0.5 text-[0.65rem] font-semibold uppercase tracking-wide text-accent-300">
                <AlertTriangle className="size-3" aria-hidden />
                Review
              </span>
            )}
          </div>
        </div>

        {/* Body */}
        <div className="flex min-w-0 flex-1 flex-col p-4 sm:p-5">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <h2 className="truncate text-base font-semibold tracking-tight text-sand-900 sm:text-lg">
                {post.title}
              </h2>
              <p className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-xs text-sand-500 sm:text-sm">
                {post.category && <span>{post.category.name}</span>}
                {post.venue && (
                  <>
                    {post.category && <span aria-hidden>·</span>}
                    <span className="truncate">{post.venue.name}</span>
                  </>
                )}
                <span aria-hidden>·</span>
                <span className="font-medium text-sand-700">{formatPrice(post.price)}</span>
                {post.nextEvent && (
                  <>
                    <span aria-hidden>·</span>
                    <span>{formatWhen(post.nextEvent.startTime)}</span>
                  </>
                )}
              </p>
            </div>

            {post.status === 'published' && (
              <p className="flex shrink-0 items-center gap-1 text-xs text-sand-500">
                <Repeat2 className="size-3.5" aria-hidden />
                <span className="tabular-nums">{post.repostCount}</span>
              </p>
            )}
          </div>

          {/* The author is told why, not just that. */}
          {withheld && (
            <p className="mt-3 rounded-lg bg-red-950/80 px-3 py-2 text-xs leading-relaxed text-red-300">
              Withheld from discovery while it is reviewed
              {post.reportCount > 0 &&
                ` after ${post.reportCount} report${post.reportCount > 1 ? 's' : ''}`}
              . Your post has not been deleted.
            </p>
          )}
          {pending && post.moderationNotes && (
            <p className="mt-3 rounded-lg bg-accent-100/60 px-3 py-2 text-xs leading-relaxed text-accent-300">
              {post.moderationNotes}
            </p>
          )}
          {isDraft && readinessLeft > 0 && (
            <p className="mt-3 rounded-lg bg-[#1a1a1a] px-3 py-2 text-xs text-zinc-400">
              {readinessLeft} thing{readinessLeft > 1 ? 's' : ''} left before it can go live
              {post.readinessProblems[0] ? ` — ${post.readinessProblems[0]}` : ''}
              {readinessLeft > 1 ? ` +${readinessLeft - 1} more` : ''}
            </p>
          )}
          {isDraft && readinessLeft === 0 && (
            <p className="mt-3 text-xs font-medium text-brand-400">Ready to publish</p>
          )}

          {/* Actions */}
          <div className="mt-auto flex flex-wrap items-center gap-2 pt-4">
            <Link to={`/compose/${post.id}`}>
              <Button variant="secondary" size="sm">
                <PencilLine className="size-3.5" aria-hidden />
                Edit
              </Button>
            </Link>

            {post.status === 'published' ? (
              <>
                <Link to={`/experiences/${post.id}`}>
                  <Button variant="ghost" size="sm">
                    <Eye className="size-3.5" aria-hidden />
                    View
                  </Button>
                </Link>
                {/* Only for a listing with dates: bookings hang off an
                    occurrence, so a place that is simply open has nothing to
                    show and the link would lead to an empty page. */}
                {post.upcomingEvents.length > 0 && (
                  <Link to={`/posts/${post.id}/bookings`}>
                    <Button variant="ghost" size="sm">
                      <Ticket className="size-3.5" aria-hidden />
                      Bookings
                    </Button>
                  </Link>
                )}
                {/* Only a published post: promoting a draft is refused by
                    the server, and offering it here would be a button that
                    returns 409. */}
                {onPromote &&
                  (promotion ? (
                    <span className="inline-flex items-center gap-1.5 rounded-lg bg-sand-200 px-2.5 py-1.5 text-xs font-medium text-sand-700">
                      <Megaphone className="size-3.5" aria-hidden />
                      {promotion.status === 'active'
                        ? promotionResult(promotion)
                        : 'Promotion awaiting payment'}
                    </span>
                  ) : (
                    <Button variant="ghost" size="sm" onClick={onPromote}>
                      <Megaphone className="size-3.5" aria-hidden />
                      Promote
                    </Button>
                  ))}
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => onAction('unpublish')}
                  disabled={busy}
                >
                  <EyeOff className="size-3.5" aria-hidden />
                  Unpublish
                </Button>
              </>
            ) : post.status === 'archived' ? (
              <Button variant="ghost" size="sm" onClick={() => onAction('restore')} disabled={busy}>
                Restore
              </Button>
            ) : (
              <Button
                size="sm"
                onClick={() => onAction('publish')}
                disabled={busy || readinessLeft > 0}
              >
                Publish
              </Button>
            )}

            <div className="ml-auto">
              {/* Last, and apart, because it is the only one that cannot be taken
                  back. The server refuses it once somebody holds a ticket. */}
              {confirming ? (
                <span className="flex items-center gap-1.5">
                  <span className="text-xs text-sand-500">Delete this?</span>
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() => {
                      setConfirming(false)
                      onDelete()
                    }}
                    disabled={busy}
                  >
                    Yes, delete
                  </Button>
                  <Button variant="ghost" size="sm" onClick={() => setConfirming(false)}>
                    Keep
                  </Button>
                </span>
              ) : (
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => setConfirming(true)}
                  disabled={busy}
                  className="text-red-300 hover:bg-red-950/50"
                  aria-label="Delete post"
                >
                  <Trash2 className="size-3.5" aria-hidden />
                  <span className="sr-only sm:not-sr-only">Delete</span>
                </Button>
              )}
            </div>
          </div>
        </div>
      </div>
    </article>
  )
}
