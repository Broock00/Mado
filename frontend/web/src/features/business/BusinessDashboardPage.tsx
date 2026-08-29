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
 */

import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Building2, Mail, Trash2, UserPlus, Wallet } from 'lucide-react'

import { ApiError, api } from '@/lib/api'
import type { Business, BusinessMember } from '@/lib/types'
import { BUSINESS_TYPES } from '@/lib/business'
import { money } from '@/lib/money'
import { Badge, Button, Card, EmptyState, Input, Skeleton } from '@/design-system/primitives'
import { BusinessGallerySection } from './BusinessGallerySection'
import { BusinessPlanSection } from './BusinessPlanSection'
import { SocialIcon } from './SocialIcon'
import { SOCIAL_PLATFORMS, normaliseSocialUrl } from './social'

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
    <Card className="space-y-4 p-5">
      <h2 className="text-sm font-medium text-sand-700">Profile</h2>

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
    <Card className="space-y-4 p-5">
      <div>
        <h2 className="text-sm font-medium text-sand-700">Team</h2>
        <p className="mt-1 text-xs text-sand-500">
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
    <Card className="space-y-4 p-5">
      <div className="flex items-center gap-2">
        <Wallet className="size-5 text-sand-500" aria-hidden />
        <h2 className="text-lg font-semibold text-sand-900">Earnings</h2>
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

export function BusinessDashboardPage() {
  const { businessId = '' } = useParams()

  const { data: business, isLoading, isError } = useQuery({
    queryKey: ['business', businessId],
    queryFn: () => api.business(businessId),
    enabled: Boolean(businessId),
  })

  // What this caller may do. Drives which sections render; never the gate.
  const { data: held } = useQuery({
    queryKey: ['business-permissions', businessId],
    queryFn: () => api.businessPermissions(businessId),
    enabled: Boolean(businessId),
  })

  if (isLoading) {
    return (
      <div className="mx-auto max-w-3xl space-y-4 px-4 py-6">
        <Skeleton className="h-24 w-full rounded-xl" />
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

  return (
    <div className="mx-auto w-full max-w-3xl space-y-5 px-4 pb-24 pt-6 sm:px-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-sand-900">
            {business.name}
          </h1>
          <p className="mt-1 text-sm text-sand-500">
            {business.businessTypeLabel ?? 'Business'}
            {business.verificationStatus !== 'verified' && ' · not verified by Mado'}
          </p>
        </div>
        <Link to={`/businesses/${business.slug}`}>
          <Button variant="secondary" size="sm">
            View public page
          </Button>
        </Link>
      </div>

      <Card className="flex flex-wrap items-center gap-2 p-4">
        {/* No `?publisher=` for the owner: their account *is* this business, so
            the composer resolves it without being told. The parameter still
            exists for a team member, whose own account is somebody else. */}
        <Link to="/compose">
          <Button size="sm">Write a post</Button>
        </Link>
        <Link to="/posts">
          <Button variant="secondary" size="sm">
            <Mail className="size-4" aria-hidden /> Your posts
          </Button>
        </Link>
      </Card>

      {can('profile:edit') && <ProfileSection business={business} />}
      {/* Same permission as the profile above, and deliberately: the gallery
          *is* the profile. `permissions.py` holds that a scope nobody needs is
          worse than no scope, so there is no separate `gallery:manage` — an
          editor writes posts, an administrator changes how the business
          presents itself. */}
      {can('profile:edit') && (
        <BusinessGallerySection businessId={business.id} slug={business.slug} />
      )}
      {/* Its own permission, not `analytics:view`: an analyst is shown how the
          posts are doing, which is a different decision from being shown what
          the business took. */}
      {can('finance:view') && <BusinessPlanSection businessId={business.id} />}
      {can('finance:view') && <EarningsSection businessId={business.id} />}
      {can('team:manage') && <TeamSection businessId={business.id} />}

      {!can('profile:edit') && !can('team:manage') && (
        <Card className="p-5 text-sm text-sand-600">
          Your role here is read-only. You can see this business and its numbers, but
          not change it.
        </Card>
      )}
    </div>
  )
}
