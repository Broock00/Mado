/**
 * "Your posts" — the author's own view.
 *
 * Shows editorial state the public payload deliberately omits: whether something
 * is a draft, whether moderation is holding it, and how many people have reported
 * it. Spec BUSINESS-07 requires moderation to be explainable to the person
 * affected, not only to the moderator.
 */

import { Link, useSearchParams } from 'react-router-dom'
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle,
  BarChart3,
  Eye,
  EyeOff,
  PencilLine,
  Plus,
  ShieldAlert,
  Ticket,
  Trash2,
} from 'lucide-react'
import { ApiError, api } from '@/lib/api'
import { useAppStore } from '@/app/store'
import { Badge, Button, Card, EmptyState, Skeleton } from '@/design-system/primitives'
import { formatPrice, formatWhen } from '@/lib/utils'
import type { OwnPost } from '@/lib/types'
import { VerificationCard } from './VerificationCard'

const STATUS_TONE = {
  published: 'success',
  draft: 'neutral',
  review: 'accent',
  archived: 'neutral',
} as const

export function MyPostsPage() {
  const user = useAppStore((s) => s.user)
  const [searchParams] = useSearchParams()
  const justPublished = searchParams.get('published')
  const queryClient = useQueryClient()

  const { data: posts, isLoading } = useQuery({
    queryKey: ['my-posts'],
    queryFn: () => api.myPosts(),
    enabled: Boolean(user),
  })

  const [refusal, setRefusal] = useState<string | null>(null)

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
    <div className="mx-auto w-full max-w-4xl px-4 pb-24 pt-6 sm:px-6">
      <div className="mb-6 flex items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-sand-900">Your posts</h1>
          <p className="mt-1 text-sm text-sand-500">
            Anything you share appears in discovery for everyone in the city.
          </p>
        </div>
        <div className="flex shrink-0 gap-2">
          <Link to="/posts/analytics">
            <Button variant="secondary">
              <BarChart3 className="size-4" aria-hidden />
              How they are doing
            </Button>
          </Link>
          <Link to="/compose">
            <Button>
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
        <div className="mb-5 rounded-card border border-brand-200 bg-brand-50 px-4 py-3 text-sm text-brand-900">
          Published. It is now discoverable by anyone exploring the city.
        </div>
      )}

      {isLoading && (
        <div className="space-y-3">
          {[0, 1, 2].map((i) => (
            <Skeleton key={i} className="h-28 w-full" />
          ))}
        </div>
      )}

      {posts && posts.length === 0 && (
        <EmptyState
          icon={<Plus className="size-8" />}
          title="You have not posted anything yet"
          description="Know a good spot, or something happening this week? Share it."
          action={
            <Link to="/compose">
              <Button>Share something</Button>
            </Link>
          }
        />
      )}

      {refusal && (
        <p
          role="alert"
          className="mb-3 rounded-lg bg-accent-100/60 px-3 py-2 text-sm text-accent-800"
        >
          {refusal}
        </p>
      )}

      <div className="space-y-3">
        {posts?.map((post) => (
          <PostRow
            key={post.id}
            post={post}
            busy={action.isPending || remove.isPending}
            onAction={(act) => action.mutate({ id: post.id, act })}
            onDelete={() => remove.mutate(post.id)}
          />
        ))}
      </div>
    </div>
  )
}

function PostRow({
  post,
  busy,
  onAction,
  onDelete,
}: {
  post: OwnPost
  busy: boolean
  onAction: (act: 'publish' | 'unpublish' | 'archive' | 'restore') => void
  onDelete: () => void
}) {
  // Two taps, in place. A confirm() blocks the whole page and a modal for one
  // sentence is more ceremony than this needs - but a single tap that destroys
  // something is not a thing to offer either.
  const [confirming, setConfirming] = useState(false)
  const image = post.media[0]
  const withheld = post.moderationStatus === 'flagged' || post.moderationStatus === 'rejected'
  const pending = post.moderationStatus === 'pending'

  return (
    <Card className="flex gap-4 p-4">
      {image ? (
        <img
          src={image.url}
          alt=""
          className="size-24 shrink-0 rounded-lg object-cover"
        />
      ) : (
        <div className="grid size-24 shrink-0 place-items-center rounded-lg bg-sand-200 text-sand-400">
          <Plus className="size-5" aria-hidden />
        </div>
      )}

      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="truncate text-base font-semibold text-sand-900">{post.title}</h2>
          <Badge tone={STATUS_TONE[post.status] ?? 'neutral'}>{post.status}</Badge>
          {withheld && (
            <Badge tone="danger" icon={<ShieldAlert className="size-3" aria-hidden />}>
              Withheld
            </Badge>
          )}
          {pending && (
            <Badge tone="accent" icon={<AlertTriangle className="size-3" aria-hidden />}>
              In review
            </Badge>
          )}
        </div>

        <p className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-sm text-sand-500">
          {post.category && <span>{post.category.name}</span>}
          {post.venue && (
            <>
              <span aria-hidden>·</span>
              <span className="truncate">{post.venue.name}</span>
            </>
          )}
          <span aria-hidden>·</span>
          <span>{formatPrice(post.price)}</span>
          {post.nextEvent && (
            <>
              <span aria-hidden>·</span>
              <span>{formatWhen(post.nextEvent.startTime)}</span>
            </>
          )}
        </p>

        {/* The author is told why, not just that. */}
        {withheld && (
          <p className="mt-2 rounded-lg bg-red-50 px-3 py-2 text-xs text-red-700">
            Withheld from discovery while it is reviewed
            {post.reportCount > 0 && ` after ${post.reportCount} report${post.reportCount > 1 ? 's' : ''}`}
            . Your post has not been deleted.
          </p>
        )}
        {pending && post.moderationNotes && (
          <p className="mt-2 rounded-lg bg-accent-100/50 px-3 py-2 text-xs text-accent-700">
            {post.moderationNotes}
          </p>
        )}
        {post.status === 'draft' && post.readinessProblems.length > 0 && (
          <p className="mt-2 text-xs text-sand-500">
            {post.readinessProblems.length} thing
            {post.readinessProblems.length > 1 ? 's' : ''} left before it can go live
          </p>
        )}

        <div className="mt-3 flex flex-wrap gap-2">
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
              disabled={busy || post.readinessProblems.length > 0}
            >
              Publish
            </Button>
          )}

          {/* Last, and apart, because it is the only one that cannot be taken
              back. The server refuses it once somebody holds a ticket. */}
          {confirming ? (
            <span className="flex items-center gap-1.5">
              <span className="text-xs text-sand-600">Delete this?</span>
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
              className="text-red-700 hover:bg-red-50"
            >
              <Trash2 className="size-3.5" aria-hidden />
              Delete
            </Button>
          )}
        </div>
      </div>
    </Card>
  )
}
