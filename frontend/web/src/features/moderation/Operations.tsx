/**
 * Feature flags and the audit trail (spec ADM-003, ADM-004).
 *
 * Together in one tab because they are two halves of the same thing: flags are
 * the levers, and the record is what was pulled. Splitting them would put the
 * evidence a page away from the action.
 *
 * The record is presented as read-only, and it genuinely is - there is no
 * endpoint that edits or removes an entry. It is also deliberately not a
 * general activity feed: nothing an explorer does appears here, only what
 * administrators do.
 */

import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Plus, ToggleLeft } from 'lucide-react'

import { api } from '@/lib/api'
import type { AuditEntry, FeatureFlag } from '@/lib/types'
import { Badge, Button, Card, EmptyState, Input, SectionHeading } from '@/design-system/primitives'

/** Plain-language names, so the record reads as sentences rather than keys. */
const ACTION_LABELS: Record<string, string> = {
  'account.suspended': 'suspended',
  'account.restored': 'restored',
  'moderator.granted': 'made a moderator',
  'moderator.revoked': 'removed moderator rights from',
  'verification.approved': 'verified',
  'verification.refused': 'refused verification to',
  'content.approved': 'approved',
  'content.rejected': 'withheld',
  'flag.changed': 'changed the flag',
}

function when(iso: string): string {
  return new Date(iso).toLocaleString([], {
    day: 'numeric',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function FlagRow({ flag }: { flag: FeatureFlag }) {
  const queryClient = useQueryClient()
  const [rollout, setRollout] = useState(flag.rolloutPercentage)

  const update = useMutation({
    mutationFn: (patch: { enabled?: boolean; rolloutPercentage?: number }) =>
      api.updateFlag(flag.key, patch),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['flags'] })
      void queryClient.invalidateQueries({ queryKey: ['audit'] })
      void queryClient.invalidateQueries({ queryKey: ['my-flags'] })
    },
  })

  return (
    <div className="py-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="flex flex-wrap items-center gap-2 font-medium text-sand-900">
            <code className="rounded bg-sand-100 px-1.5 py-0.5 text-sm">{flag.key}</code>
            {flag.enabled ? (
              <Badge tone="success">
                On{flag.rolloutPercentage < 100 && ` for ${flag.rolloutPercentage}%`}
              </Badge>
            ) : (
              <Badge tone="neutral">Off</Badge>
            )}
          </p>
          <p className="mt-1 text-sm text-sand-600">{flag.description}</p>
        </div>

        <label className="flex cursor-pointer items-center gap-2 text-sm text-sand-700">
          <input
            type="checkbox"
            role="switch"
            checked={flag.enabled}
            disabled={update.isPending}
            onChange={(event) => update.mutate({ enabled: event.target.checked })}
            className="size-5 rounded border-sand-300 accent-brand-600"
          />
          Enabled
        </label>
      </div>

      {flag.enabled && (
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <label className="text-sm text-sand-600" htmlFor={`rollout-${flag.key}`}>
            Rolled out to
          </label>
          <Input
            id={`rollout-${flag.key}`}
            type="number"
            min={0}
            max={100}
            value={rollout}
            onChange={(event) => setRollout(Number(event.target.value))}
            className="w-20"
          />
          <span className="text-sm text-sand-600">% of explorers</span>
          {rollout !== flag.rolloutPercentage && (
            <Button
              size="sm"
              variant="secondary"
              disabled={update.isPending}
              onClick={() => update.mutate({ rolloutPercentage: rollout })}
            >
              Apply
            </Button>
          )}
          {/* Said explicitly, because a percentage that re-rolled per request
              would be a different and much worse feature. */}
          <span className="text-xs text-sand-500">
            The same explorers stay inside the rollout as it grows.
          </span>
        </div>
      )}

      {update.isError && (
        <p className="mt-2 text-sm text-red-700" role="alert">
          {(update.error as Error).message}
        </p>
      )}
    </div>
  )
}

function NewFlag() {
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(false)
  const [key, setKey] = useState('')
  const [description, setDescription] = useState('')

  const create = useMutation({
    mutationFn: () => api.createFlag(key.trim(), description.trim()),
    onSuccess: () => {
      setKey('')
      setDescription('')
      setOpen(false)
      void queryClient.invalidateQueries({ queryKey: ['flags'] })
      void queryClient.invalidateQueries({ queryKey: ['audit'] })
    },
  })

  if (!open) {
    return (
      <Button variant="secondary" size="sm" onClick={() => setOpen(true)}>
        <Plus className="size-4" aria-hidden />
        New flag
      </Button>
    )
  }

  return (
    <Card className="mt-3 p-4">
      <div className="space-y-2">
        <Input
          aria-label="Flag key"
          placeholder="concierge.voice"
          value={key}
          onChange={(event) => setKey(event.target.value)}
        />
        <Input
          aria-label="What it turns on"
          placeholder="What does this turn on?"
          value={description}
          onChange={(event) => setDescription(event.target.value)}
        />
      </div>
      <div className="mt-3 flex flex-wrap gap-2">
        <Button
          size="sm"
          disabled={create.isPending || !key.trim() || !description.trim()}
          onClick={() => create.mutate()}
        >
          {create.isPending ? 'Creating…' : 'Create'}
        </Button>
        <Button variant="ghost" size="sm" onClick={() => setOpen(false)}>
          Cancel
        </Button>
      </div>
      <p className="mt-2 text-xs text-sand-500">
        Created switched off. A flag that arrives on has shipped the feature by existing.
      </p>
      {create.isError && (
        <p className="mt-2 text-sm text-red-700" role="alert">
          {(create.error as Error).message}
        </p>
      )}
    </Card>
  )
}

function AuditRow({ entry }: { entry: AuditEntry }) {
  // Registering a flag and changing one are the same action on the wire, and
  // the context is what tells them apart. Reading "changed the flag X to off"
  // for a flag that did not exist a second earlier is a small lie the record
  // does not need to tell.
  const created = entry.context?.created === true
  const verb = created ? 'registered the flag' : (ACTION_LABELS[entry.action] ?? entry.action)
  const change = created
    ? undefined
    : (entry.context?.after as { enabled?: boolean; rolloutPercentage?: number } | undefined)

  return (
    <li className="border-t border-sand-200 py-3 first:border-t-0">
      <p className="text-sm text-sand-800">
        <span className="font-medium">{entry.actorLabel}</span> {verb}{' '}
        <span className="font-medium">{entry.subjectLabel ?? entry.subjectType}</span>
        {change && typeof change.enabled === 'boolean' && (
          <span className="text-sand-600">
            {' '}
            to {change.enabled ? 'on' : 'off'}
            {change.enabled && change.rolloutPercentage !== 100
              ? ` at ${change.rolloutPercentage}%`
              : ''}
          </span>
        )}
      </p>
      {entry.reason && <p className="mt-0.5 text-sm italic text-sand-600">“{entry.reason}”</p>}
      <p className="mt-0.5 text-xs text-sand-500">{when(entry.occurredAt)}</p>
    </li>
  )
}

export function Operations() {
  const { data: flags, isError, error } = useQuery({
    queryKey: ['flags'],
    queryFn: () => api.flags(),
    retry: false,
  })

  const { data: entries } = useQuery({
    queryKey: ['audit'],
    queryFn: () => api.auditTrail(30),
    retry: false,
  })

  if (isError) {
    return (
      <Card className="mt-6 p-5">
        <EmptyState
          icon={<ToggleLeft className="size-8" />}
          title="Not available"
          description={(error as Error).message}
        />
      </Card>
    )
  }

  return (
    <>
      <section className="mt-4">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <SectionHeading
            title="Feature flags"
            subtitle="Product decisions you can change without a deploy. Infrastructure settings are not here."
          />
          <NewFlag />
        </div>

        {flags && flags.length > 0 ? (
          <Card className="mt-3 divide-y divide-sand-200 px-5">
            {flags.map((flag) => (
              <FlagRow key={flag.key} flag={flag} />
            ))}
          </Card>
        ) : (
          <Card className="mt-3 p-5">
            <EmptyState
              icon={<ToggleLeft className="size-8" />}
              title="No flags yet"
              description="Register one when you have a feature worth turning on for some people first."
            />
          </Card>
        )}
      </section>

      <section className="mt-10">
        <SectionHeading
          title="What administrators have done"
          subtitle="Append only. Nothing an explorer does appears here - this records authority, not people."
        />
        {entries && entries.length > 0 ? (
          <Card className="mt-3 px-5 py-2">
            <ul>
              {entries.map((entry) => (
                <AuditRow key={entry.id} entry={entry} />
              ))}
            </ul>
          </Card>
        ) : (
          <Card className="mt-3 p-5 text-sm text-sand-500">
            Nothing in the last 30 days.
          </Card>
        )}
      </section>
    </>
  )
}
