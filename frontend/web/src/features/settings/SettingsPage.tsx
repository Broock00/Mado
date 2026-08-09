/**
 * Privacy controls and what Mado remembers (spec PRODUCT-00 principle 9).
 *
 * The backend already treated personalization as opt-in and memory as revocable.
 * Until this page existed those were promises with no way to act on them, which
 * is not meaningfully different from not having them: consent you cannot
 * withdraw is not consent.
 *
 * Two things this page deliberately does:
 *
 * - It shows *inferred* memories alongside stated ones, labelled. Someone should
 *   be able to see the guesses the platform made about them and disagree.
 * - It shows the decayed confidence, not the stored figure, because that is what
 *   is actually steering results today.
 */

import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Brain, Download, Lock, Trash2 } from 'lucide-react'

import { api } from '@/lib/api'
import { useAppStore } from '@/app/store'
import type { MemoryEntry, PrivacySettings } from '@/lib/types'
import { Button, Card, EmptyState, SectionHeading } from '@/design-system/primitives'
import { AccountSecurity } from './AccountSecurity'
import { MyActivity } from '@/features/analytics/MyActivity'

/**
 * Notification kinds, in the order they matter to an explorer.
 *
 * Reminders about things they saved or planned default on - saving an event is
 * the request. Suggestions default off, because nobody asked for those.
 */
const NOTIFICATION_KINDS = [
  {
    key: 'event_reminder',
    label: 'Reminders for things you saved',
    description: 'A few hours before something you saved is due to start.',
  },
  {
    key: 'plan_reminder',
    label: 'Reminders for your plans',
    description: 'Before a plan you kept is due to begin.',
  },
  {
    key: 'moderation_outcome',
    label: 'Decisions about your posts',
    description: 'When a moderator rules on something you published or reported.',
  },
  {
    key: 'nearby_suggestion',
    label: 'Suggestions near you',
    description: 'Occasional nudges when something you might like is on nearby. Off by default.',
  },
] as const

const TOGGLES: { key: keyof PrivacySettings; label: string; description: string }[] = [
  {
    key: 'personalizationEnabled',
    label: 'Personalized recommendations',
    description:
      'Uses what you save, open and dismiss to order results. Turning this off ranks everything the same way for everyone.',
  },
  {
    key: 'aiMemoryEnabled',
    label: 'Concierge memory',
    description:
      'Lets the concierge remember things you tell it about yourself, like a dietary restriction, between conversations.',
  },
  {
    key: 'locationEnabled',
    label: 'Location',
    description:
      'Allows distance and "near you" results. Your coordinates are used for ranking and are not stored on your profile.',
  },
  {
    key: 'analyticsEnabled',
    label: 'Usage analytics',
    description: 'Aggregate data about which features are used. Never sold or shared.',
  },
]

function Toggle({
  checked,
  onChange,
  label,
  description,
  disabled,
}: {
  checked: boolean
  onChange: (next: boolean) => void
  label: string
  description: string
  disabled?: boolean
}) {
  return (
    <label className="flex cursor-pointer items-start justify-between gap-4 py-4">
      <span className="min-w-0">
        <span className="block font-medium text-sand-900">{label}</span>
        <span className="mt-0.5 block text-sm text-sand-600">{description}</span>
      </span>
      <input
        type="checkbox"
        role="switch"
        checked={checked}
        disabled={disabled}
        onChange={(event) => onChange(event.target.checked)}
        className="mt-1 size-5 shrink-0 rounded border-sand-300 accent-brand-600"
      />
    </label>
  )
}

function MemoryRow({
  memory,
  onForget,
  forgetting,
}: {
  memory: MemoryEntry
  onForget: () => void
  forgetting: boolean
}) {
  return (
    <li className="flex items-center justify-between gap-4 py-3">
      <div className="min-w-0">
        <p className="text-sand-900">{memory.value}</p>
        <p className="mt-0.5 text-xs text-sand-500">
          {memory.attribute}
          {' · '}
          {memory.isExplicit ? 'you told the concierge' : 'inferred from what you did'}
          {' · '}
          {Math.round(memory.confidence * 100)}% confidence
        </p>
      </div>
      <Button
        variant="ghost"
        size="sm"
        onClick={onForget}
        disabled={forgetting}
        aria-label={`Forget: ${memory.value}`}
      >
        <Trash2 className="size-4" aria-hidden />
      </Button>
    </li>
  )
}

export function SettingsPage() {
  const user = useAppStore((s) => s.user)
  const setUser = useAppStore((s) => s.setUser)
  const queryClient = useQueryClient()
  const [confirmingClear, setConfirmingClear] = useState(false)

  const privacy: PrivacySettings = {
    personalizationEnabled: true,
    locationEnabled: false,
    aiMemoryEnabled: true,
    analyticsEnabled: true,
    ...((user?.profile?.privacy as Partial<PrivacySettings>) ?? {}),
  }

  const { data: notificationPreferences } = useQuery({
    queryKey: ['notification-preferences'],
    queryFn: () => api.notificationPreferences(),
    enabled: Boolean(user),
  })
  const notificationKinds = notificationPreferences?.kinds

  const updateNotifications = useMutation({
    mutationFn: (kinds: Record<string, boolean>) => api.updateNotificationPreferences(kinds),
    onSuccess: () =>
      void queryClient.invalidateQueries({ queryKey: ['notification-preferences'] }),
  })

  const { data: memories, isLoading: loadingMemories } = useQuery({
    queryKey: ['memories'],
    queryFn: () => api.memories(),
    enabled: Boolean(user) && privacy.aiMemoryEnabled,
  })

  const updatePrivacy = useMutation({
    mutationFn: (next: Partial<PrivacySettings>) => api.updatePrivacy(next),
    onSuccess: (me) => setUser(me),
  })

  const forget = useMutation({
    mutationFn: (id: string) => api.forgetMemory(id),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['memories'] }),
  })

  const forgetAll = useMutation({
    mutationFn: () => api.forgetAllMemories(),
    onSuccess: () => {
      setConfirmingClear(false)
      void queryClient.invalidateQueries({ queryKey: ['memories'] })
    },
  })

  const exportData = useMutation({
    mutationFn: () => api.exportMyData(),
    onSuccess: (payload) => {
      // Downloaded client-side rather than emailed: the explorer already has the
      // data on screen, and a download needs no delivery channel to go wrong.
      const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' })
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = 'mado-my-data.json'
      link.click()
      URL.revokeObjectURL(url)
    },
  })

  if (!user) {
    return (
      <div className="mx-auto max-w-3xl px-4 py-16">
        <EmptyState
          icon={<Lock className="size-8" />}
          title="Sign in to manage your data"
          description="Privacy settings and concierge memory belong to your account."
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
      <h1 className="text-2xl font-semibold tracking-tight text-sand-900">
        Privacy and data
      </h1>
      <p className="mt-1 text-sand-600">
        What Mado uses to personalize your results, and what it remembers about you.
      </p>

      <Card className="mt-6 divide-y divide-sand-200 px-5">
        {TOGGLES.map((toggle) => (
          <Toggle
            key={toggle.key}
            label={toggle.label}
            description={toggle.description}
            checked={privacy[toggle.key]}
            disabled={updatePrivacy.isPending}
            onChange={(next) => updatePrivacy.mutate({ [toggle.key]: next })}
          />
        ))}
      </Card>

      {updatePrivacy.isError && (
        <p className="mt-2 text-sm text-red-700" role="alert">
          {(updatePrivacy.error as Error).message}
        </p>
      )}

      <MyActivity />

      <AccountSecurity />

      {/* Here rather than in the header. Keys and webhooks matter enormously to
          the few publishers who automate and not at all to anybody else, and a
          permanent nav icon for a page most people open once is clutter charged
          to everyone. */}
      <section className="mt-10">
        <SectionHeading
          title="Developers"
          subtitle="API keys and webhooks, for getting your listings in and out without a browser."
        />
        <Card className="mt-3 p-5">
          <Link to="/developers" className="text-brand-700 underline">
            Keys and webhooks
          </Link>
        </Card>
      </section>

      <section className="mt-10">
        <SectionHeading
          title="What you are told about"
          subtitle="Reminders about things you saved or planned are on; nothing else is."
        />
        <Card className="mt-3 divide-y divide-sand-200 px-5">
          {NOTIFICATION_KINDS.map((kind) => (
            <Toggle
              key={kind.key}
              label={kind.label}
              description={kind.description}
              checked={notificationKinds?.[kind.key] ?? false}
              disabled={updateNotifications.isPending}
              onChange={(next) => updateNotifications.mutate({ [kind.key]: next })}
            />
          ))}
        </Card>
      </section>

      <section className="mt-10">
        <SectionHeading
          title="What the concierge remembers"
          subtitle="Things you have told it, and things it worked out from what you did."
        />

        {!privacy.aiMemoryEnabled ? (
          <Card className="mt-3 p-5 text-sm text-sand-600">
            Concierge memory is switched off, so nothing new is being recorded and
            nothing is being read. Anything stored previously is kept until you
            clear it below.
          </Card>
        ) : loadingMemories ? (
          <Card className="mt-3 p-5 text-sm text-sand-500">Loading…</Card>
        ) : !memories || memories.length === 0 ? (
          <Card className="mt-3 p-5">
            <EmptyState
              icon={<Brain className="size-8" />}
              title="Nothing remembered yet"
              description="Tell the concierge something about yourself - a dietary restriction, or what you enjoy - and it will remember for next time."
            />
          </Card>
        ) : (
          <Card className="mt-3 divide-y divide-sand-200 px-5">
            {memories.map((memory) => (
              <MemoryRow
                key={memory.id}
                memory={memory}
                forgetting={forget.isPending}
                onForget={() => forget.mutate(memory.id)}
              />
            ))}
          </Card>
        )}

        <div className="mt-4 flex flex-wrap items-center gap-3">
          {confirmingClear ? (
            <>
              <span className="text-sm text-sand-700">
                Forget everything? This cannot be undone.
              </span>
              <Button
                variant="danger"
                size="sm"
                onClick={() => forgetAll.mutate()}
                disabled={forgetAll.isPending}
              >
                {forgetAll.isPending ? 'Clearing…' : 'Yes, forget it all'}
              </Button>
              <Button variant="ghost" size="sm" onClick={() => setConfirmingClear(false)}>
                Cancel
              </Button>
            </>
          ) : (
            <Button
              variant="secondary"
              size="sm"
              onClick={() => setConfirmingClear(true)}
              disabled={!memories || memories.length === 0}
            >
              <Trash2 className="size-4" aria-hidden />
              Forget everything
            </Button>
          )}

          <Button
            variant="ghost"
            size="sm"
            onClick={() => exportData.mutate()}
            disabled={exportData.isPending}
          >
            <Download className="size-4" aria-hidden />
            {exportData.isPending ? 'Preparing…' : 'Download my data'}
          </Button>
        </div>
      </section>
    </div>
  )
}
