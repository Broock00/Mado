/**
 * The account-kind row in settings.
 *
 * The only place "become a business" is offered after signup. Settings is where
 * somebody goes having decided; putting it in the navigation would mean showing
 * a business option to the overwhelming majority of accounts that will never be
 * one.
 *
 * A business account sees its business and the way in to managing it, not an
 * offer — there is nothing left to choose.
 */

import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Building2 } from 'lucide-react'

import { api } from '@/lib/api'
import { useAppStore } from '@/app/store'
import { BusinessInvitations } from '@/features/business/BusinessInvitations'
import { Button, Card, SectionHeading } from '@/design-system/primitives'

export function AccountKindSection() {
  const user = useAppStore((s) => s.user)

  const { data: state } = useQuery({
    queryKey: ['account-type'],
    queryFn: () => api.accountType(),
    enabled: Boolean(user),
    staleTime: 60 * 60_000,
  })

  if (!user || !state) return null

  const isBusiness = state.accountType === 'business' && state.business

  return (
    <section className="mt-10">
      {/* Being invited to help run somebody else's business is separate from
          what your own account is, but both answer "what am I on Mado" - so it
          lives here, and renders nothing when nothing is pending. */}
      <div className="mb-3">
        <BusinessInvitations />
      </div>

      <SectionHeading
        title="Account"
        subtitle={
          isBusiness
            ? 'This account is a business. Everything it publishes is published under that name.'
            : 'You are here as a person. That is what most accounts are.'
        }
      />
      <Card className="mt-3 p-5">
        {isBusiness && state.business ? (
          <div className="flex flex-wrap items-center gap-3">
            <div className="grid size-10 shrink-0 place-items-center rounded-lg bg-sand-100">
              {state.business.logoUrl ? (
                <img
                  src={state.business.logoUrl}
                  alt=""
                  className="size-10 rounded-lg object-cover"
                />
              ) : (
                <Building2 className="size-5 text-sand-500" aria-hidden />
              )}
            </div>
            <div className="min-w-0 flex-1">
              <p className="truncate font-medium text-sand-900">{state.business.name}</p>
              <p className="text-sm text-sand-500">
                {state.business.businessTypeLabel ?? 'Business'}
              </p>
            </div>
            <Link to={`/businesses/${state.business.id}/manage`}>
              <Button variant="secondary" size="sm">
                Manage
              </Button>
            </Link>
          </div>
        ) : (
          <div className="space-y-3">
            <p className="text-sm text-sand-600">
              If you run a hotel, cafe, venue or anywhere people visit, you can turn this
              into a business account. Your profile becomes the business and you can invite
              staff to help run it — the same sign-in, the same account.
            </p>
            <p className="text-xs text-sand-500">
              This cannot be undone, so it is worth being sure.
            </p>
            <Link to="/account-type/business">
              <Button variant="secondary" size="sm">
                <Building2 className="size-4" aria-hidden /> Change to a business
              </Button>
            </Link>
          </div>
        )}
      </Card>
    </section>
  )
}
