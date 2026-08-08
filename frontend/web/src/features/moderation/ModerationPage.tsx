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
 *
 * Four surfaces, because they are four things a moderator does about the same
 * city and splitting them across pages would hide three of them: content
 * waiting on a ruling, publishers waiting on verification, accounts, and the
 * levers plus the record of who pulled them.
 *
 * Every action here is reversible and none of them delete anything - which is
 * the whole of spec BUSINESS-07 restated as an interface constraint.
 */

import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  BadgeCheck,
  CheckCircle2,
  Flag,
  Search,
  ShieldCheck,
  SlidersHorizontal,
  UserCog,
  XCircle,
} from 'lucide-react'

import { api } from '@/lib/api'
import { useAppStore } from '@/app/store'
import type { AdminAccount, ModerationItem, PublisherVerification } from '@/lib/types'
import { Badge, Button, Card, EmptyState, Input } from '@/design-system/primitives'
import { cn } from '@/lib/utils'
import { Operations } from './Operations'

const TABS = [
  { key: 'content', label: 'Content', icon: ShieldCheck },
  { key: 'verification', label: 'Verification', icon: BadgeCheck },
  { key: 'accounts', label: 'Accounts', icon: UserCog },
  { key: 'operations', label: 'Operations', icon: SlidersHorizontal },
] as const

type TabKey = (typeof TABS)[number]['key']

function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString([], {
    day: 'numeric',
    month: 'short',
    year: 'numeric',
  })
}

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

function ContentQueue() {
  const queryClient = useQueryClient()

  const { data: queue, isLoading, isError, error } = useQuery({
    queryKey: ['moderation-queue'],
    queryFn: () => api.moderationQueue(),
    // Moderation is a shared queue - two people working it should not both spend
    // time on an item the other already decided.
    staleTime: 0,
  })

  const decide = useMutation({
    mutationFn: ({ id, approve, note }: { id: string; approve: boolean; note: string }) =>
      api.decideModeration(id, approve, note.trim() || undefined),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['moderation-queue'] }),
  })

  // A 403 here is the API's own check refusing, which is the authority. The
  // client-side isModerator flag only decides whether to show the nav link.
  if (isError) {
    return (
      <Card className="mt-6 p-5">
        <EmptyState
          icon={<ShieldCheck className="size-8" />}
          title="Not available"
          description={(error as Error).message}
        />
      </Card>
    )
  }

  return (
    <>
      <p className="mt-4 text-sand-600">
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
    </>
  )
}

/**
 * Publishers waiting to be verified.
 *
 * Verification is a claim about identity, not about quality - so what a
 * moderator is shown is the evidence the publisher offered, and nothing about
 * how good their events are. Refusal is not a dead end: it returns them to
 * unverified so someone refused for a blurry licence can come back with a
 * better one.
 */
function VerificationQueue() {
  const queryClient = useQueryClient()
  const [notes, setNotes] = useState<Record<string, string>>({})

  const { data: pending, isLoading, isError, error } = useQuery({
    queryKey: ['pending-verifications'],
    queryFn: () => api.pendingVerifications(),
    staleTime: 0,
  })

  const decide = useMutation({
    mutationFn: ({ id, approve, note }: { id: string; approve: boolean; note: string }) =>
      api.decideVerification(id, approve, note.trim() || undefined),
    onSuccess: () =>
      void queryClient.invalidateQueries({ queryKey: ['pending-verifications'] }),
  })

  if (isError) {
    return (
      <Card className="mt-6 p-5">
        <EmptyState
          icon={<BadgeCheck className="size-8" />}
          title="Not available"
          description={(error as Error).message}
        />
      </Card>
    )
  }

  return (
    <>
      <p className="mt-4 text-sand-600">
        Publishers who asked to be verified. A badge says somebody checked who they
        are - it says nothing about whether their events are any good, so weigh the
        evidence and not the listings.
      </p>

      {isLoading && <Card className="mt-6 p-5 text-sm text-sand-500">Loading…</Card>}

      {!isLoading && pending && pending.length === 0 && (
        <Card className="mt-6 p-5">
          <EmptyState
            icon={<BadgeCheck className="size-8" />}
            title="Nobody waiting"
            description="No publisher has an open verification request."
          />
        </Card>
      )}

      {pending && pending.length > 0 && (
        <ul className="mt-6 space-y-4">
          {pending.map((publisher) => (
            <li key={publisher.id}>
              <VerificationRow
                publisher={publisher}
                note={notes[publisher.id] ?? ''}
                onNote={(value) => setNotes((current) => ({ ...current, [publisher.id]: value }))}
                deciding={decide.isPending}
                onDecide={(approve) =>
                  decide.mutate({ id: publisher.id, approve, note: notes[publisher.id] ?? '' })
                }
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
    </>
  )
}

function VerificationRow({
  publisher,
  note,
  onNote,
  onDecide,
  deciding,
}: {
  publisher: PublisherVerification
  note: string
  onNote: (value: string) => void
  onDecide: (approve: boolean) => void
  deciding: boolean
}) {
  return (
    <Card className="p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <Link
            to={`/publishers/${publisher.slug}`}
            className="text-lg font-medium text-sand-900 hover:underline"
          >
            {publisher.name}
          </Link>
          <p className="mt-0.5 text-sm text-sand-600">
            Asked{' '}
            {publisher.verificationRequestedAt
              ? formatDate(publisher.verificationRequestedAt)
              : 'recently'}
          </p>
        </div>
        <Badge tone="neutral">Trust {publisher.trustLevel}</Badge>
      </div>

      <div className="mt-3 rounded-lg bg-sand-100 px-3 py-2">
        <p className="text-xs font-medium uppercase tracking-wide text-sand-500">
          What they offered as evidence
        </p>
        <p className="mt-1 text-sm text-sand-700">
          {publisher.verificationNote?.trim() || 'They gave no supporting detail.'}
        </p>
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-3">
        <Input
          aria-label={`Note on ${publisher.name}`}
          placeholder="Note (optional, kept on the record)"
          value={note}
          onChange={(event) => onNote(event.target.value)}
          className="min-w-[14rem] flex-1"
        />
        <Button variant="secondary" size="sm" disabled={deciding} onClick={() => onDecide(true)}>
          <CheckCircle2 className="size-4" aria-hidden />
          Verify
        </Button>
        <Button variant="secondary" size="sm" disabled={deciding} onClick={() => onDecide(false)}>
          <XCircle className="size-4" aria-hidden />
          Refuse
        </Button>
      </div>

      <p className="mt-2 text-xs text-sand-500">
        Refusing returns them to unverified rather than rejecting them permanently,
        so they can ask again with better evidence.
      </p>
    </Card>
  )
}

/**
 * Account administration.
 *
 * Search-only, on purpose. An administrator looking for a specific person
 * should search for them; a browsable list of everyone on the platform is a
 * surveillance surface with a workflow attached, and no decision needs it.
 */
function AccountAdmin({ actorId }: { actorId: string }) {
  const queryClient = useQueryClient()
  const [draft, setDraft] = useState('')
  const [query, setQuery] = useState('')

  const { data: accounts, isFetching, isError, error } = useQuery({
    queryKey: ['admin-accounts', query],
    queryFn: () => api.adminAccounts(query),
    enabled: query.trim().length > 0,
  })

  const invalidate = () =>
    void queryClient.invalidateQueries({ queryKey: ['admin-accounts', query] })

  const suspend = useMutation({
    mutationFn: ({ id, suspended, reason }: { id: string; suspended: boolean; reason?: string }) =>
      api.setAccountSuspended(id, suspended, reason),
    onSuccess: invalidate,
  })

  const moderator = useMutation({
    mutationFn: ({ id, isModerator }: { id: string; isModerator: boolean }) =>
      api.setAccountModerator(id, isModerator),
    onSuccess: invalidate,
  })

  const failure = (suspend.error ?? moderator.error ?? (isError ? error : null)) as Error | null

  return (
    <>
      <p className="mt-4 text-sand-600">
        Suspension withholds; it never deletes. A suspended account keeps its posts,
        its saved list and its history, and loses the ability to publish and to be
        seen. Lifting it restores everything.
      </p>

      <form
        className="mt-5 flex gap-2"
        onSubmit={(event) => {
          event.preventDefault()
          setQuery(draft.trim())
        }}
      >
        <Input
          aria-label="Search accounts by name or email"
          placeholder="Search by name or email"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          className="flex-1"
        />
        <Button type="submit" variant="secondary" disabled={!draft.trim()}>
          <Search className="size-4" aria-hidden />
          Search
        </Button>
      </form>

      {failure && (
        <p className="mt-3 text-sm text-red-700" role="alert">
          {failure.message}
        </p>
      )}

      {!query && (
        <Card className="mt-6 p-5">
          <EmptyState
            icon={<UserCog className="size-8" />}
            title="Search for an account"
            description="Accounts are looked up by name or email rather than browsed."
          />
        </Card>
      )}

      {query && isFetching && (
        <Card className="mt-6 p-5 text-sm text-sand-500">Searching…</Card>
      )}

      {query && !isFetching && accounts && accounts.length === 0 && (
        <Card className="mt-6 p-5">
          <EmptyState
            icon={<UserCog className="size-8" />}
            title="No match"
            description={`Nothing found for "${query}".`}
          />
        </Card>
      )}

      {accounts && accounts.length > 0 && (
        <ul className="mt-6 space-y-3">
          {accounts.map((account) => (
            <li key={account.id}>
              <AccountRow
                account={account}
                isSelf={account.id === actorId}
                busy={suspend.isPending || moderator.isPending}
                onSuspend={(suspended, reason) =>
                  suspend.mutate({ id: account.id, suspended, reason })
                }
                onModerator={(isModerator) =>
                  moderator.mutate({ id: account.id, isModerator })
                }
              />
            </li>
          ))}
        </ul>
      )}
    </>
  )
}

function AccountRow({
  account,
  isSelf,
  busy,
  onSuspend,
  onModerator,
}: {
  account: AdminAccount
  isSelf: boolean
  busy: boolean
  onSuspend: (suspended: boolean, reason?: string) => void
  onModerator: (moderator: boolean) => void
}) {
  const [confirming, setConfirming] = useState(false)
  const [reason, setReason] = useState('')
  const suspended = account.status === 'suspended'

  return (
    <Card className="p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="font-medium text-sand-900">
            {account.displayName}
            {isSelf && <span className="ml-2 text-sm font-normal text-sand-500">(you)</span>}
          </p>
          <p className="mt-0.5 truncate text-sm text-sand-600">{account.email}</p>
          <p className="mt-1 text-xs text-sand-500">
            Joined {formatDate(account.createdAt)}
            {' · '}
            {account.publishedCount} published
            {' · '}
            {account.reportedCount} {account.reportedCount === 1 ? 'report' : 'reports'}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {suspended && <Badge tone="danger">Suspended</Badge>}
          {account.isModerator && <Badge tone="neutral">Moderator</Badge>}
          {account.isVerified && <Badge tone="neutral">Verified</Badge>}
        </div>
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-3">
        {suspended ? (
          <Button variant="secondary" size="sm" disabled={busy} onClick={() => onSuspend(false)}>
            Lift suspension
          </Button>
        ) : confirming ? (
          <>
            <Input
              aria-label={`Reason for suspending ${account.displayName}`}
              placeholder="Reason (kept on the record)"
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              className="min-w-[14rem] flex-1"
            />
            <Button
              variant="danger"
              size="sm"
              disabled={busy}
              onClick={() => {
                onSuspend(true, reason.trim() || undefined)
                setConfirming(false)
              }}
            >
              Suspend
            </Button>
            <Button variant="ghost" size="sm" onClick={() => setConfirming(false)}>
              Cancel
            </Button>
          </>
        ) : (
          <Button
            variant="secondary"
            size="sm"
            disabled={busy || isSelf || account.isModerator}
            onClick={() => setConfirming(true)}
          >
            Suspend
          </Button>
        )}

        <Button
          variant="ghost"
          size="sm"
          disabled={busy || (isSelf && account.isModerator) || (suspended && !account.isModerator)}
          onClick={() => onModerator(!account.isModerator)}
        >
          {account.isModerator ? 'Remove moderator rights' : 'Make moderator'}
        </Button>
      </div>

      {/* The disabled buttons above are hints, not the enforcement - the API
          refuses these regardless. Saying why keeps a greyed-out control from
          reading as a bug. */}
      {isSelf && (
        <p className="mt-2 text-xs text-sand-500">
          You cannot suspend yourself or drop your own moderator rights - there
          would be nobody left to undo it.
        </p>
      )}
      {!isSelf && account.isModerator && !suspended && (
        <p className="mt-2 text-xs text-sand-500">
          Remove moderator rights before suspending. Privileges are withdrawn
          deliberately, never as a side effect.
        </p>
      )}
    </Card>
  )
}

export function ModerationPage() {
  const user = useAppStore((s) => s.user)
  const [tab, setTab] = useState<TabKey>('content')

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

  return (
    <div className="mx-auto w-full max-w-3xl px-4 pb-24 pt-6 sm:px-6">
      <h1 className="text-2xl font-semibold tracking-tight text-sand-900">Moderation</h1>

      <div className="mt-4 flex gap-1 border-b border-sand-200" role="tablist">
        {TABS.map(({ key, label, icon: Icon }) => (
          <button
            key={key}
            type="button"
            role="tab"
            aria-selected={tab === key}
            onClick={() => setTab(key)}
            className={cn(
              '-mb-px flex items-center gap-1.5 border-b-2 px-3 py-2 text-sm transition-colors',
              tab === key
                ? 'border-brand-600 font-medium text-sand-900'
                : 'border-transparent text-sand-600 hover:text-sand-900',
            )}
          >
            <Icon className="size-4" aria-hidden />
            {label}
          </button>
        ))}
      </div>

      {tab === 'content' && <ContentQueue />}
      {tab === 'verification' && <VerificationQueue />}
      {tab === 'accounts' && <AccountAdmin actorId={user.id} />}
      {tab === 'operations' && <Operations />}
    </div>
  )
}
