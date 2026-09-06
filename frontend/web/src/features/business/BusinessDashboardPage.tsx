/**
 * Managing a business: its profile, and who else may touch it (phase 2).
 *
 * **The permission list drives the interface, and is not the enforcement.** The
 * server returns what this caller may do, and sections they cannot use are
 * hidden rather than shown broken. Every action is still checked again when it
 * is attempted — hiding a button stops an honest person doing the wrong thing
 * by accident, and stops nobody else.
 *
 * Roles come from the server too, with the permissions each one carries. A
 * hard-coded caption here would eventually describe a role differently from the
 * thing that enforces it, and the person choosing would be reading a promise
 * nobody keeps.
 *
 * Sections live behind tabs rather than one long scroll. A team member who
 * only holds `team:manage` should not have to page past a profile they cannot
 * edit to reach the invite form; an owner editing social links should not lose
 * their place under a wall of earnings and plan cards.
 */

import { useState, type ReactNode } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  BadgeCheck,
  Building2,
  CreditCard,
  ExternalLink,
  Images,
  PenSquare,
  Trash2,
  UserPlus,
  Users,
  Wallet,
} from 'lucide-react'

import { ApiError, api } from '@/lib/api'
import type { Business, BusinessMember } from '@/lib/types'
import { BUSINESS_TYPES } from '@/lib/business'
import { money } from '@/lib/money'
import { cn } from '@/lib/utils'
import { Badge, Button, Card, EmptyState, Input, Skeleton } from '@/design-system/primitives'
import { BusinessGallerySection } from './BusinessGallerySection'
import { BusinessPlanSection } from './BusinessPlanSection'
import { SocialIcon } from './SocialIcon'
import { SOCIAL_PLATFORMS, normaliseSocialUrl } from './social'

type ManageTab = 'profile' | 'gallery' | 'plan' | 'earnings' | 'team'

function ProfileSection({ business }: { business: Business }) {
  const queryClient = useQueryClient()
  const [form, setForm] = useState({
    name: business.name,
    businessType: business.businessType ?? '',
    description: business.description ?? '',
    website: business.website ?? '',
  })
  // Every platform gets an entry, empty or not: the form is a fixed set of
  // fields, and a blank one is how a link is removed. Kept apart from `form`
  // above only because it is addressed by platform rather than by field name.
  const [social, setSocial] = useState<Record<string, string>>(() =>
    Object.fromEntries(
      SOCIAL_PLATFORMS.map((platform) => [platform.value, business.social?.[platform.value] ?? '']),
    ),
  )
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // What somebody typed that could not become a link. Named rather than
  // silently dropped: the server drops these too, and a field that empties
  // itself on the next load looks like the save failed for no reason.
  const unusable = SOCIAL_PLATFORMS.filter(
    (platform) => social[platform.value].trim() && !normaliseSocialUrl(social[platform.value]),
  )

  const save = useMutation({
    mutationFn: () =>
      api.updateBusiness(business.id, {
        name: form.name.trim(),
        businessType: form.businessType || null,
        description: form.description.trim() || null,
        website: form.website.trim() || null,
        // The whole set, every time. A partial object would leave a link the
        // publisher just cleared exactly where it was: the server replaces
        // `social` rather than merging into it.
        social: Object.fromEntries(
          SOCIAL_PLATFORMS.flatMap((platform) => {
            const url = normaliseSocialUrl(social[platform.value])
            return url ? [[platform.value, url]] : []
          }),
        ),
      }),
    onSuccess: (updated) => {
      setError(null)
      setSaved(true)
      // Show what was stored, not what was typed. `instagram.com/x` is saved as
      // `https://instagram.com/x`, and leaving the shorter one in the box makes
      // the next visit look like the field changed on its own.
      setSocial(
        Object.fromEntries(
          SOCIAL_PLATFORMS.map((platform) => [
            platform.value,
            updated.social?.[platform.value] ?? '',
          ]),
        ),
      )
      void queryClient.invalidateQueries({ queryKey: ['business', business.id] })
      void queryClient.invalidateQueries({ queryKey: ['my-businesses'] })
    },
    onError: (caught) =>
      setError(caught instanceof ApiError ? caught.message : 'That could not be saved.'),
  })

  return (
    <Card className="space-y-5 p-5 sm:p-6">
      <div>
        <h2 className="text-lg font-semibold tracking-tight text-sand-900">Profile</h2>
        <p className="mt-0.5 text-sm text-sand-500">
          Name, kind of place, and the links explorers see on your public page.
        </p>
      </div>

      <div className="space-y-3">
        <div>
          <label htmlFor="name" className="mb-1 block text-sm text-sand-700">
            Name
          </label>
          <Input
            id="name"
            value={form.name}
            onChange={(e) => {
              setForm({ ...form, name: e.target.value })
              setSaved(false)
            }}
            maxLength={200}
          />
        </div>

        <div>
          <label htmlFor="type" className="mb-1 block text-sm text-sand-700">
            Kind of place
          </label>
          <select
            id="type"
            value={form.businessType}
            onChange={(e) => {
              setForm({ ...form, businessType: e.target.value })
              setSaved(false)
            }}
            className="w-full rounded-lg border border-sand-300 bg-sand-100 px-3 py-2 text-sm text-sand-900"
          >
            <option value="">Not saying</option>
            {BUSINESS_TYPES.map((type) => (
              <option key={type.value} value={type.value}>
                {type.label}
              </option>
            ))}
          </select>
        </div>

        <div>
          <label htmlFor="description" className="mb-1 block text-sm text-sand-700">
            Description
          </label>
          <textarea
            id="description"
            value={form.description}
            onChange={(e) => {
              setForm({ ...form, description: e.target.value })
              setSaved(false)
            }}
            rows={4}
            maxLength={4000}
            className="w-full rounded-lg border border-sand-300 bg-sand-100 px-3 py-2 text-sm text-sand-900"
          />
        </div>

        <div>
          <label htmlFor="website" className="mb-1 block text-sm text-sand-700">
            Website
          </label>
          <Input
            id="website"
            value={form.website}
            onChange={(e) => {
              setForm({ ...form, website: e.target.value })
              setSaved(false)
            }}
            placeholder="https://…"
            maxLength={2000}
          />
        </div>
      </div>

      {/* Social links. Every platform gets a field whether or not it is used —
          a list you add rows to would make somebody choose the platform twice,
          once from a menu and once by pasting, and there are only seven. The
          mark sits beside its field so the row is identifiable at a glance,
          which is also how it will be shown on the public page. */}
      <div className="space-y-3 border-t border-sand-200 pt-4">
        <div>
          <h3 className="text-sm font-medium text-sand-700">Social links</h3>
          <p className="mt-1 text-xs text-sand-500">
            Shown on your public page as icons. Leave one blank to remove it.
          </p>
        </div>

        <div className="space-y-2">
          {SOCIAL_PLATFORMS.map((platform) => {
            const value = social[platform.value]
            const broken = Boolean(value.trim()) && !normaliseSocialUrl(value)
            const fieldId = `social-${platform.value}`
            return (
              <div key={platform.value}>
                <div className="flex items-center gap-2.5">
                  <label
                    htmlFor={fieldId}
                    className="grid size-9 shrink-0 place-items-center rounded-lg bg-sand-200 text-sand-600"
                    title={platform.label}
                  >
                    <SocialIcon platform={platform} />
                    <span className="sr-only">{platform.label}</span>
                  </label>
                  <Input
                    id={fieldId}
                    type="url"
                    inputMode="url"
                    value={value}
                    onChange={(e) => {
                      setSocial({ ...social, [platform.value]: e.target.value })
                      setSaved(false)
                    }}
                    placeholder={platform.example}
                    maxLength={500}
                    aria-invalid={broken}
                    aria-describedby={broken ? `${fieldId}-problem` : undefined}
                  />
                </div>
                {broken && (
                  <p id={`${fieldId}-problem`} className="ml-[2.875rem] mt-1 text-xs text-red-300">
                    That is not a link. Paste the address of your {platform.label} page, like{' '}
                    {platform.example}.
                  </p>
                )}
              </div>
            )
          })}
        </div>
      </div>

      {error && <p className="text-sm text-red-300">{error}</p>}

      <div className="flex items-center gap-3">
        <Button
          onClick={() => save.mutate()}
          loading={save.isPending}
          // Saving around a bad link would drop it server-side and report
          // success, which is indistinguishable from having saved it.
          disabled={unusable.length > 0}
        >
          Save
        </Button>
        {saved && !save.isPending && <span className="text-sm text-sand-500">Saved.</span>}
      </div>
    </Card>
  )
}

function MemberRow({
  member,
  businessId,
  roles,
}: {
  member: BusinessMember
  businessId: string
  roles: { value: string; label: string }[]
}) {
  const queryClient = useQueryClient()
  const [error, setError] = useState<string | null>(null)

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['business-members', businessId] })
  }
  const onError = (caught: unknown) =>
    setError(caught instanceof ApiError ? caught.message : 'That did not work.')

  const changeRole = useMutation({
    mutationFn: (role: string) => api.changeBusinessMemberRole(businessId, member.id, role),
    onSuccess: () => {
      setError(null)
      invalidate()
    },
    onError,
  })

  const remove = useMutation({
    mutationFn: () => api.removeBusinessMember(businessId, member.id),
    onSuccess: () => {
      setError(null)
      invalidate()
    },
    onError,
  })

  return (
    <li className="flex flex-wrap items-center gap-3 border-t border-sand-200 py-3 first:border-t-0">
      <div className="min-w-0 flex-1">
        <p className="truncate text-sand-900">
          {member.displayName ?? member.invitedEmail ?? 'Someone'}
        </p>
        <p className="text-xs text-sand-500">
          {member.status === 'invited' ? 'Invited, not yet accepted' : member.roleLabel}
          {member.invitedEmail && member.displayName ? ` · ${member.invitedEmail}` : ''}
        </p>
        {error && <p className="mt-1 text-xs text-red-300">{error}</p>}
      </div>

      {member.status === 'invited' && <Badge tone="neutral">Pending</Badge>}

      <select
        aria-label={`Role for ${member.displayName ?? member.invitedEmail ?? 'this member'}`}
        value={member.role}
        onChange={(e) => changeRole.mutate(e.target.value)}
        disabled={changeRole.isPending || remove.isPending}
        className="rounded-lg border border-sand-300 bg-sand-100 px-2 py-1.5 text-sm text-sand-900"
      >
        {roles.map((role) => (
          <option key={role.value} value={role.value}>
            {role.label}
          </option>
        ))}
      </select>

      <Button
        variant="ghost"
        size="sm"
        onClick={() => remove.mutate()}
        loading={remove.isPending}
        aria-label={`Remove ${member.displayName ?? member.invitedEmail ?? 'this member'}`}
      >
        <Trash2 className="size-4" aria-hidden />
      </Button>
    </li>
  )
}

function TeamSection({ businessId }: { businessId: string }) {
  const queryClient = useQueryClient()
  const [email, setEmail] = useState('')
  const [role, setRole] = useState('editor')
  const [error, setError] = useState<string | null>(null)

  const { data: roles } = useQuery({
    queryKey: ['business-roles'],
    queryFn: () => api.businessRoles(),
    staleTime: 60 * 60_000,
  })

  const { data: members, isLoading } = useQuery({
    queryKey: ['business-members', businessId],
    queryFn: () => api.businessMembers(businessId),
  })

  const invite = useMutation({
    mutationFn: () => api.inviteBusinessMember(businessId, email.trim(), role),
    onSuccess: () => {
      setEmail('')
      setError(null)
      void queryClient.invalidateQueries({ queryKey: ['business-members', businessId] })
    },
    onError: (caught) =>
      setError(
        caught instanceof ApiError ? caught.message : 'That invitation could not be sent.',
      ),
  })

  const options = (roles ?? []).map((r) => ({ value: r.value, label: r.label }))
  const chosen = roles?.find((r) => r.value === role)

  return (
    <Card className="space-y-5 p-5 sm:p-6">
      <div>
        <h2 className="text-lg font-semibold tracking-tight text-sand-900">Team</h2>
        <p className="mt-0.5 text-sm text-sand-500">
          People you invite can act for this business. They keep their own Mado account —
          you are giving them access, not sharing a login.
        </p>
      </div>

      <div className="flex flex-wrap items-end gap-2">
        <div className="min-w-[14rem] flex-1">
          <label htmlFor="invite-email" className="mb-1 block text-sm text-sand-700">
            Invite by email
          </label>
          <Input
            id="invite-email"
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="them@example.com"
          />
        </div>
        <div>
          <label htmlFor="invite-role" className="mb-1 block text-sm text-sand-700">
            As
          </label>
          <select
            id="invite-role"
            value={role}
            onChange={(e) => setRole(e.target.value)}
            className="rounded-lg border border-sand-300 bg-sand-100 px-3 py-2 text-sm text-sand-900"
          >
            {options.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </div>
        <Button
          onClick={() => invite.mutate()}
          loading={invite.isPending}
          disabled={!email.includes('@')}
        >
          <UserPlus className="size-4" aria-hidden /> Invite
        </Button>
      </div>

      {/* What the chosen role actually permits, from the server. A caption
          written here would eventually disagree with what is enforced. */}
      {chosen && (
        <p className="text-xs text-sand-500">
          {chosen.label} can: {chosen.permissions.join(', ')}
        </p>
      )}

      {error && <p className="text-sm text-red-300">{error}</p>}

      {isLoading ? (
        <Skeleton className="h-16 w-full rounded-lg" />
      ) : !members || members.length === 0 ? (
        <p className="text-sm text-sand-500">
          Nobody else has access yet. It is just you.
        </p>
      ) : (
        <ul>
          {members.map((member) => (
            <MemberRow
              key={member.id}
              member={member}
              businessId={businessId}
              roles={options}
            />
          ))}
        </ul>
      )}
    </Card>
  )
}

/**
 * What the business has taken, and what Mado still owes it.
 *
 * Every ticket is paid into Mado's own merchant account, so the business's
 * share is a debt rather than money already in its bank. That is the one thing
 * this section has to be honest about: it shows the commission as a named
 * deduction and says outright that the balance is paid out by hand, because a
 * figure labelled "earnings" that never arrives is worse than no figure.
 */
function EarningsSection({ businessId }: { businessId: string }) {
  const { data, isLoading } = useQuery({
    queryKey: ['business-earnings', businessId],
    queryFn: () => api.businessEarnings(businessId),
    enabled: Boolean(businessId),
  })

  if (isLoading) return <Skeleton className="h-40 w-full rounded-xl" />
  if (!data) return null

  // Basis points to a readable percentage. Trailing zeroes trimmed, so 500
  // reads as "5%" rather than "5.00%" while 250 still reads as "2.5%".
  const rate = `${Number((data.feeRateBps / 100).toFixed(2))}%`

  return (
    <Card className="space-y-5 p-5 sm:p-6">
      <div>
        <h2 className="text-lg font-semibold tracking-tight text-sand-900">Earnings</h2>
        <p className="mt-0.5 text-sm text-sand-500">
          What ticket sales have brought in, and what Mado still owes you.
        </p>
      </div>

      {data.totals.length === 0 ? (
        <p className="text-sm text-sand-600">
          No ticket sales yet. When somebody buys a ticket, what you have earned appears
          here.
        </p>
      ) : (
        <div className="space-y-4">
          {data.totals.map((line) => (
            <div key={line.currency} className="rounded-xl bg-sand-50 p-4">
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <span className="text-xl font-semibold text-sand-900">
                  {money(line.netMinor, line.currency)}
                </span>
                <span className="text-sm text-sand-500">
                  from {line.sales} {line.sales === 1 ? 'sale' : 'sales'}
                </span>
              </div>
              {/* Spelled out rather than left as the difference between two
                  numbers. A publisher who cannot see where the gap went
                  assumes an error, and asks. */}
              <dl className="mt-3 space-y-1 text-sm text-sand-600">
                <div className="flex justify-between gap-4">
                  <dt>Tickets sold</dt>
                  <dd>{money(line.grossMinor, line.currency)}</dd>
                </div>
                <div className="flex justify-between gap-4">
                  <dt>Mado's commission ({rate})</dt>
                  <dd>−{money(line.feeMinor, line.currency)}</dd>
                </div>
                <div className="flex justify-between gap-4 font-medium text-sand-900">
                  <dt>Still to be paid to you</dt>
                  <dd>{money(line.owingMinor, line.currency)}</dd>
                </div>
              </dl>
            </div>
          ))}
          <p className="text-sm text-sand-500">
            Payouts are sent by hand while Mado is young — we will be in touch about
            where to send yours.
          </p>
        </div>
      )}

      {data.payouts.length > 0 && (
        <div className="space-y-2 border-t border-sand-200 pt-4">
          <h3 className="text-sm font-medium text-sand-900">Payouts</h3>
          {data.payouts.map((payout) => (
            <div
              key={payout.id}
              className="flex flex-wrap items-center justify-between gap-2 text-sm"
            >
              <span className="text-sand-700">
                {money(payout.totalMinor, payout.currency)}
                <span className="text-sand-500">
                  {' '}
                  · {payout.entryCount} {payout.entryCount === 1 ? 'sale' : 'sales'}
                </span>
              </span>
              <Badge tone={payout.status === 'paid' ? 'success' : 'neutral'}>
                {payout.status === 'paid' ? 'Sent' : 'Being prepared'}
              </Badge>
            </div>
          ))}
        </div>
      )}
    </Card>
  )
}

/**
 * One section at a time. Same roving-tabindex pattern as the public profile:
 * one Tab stop for the strip, arrow keys move within it. Tabs the caller
 * cannot use are omitted rather than disabled — a disabled "Team" tab still
 * announces that the section exists, and for a role that should not know
 * about inviting people that is information they do not need.
 *
 * Underline tabs rather than filled pills: this page already has enough
 * orange from the brand actions, and a row of solid chips competed with the
 * header. The active mark is a brand bar under the label so the strip reads
 * as navigation, not as another button group.
 */
function ManageTabs({
  tabs,
  children,
}: {
  tabs: { id: ManageTab; label: string; icon: ReactNode }[]
  children: (tab: ManageTab) => ReactNode
}) {
  const [tab, setTab] = useState<ManageTab>(tabs[0]?.id ?? 'profile')

  if (tabs.length === 0) return null

  // Derived rather than stored: permissions can arrive after the first paint,
  // and a selected tab that is no longer in the list would leave an empty panel.
  const active: ManageTab = tabs.some((entry) => entry.id === tab) ? tab : tabs[0].id

  // A single section does not need a tablist — the strip would only restate
  // the heading already on the card.
  if (tabs.length === 1) {
    return (
      <div role="tabpanel" id={`manage-panel-${active}`} aria-label={tabs[0].label}>
        {children(active)}
      </div>
    )
  }

  return (
    <section aria-label="Manage sections" className="space-y-5">
      <div className="sticky top-16 z-20 -mx-4 border-b border-sand-200 bg-sand-50/90 px-4 backdrop-blur-md sm:-mx-6 sm:px-6">
        <div
          role="tablist"
          aria-label="Manage sections"
          className="scrollbar-none flex gap-0 overflow-x-auto"
        >
          {tabs.map(({ id, label, icon }) => {
            const selected = active === id
            return (
              <button
                key={id}
                role="tab"
                type="button"
                id={`manage-tab-${id}`}
                aria-selected={selected}
                aria-controls={`manage-panel-${id}`}
                tabIndex={selected ? 0 : -1}
                onClick={() => setTab(id)}
                onKeyDown={(event) => {
                  if (event.key !== 'ArrowRight' && event.key !== 'ArrowLeft') return
                  const delta = event.key === 'ArrowRight' ? 1 : -1
                  const from = tabs.findIndex((entry) => entry.id === active)
                  const next = tabs[(from + delta + tabs.length) % tabs.length]
                  setTab(next.id)
                  document.getElementById(`manage-tab-${next.id}`)?.focus()
                }}
                className={cn(
                  'relative flex shrink-0 items-center gap-2 px-3.5 py-3 text-sm font-medium transition-colors',
                  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500/40 focus-visible:ring-offset-2 focus-visible:ring-offset-sand-50',
                  selected
                    ? 'text-sand-950'
                    : 'text-sand-500 hover:text-sand-800',
                )}
              >
                <span
                  className={cn(
                    'grid size-7 place-items-center rounded-lg transition-colors',
                    selected
                      ? 'bg-brand-600/15 text-brand-500'
                      : 'bg-sand-200/80 text-sand-500',
                  )}
                >
                  {icon}
                </span>
                {label}
                <span
                  aria-hidden
                  className={cn(
                    'absolute inset-x-2 bottom-0 h-0.5 rounded-full transition-colors',
                    selected ? 'bg-brand-500' : 'bg-transparent',
                  )}
                />
              </button>
            )
          })}
        </div>
      </div>

      <div
        role="tabpanel"
        id={`manage-panel-${active}`}
        aria-labelledby={`manage-tab-${active}`}
        key={active}
        className="motion-safe:animate-[managePanelIn_220ms_var(--ease-out-soft)]"
      >
        {children(active)}
      </div>
    </section>
  )
}

function ManageHeader({ business }: { business: Business }) {
  const verified = business.verificationStatus === 'verified'

  return (
    <div className="overflow-hidden rounded-2xl border border-sand-200 bg-sand-100 shadow-card">
      {business.coverUrl ? (
        <img
          src={business.coverUrl}
          alt=""
          className="h-28 w-full object-cover sm:h-36"
          loading="lazy"
        />
      ) : (
        // Soft brand wash rather than a blank strip — manage is a working surface,
        // and an empty grey band under the shell reads as unfinished layout.
        <div
          className="h-20 w-full bg-gradient-to-br from-brand-900/40 via-sand-100 to-sand-200 sm:h-24"
          aria-hidden
        />
      )}

      <div className="relative px-5 pb-5 pt-0 sm:px-6 sm:pb-6">
        <div className="-mt-8 mb-4 flex flex-wrap items-end justify-between gap-3 sm:-mt-10">
          {business.logoUrl ? (
            <img
              src={business.logoUrl}
              alt=""
              className="size-16 rounded-xl object-cover shadow-lifted ring-2 ring-sand-100 sm:size-[4.5rem]"
              loading="lazy"
            />
          ) : (
            <span className="grid size-16 place-items-center rounded-xl bg-sand-200 shadow-lifted ring-2 ring-sand-100 sm:size-[4.5rem]">
              <Building2 className="size-7 text-brand-500" aria-hidden />
            </span>
          )}

          <div className="flex flex-wrap items-center gap-2 pb-0.5">
            {/* No `?publisher=` for the owner: their account *is* this business, so
                the composer resolves it without being told. The parameter still
                exists for a team member, whose own account is somebody else. */}
            <Link to="/compose">
              <Button size="sm">
                <PenSquare className="size-3.5" aria-hidden />
                Write a post
              </Button>
            </Link>
            <Link to={`/businesses/${business.slug}`}>
              <Button variant="secondary" size="sm">
                <ExternalLink className="size-3.5" aria-hidden />
                Public page
              </Button>
            </Link>
          </div>
        </div>

        <div className="space-y-2">
          <p className="text-xs font-medium uppercase tracking-wider text-sand-500">
            Manage business
          </p>
          <div className="flex flex-wrap items-center gap-2">
            <h1 className="text-2xl font-semibold tracking-tight text-sand-950 sm:text-[1.75rem]">
              {business.name}
            </h1>
            {verified ? (
              <Badge tone="success" icon={<BadgeCheck className="size-3.5" aria-hidden />}>
                Verified
              </Badge>
            ) : (
              <Badge tone="neutral">Not verified by Mado</Badge>
            )}
          </div>
          <p className="text-sm text-sand-500">
            {business.businessTypeLabel ?? 'Business'}
            <span className="text-sand-400"> · </span>
            <Link
              to="/posts"
              className="text-sand-600 underline-offset-2 transition-colors hover:text-brand-500 hover:underline"
            >
              Your posts
            </Link>
          </p>
        </div>
      </div>
    </div>
  )
}

export function BusinessDashboardPage() {
  const { businessId = '' } = useParams()

  const { data: business, isLoading, isError } = useQuery({
    queryKey: ['business', businessId],
    queryFn: () => api.business(businessId),
    enabled: Boolean(businessId),
  })

  // What this caller may do. Drives which tabs render; never the gate.
  const { data: held } = useQuery({
    queryKey: ['business-permissions', businessId],
    queryFn: () => api.businessPermissions(businessId),
    enabled: Boolean(businessId),
  })

  if (isLoading) {
    return (
      <div className="mx-auto max-w-3xl space-y-5 px-4 py-6 sm:px-6">
        <Skeleton className="h-52 w-full rounded-2xl" />
        <Skeleton className="h-12 w-full rounded-xl" />
        <Skeleton className="h-64 w-full rounded-xl" />
      </div>
    )
  }

  if (isError || !business) {
    return (
      <div className="mx-auto max-w-lg px-4 py-16">
        <EmptyState
          icon={<Building2 className="size-8" />}
          title="Not found"
          description="This business does not exist, or you do not have access to it."
          action={
            <Link to="/account-type">
              <Button variant="secondary">Your account</Button>
            </Link>
          }
        />
      </div>
    )
  }

  const can = (permission: string) => (held ?? []).includes(permission)
  // Permissions arrive after the business. Until then every `can` is false, and
  // treating that as "read-only" flashes the wrong card for a frame.
  const permissionsReady = held !== undefined

  // Built in order, not looked up: the first entry the caller may open is the
  // default tab, so an editor lands on Profile and a bookkeeper on Plan.
  const tabs: { id: ManageTab; label: string; icon: ReactNode }[] = [
    ...(can('profile:edit')
      ? [
          {
            id: 'profile' as const,
            label: 'Profile',
            icon: <Building2 className="size-3.5" aria-hidden />,
          },
          // Same permission as Profile, and deliberately: the gallery *is* the
          // profile. `permissions.py` holds that a scope nobody needs is worse
          // than no scope, so there is no separate `gallery:manage`. Split into
          // its own tab anyway — one long form plus a media grid was the scroll
          // this layout exists to end.
          {
            id: 'gallery' as const,
            label: 'Gallery',
            icon: <Images className="size-3.5" aria-hidden />,
          },
        ]
      : []),
    // Its own permission, not `analytics:view`: an analyst is shown how the
    // posts are doing, which is a different decision from being shown what
    // the business took. Plan and earnings stay separate tabs so pricing and
    // payouts are not fighting for the same viewport — they share the gate,
    // not the screen.
    ...(can('finance:view')
      ? [
          {
            id: 'plan' as const,
            label: 'Plan',
            icon: <CreditCard className="size-3.5" aria-hidden />,
          },
          {
            id: 'earnings' as const,
            label: 'Earnings',
            icon: <Wallet className="size-3.5" aria-hidden />,
          },
        ]
      : []),
    ...(can('team:manage')
      ? [
          {
            id: 'team' as const,
            label: 'Team',
            icon: <Users className="size-3.5" aria-hidden />,
          },
        ]
      : []),
  ]

  return (
    <div className="mx-auto w-full max-w-3xl space-y-6 px-4 pb-24 pt-6 sm:px-6">
      <ManageHeader business={business} />

      {!permissionsReady ? (
        <Skeleton className="h-64 w-full rounded-xl" />
      ) : tabs.length > 0 ? (
        <ManageTabs tabs={tabs}>
          {(tab) => {
            switch (tab) {
              case 'profile':
                return <ProfileSection business={business} />
              case 'gallery':
                return (
                  <BusinessGallerySection businessId={business.id} slug={business.slug} />
                )
              case 'plan':
                return <BusinessPlanSection businessId={business.id} />
              case 'earnings':
                return <EarningsSection businessId={business.id} />
              case 'team':
                return <TeamSection businessId={business.id} />
            }
          }}
        </ManageTabs>
      ) : (
        <Card className="p-5 text-sm text-sand-600 sm:p-6">
          Your role here is read-only. You can see this business and its numbers, but
          not change it.
        </Card>
      )}
    </div>
  )
}
