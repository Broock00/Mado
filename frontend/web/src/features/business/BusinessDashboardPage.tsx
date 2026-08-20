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
import { Building2, Mail, Trash2, UserPlus } from 'lucide-react'

import { ApiError, api } from '@/lib/api'
import type { Business, BusinessMember } from '@/lib/types'
import { BUSINESS_TYPES } from '@/lib/business'
import { Badge, Button, Card, EmptyState, Input, Skeleton } from '@/design-system/primitives'

function ProfileSection({ business }: { business: Business }) {
  const queryClient = useQueryClient()
  const [form, setForm] = useState({
    name: business.name,
    businessType: business.businessType ?? '',
    description: business.description ?? '',
    website: business.website ?? '',
  })
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const save = useMutation({
    mutationFn: () =>
      api.updateBusiness(business.id, {
        name: form.name.trim(),
        businessType: form.businessType || null,
        description: form.description.trim() || null,
        website: form.website.trim() || null,
      }),
    onSuccess: () => {
      setError(null)
      setSaved(true)
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
            className="w-full rounded-lg border border-sand-300 bg-white px-3 py-2 text-sm text-sand-900"
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
            className="w-full rounded-lg border border-sand-300 bg-white px-3 py-2 text-sm text-sand-900"
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

      {error && <p className="text-sm text-red-700">{error}</p>}

      <div className="flex items-center gap-3">
        <Button onClick={() => save.mutate()} loading={save.isPending}>
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
        {error && <p className="mt-1 text-xs text-red-700">{error}</p>}
      </div>

      {member.status === 'invited' && <Badge tone="neutral">Pending</Badge>}

      <select
        aria-label={`Role for ${member.displayName ?? member.invitedEmail ?? 'this member'}`}
        value={member.role}
        onChange={(e) => changeRole.mutate(e.target.value)}
        disabled={changeRole.isPending || remove.isPending}
        className="rounded-lg border border-sand-300 bg-white px-2 py-1.5 text-sm text-sand-900"
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
            className="rounded-lg border border-sand-300 bg-white px-3 py-2 text-sm text-sand-900"
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

      {error && <p className="text-sm text-red-700">{error}</p>}

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
