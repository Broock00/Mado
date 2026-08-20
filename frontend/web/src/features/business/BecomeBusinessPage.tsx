/**
 * Converting an existing individual account into a business.
 *
 * Reached only from settings — not from navigation, and not from a prompt. The
 * overwhelming majority of accounts will never be a business, and putting this
 * anywhere they pass through would be asking a question nobody had.
 */

import { Link, useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Building2 } from 'lucide-react'

import { api } from '@/lib/api'
import { useAppStore } from '@/app/store'
import { BecomeBusinessForm } from '@/features/business/BecomeBusinessForm'
import { Button, EmptyState, Skeleton } from '@/design-system/primitives'

export function BecomeBusinessPage() {
  const user = useAppStore((s) => s.user)
  const navigate = useNavigate()

  const { data: state, isLoading } = useQuery({
    queryKey: ['account-type'],
    queryFn: () => api.accountType(),
    enabled: Boolean(user),
  })

  if (!user) {
    return (
      <div className="mx-auto max-w-lg px-4 py-16">
        <EmptyState
          icon={<Building2 className="size-8" />}
          title="Sign in first"
          description="A business is what an account becomes, so you need one to start."
          action={
            <Link to="/signin">
              <Button>Sign in</Button>
            </Link>
          }
        />
      </div>
    )
  }

  if (isLoading || !state) {
    return (
      <div className="mx-auto max-w-2xl px-4 py-6">
        <Skeleton className="h-64 w-full rounded-xl" />
      </div>
    )
  }

  // Already one. Nothing to convert, and offering the form again would be
  // offering something the server will refuse.
  if (state.accountType === 'business' && state.business) {
    return (
      <div className="mx-auto max-w-lg px-4 py-16">
        <EmptyState
          icon={<Building2 className="size-8" />}
          title="Already a business"
          description={`This account is ${state.business.name}.`}
          action={
            <Link to={`/businesses/${state.business.id}/manage`}>
              <Button variant="secondary">Manage it</Button>
            </Link>
          }
        />
      </div>
    )
  }

  return (
    <div className="mx-auto w-full max-w-2xl space-y-5 px-4 pb-24 pt-6 sm:px-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight text-sand-900">
          Change to a business account
        </h1>
        <p className="mt-1 text-sm text-sand-500">
          Your profile becomes the business. Anything you posted as yourself stays yours.
        </p>
      </div>

      <BecomeBusinessForm onDone={(slug) => navigate(`/businesses/${slug}`)} />

      <Link to="/settings">
        <Button variant="ghost" size="sm">
          Not now
        </Button>
      </Link>
    </div>
  )
}
