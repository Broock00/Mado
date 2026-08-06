/**
 * The moderator console (spec BUSINESS-07).
 *
 * The governing rule is that automated systems detect and humans decide, which
 * only holds if the humans have somewhere to decide. Until this existed the
 * queue could only be worked through the API by hand, so in practice flagged
 * content sat withheld indefinitely - detection without adjudication.
 *
 * Two things the design takes seriously:
 *
 * - **The reasoning is shown, not the score.** A moderator seeing "0.85" learns
 *   nothing actionable. The screening signals say what was actually detected,
 *   and a decision made from those is reviewable afterwards.
 * - **Both outcomes are equally available.** The approve and reject actions are
 *   presented with the same weight. A queue that makes rejection the easy path
 *   trains moderators to reject.
 */

import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { CheckCircle2, Flag, ShieldCheck, XCircle } from 'lucide-react'

import { api } from '@/lib/api'
import { useAppStore } from '@/app/store'
import type { ModerationItem } from '@/lib/types'
import { Badge, Button, Card, EmptyState, Input } from '@/design-system/primitives'

/** Risk shown as a band, because the exact figure is not what a decision turns on. */
function riskBand(score: number): { label: string; tone: 'danger' | 'warning' | 'neutral' } {
  if (score >= 0.7) return { label: 'High risk', tone: 'danger' }
  if (score >= 0.4) return { label: 'Some signals', tone: 'warning' }
  return { label: 'Low risk', tone: 'neutral' }
}

/**
 * The route by which this reached the queue.
 *
 * Mirrors the two triggers in the trust service: a screening score at or above
 * the review threshold, and reports reaching their own threshold (or a single
 * scam report, which does not wait). Stated plainly because "why am I looking at
 * this" is the first question a moderator has, and the screening note alone
 * frequently answers a different one.
 */
function queueReason(item: ModerationItem): string {
  const flaggedByScreening = item.riskScore >= 0.7
  const reported = item.reportCount > 0

  if (flaggedByScreening && reported) {
    return `Automated screening flagged this, and ${item.reportCount} ${
      item.reportCount === 1 ? 'reader has' : 'readers have'
    } reported it.`
  }
  if (flaggedByScreening) {
    return 'Automated screening flagged this before it reached anyone.'
  }
  if (reported) {
    return `${item.reportCount} ${
      item.reportCount === 1 ? 'reader' : 'readers'
    } reported this. Screening itself found nothing unusual, so the reports are the whole case.`
  }
  return 'Awaiting a routine first review.'
}

function QueueRow({
  item,
  onDecide,
  deciding,
}: {
  item: ModerationItem
  onDecide: (approve: boolean, note: string) => void
  deciding: boolean
}) {
  const [note, setNote] = useState('')
  const band = riskBand(item.riskScore)

  return (
    <Card className="p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <Link
            to={`/experiences/${item.id}`}
            className="text-lg font-medium text-sand-900 hover:underline"
          >
            {item.title}
          </Link>
          <p className="mt-0.5 text-sm text-sand-600">
            {item.publisherName ?? 'Unknown publisher'}
            {' · '}
            {new Date(item.createdAt).toLocaleDateString([], {
              day: 'numeric',
              month: 'short',
              year: 'numeric',
            })}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Badge tone={band.tone === 'danger' ? 'danger' : 'neutral'}>{band.label}</Badge>
          {item.reportCount > 0 && (
            <Badge tone="neutral">
              <Flag className="size-3" aria-hidden />
              {item.reportCount} {item.reportCount === 1 ? 'report' : 'reports'}
            </Badge>
          )}
        </div>
      </div>

      {item.summary && <p className="mt-3 text-sm text-sand-700">{item.summary}</p>}

      {/* Why it is in the queue, stated before the screening notes.
          Content arrives here by two different routes and the distinction
          changes the decision: automated screening flagged it, or readers
          reported it. Showing only the screening note made a reported item look
          reassuring - "a harmless description of a poetry event" sat directly
          beside "3 reports", which tells a moderator nothing about the reports. */}
      <div className="mt-3 rounded-lg bg-sand-100 px-3 py-2">
        <p className="text-xs font-medium uppercase tracking-wide text-sand-500">
          Why it is here
        </p>
        <p className="mt-1 text-sm text-sand-700">{queueReason(item)}</p>
        {item.moderationNotes && (
          <p className="mt-2 text-sm text-sand-600">
            <span className="text-sand-500">Automated screening: </span>
            {item.moderationNotes.replace(/^Automated screening:\s*/i, '')}
          </p>
        )}
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-3">
        <Input
          aria-label={`Note on ${item.title}`}
          placeholder="Note (optional, kept on the record)"
          value={note}
          onChange={(event) => setNote(event.target.value)}
          className="min-w-[14rem] flex-1"
        />
        <Button
          variant="secondary"
          size="sm"
          disabled={deciding}
          onClick={() => onDecide(true, note)}
        >
          <CheckCircle2 className="size-4" aria-hidden />
          Approve
        </Button>
        <Button
          variant="secondary"
          size="sm"
          disabled={deciding}
          onClick={() => onDecide(false, note)}
        >
          <XCircle className="size-4" aria-hidden />
          Reject
        </Button>
      </div>

      <p className="mt-2 text-xs text-sand-500">
        Either decision is final for automated screening: it will not re-flag this
        afterwards. Rejecting withholds it from discovery; the author keeps their copy.
      </p>
    </Card>
  )
}

export function ModerationPage() {
  const user = useAppStore((s) => s.user)
  const queryClient = useQueryClient()

  const { data: queue, isLoading, isError, error } = useQuery({
    queryKey: ['moderation-queue'],
    queryFn: () => api.moderationQueue(),
    enabled: Boolean(user),
    // Moderation is a shared queue - two people working it should not both spend
    // time on an item the other already decided.
    staleTime: 0,
  })

  const decide = useMutation({
    mutationFn: ({
      id,
      approve,
      note,
    }: {
      id: string
      approve: boolean
      note: string
    }) => api.decideModeration(id, approve, note.trim() || undefined),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['moderation-queue'] }),
  })

  if (!user) {
    return (
      <div className="mx-auto max-w-3xl px-4 py-16">
        <EmptyState
          icon={<ShieldCheck className="size-8" />}
          title="Sign in"
          description="The moderation queue is available to moderators."
          action={
            <Link to="/signin">
              <Button>Sign in</Button>
            </Link>
          }
        />
      </div>
    )
  }

  // A 403 here is the API's own check refusing, which is the authority. The
  // client-side isModerator flag only decides whether to show the nav link.
  if (isError) {
    return (
      <div className="mx-auto max-w-3xl px-4 py-16">
        <EmptyState
          icon={<ShieldCheck className="size-8" />}
          title="Not available"
          description={(error as Error).message}
        />
      </div>
    )
  }

  return (
    <div className="mx-auto w-full max-w-3xl px-4 pb-24 pt-6 sm:px-6">
      <h1 className="text-2xl font-semibold tracking-tight text-sand-900">Moderation</h1>
      <p className="mt-1 text-sand-600">
        Content withheld from discovery pending a decision. Nothing here has been
        deleted, and nothing will be without one.
      </p>

      {isLoading && <Card className="mt-6 p-5 text-sm text-sand-500">Loading queue…</Card>}

      {!isLoading && queue && queue.length === 0 && (
        <Card className="mt-6 p-5">
          <EmptyState
            icon={<ShieldCheck className="size-8" />}
            title="Queue is clear"
            description="Nothing is waiting on a decision."
          />
        </Card>
      )}

      {queue && queue.length > 0 && (
        <ul className="mt-6 space-y-4">
          {queue.map((item) => (
            <li key={item.id}>
              <QueueRow
                item={item}
                deciding={decide.isPending}
                onDecide={(approve, note) => decide.mutate({ id: item.id, approve, note })}
              />
            </li>
          ))}
        </ul>
      )}

      {decide.isError && (
        <p className="mt-3 text-sm text-red-700" role="alert">
          {(decide.error as Error).message}
        </p>
      )}
    </div>
  )
}
