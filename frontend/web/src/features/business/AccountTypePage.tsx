/**
 * The step between registering and reaching the app: what kind of account is
 * this?
 *
 * An account is a person **or** a business, never both — so this is not "add a
 * business", it is what the account becomes.
 *
 * **Asked once, here.** This is the only moment somebody expects to be set up,
 * and the only moment the answer is genuinely needed. An account that has
 * already answered is sent straight on rather than being asked again; the way
 * to become a business afterwards is in settings, which is where somebody goes
 * when they have decided, not somewhere they trip over.
 *
 * Two things are said on screen rather than buried: choosing business is **one
 * way**, and there is no second login. People are reasonably cautious about a
 * button that changes what their account is, and the honest answer to that is
 * to say what it does before they press it.
 */

import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Building2, User as UserIcon } from 'lucide-react'

import { api } from '@/lib/api'
import { useAppStore } from '@/app/store'
import { BecomeBusinessForm } from '@/features/business/BecomeBusinessForm'
import { Button, Card, EmptyState, Skeleton } from '@/design-system/primitives'

export function AccountTypePage() {
  const user = useAppStore((s) => s.user)
  const navigate = useNavigate()
  const [choosing, setChoosing] = useState(false)
  const queryClient = useQueryClient()

  const { data: state, isLoading } = useQuery({
    queryKey: ['account-type'],
    queryFn: () => api.accountType(),
    enabled: Boolean(user),
  })

  // Somebody who has already answered has no business on this screen. That
  // happens when a link is shared, or the browser goes back after signing up -
  // and re-asking is how a one-time setup step turns into a nag.
  useEffect(() => {
    if (state?.chosen) navigate('/', { replace: true })
  }, [state?.chosen, navigate])

  const stayIndividual = useMutation({
    mutationFn: () => api.chooseIndividual(),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['account-type'] })
      void queryClient.invalidateQueries({ queryKey: ['session'] })
      navigate('/', { replace: true })
    },
  })

  if (!user) {
    return (
      <div className="mx-auto max-w-3xl px-4 py-16">
        <EmptyState
          icon={<Building2 className="size-8" />}
          title="Sign in first"
          description="An account is a person or a business. You need one before choosing which."
          action={
            <Link to="/signin">
              <Button>Sign in</Button>
            </Link>
          }
        />
      </div>
    )
  }

  // Also covers the moment between the redirect firing and the route changing.
  if (isLoading || !state || state.chosen) {
    return (
      <div className="mx-auto max-w-2xl space-y-4 px-4 py-6">
        <Skeleton className="h-24 w-full rounded-xl" />
        <Skeleton className="h-40 w-full rounded-xl" />
      </div>
    )
  }

  return (
    <div className="mx-auto w-full max-w-2xl space-y-5 px-4 pb-24 pt-6 sm:px-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight text-sand-900">
          Welcome to Mado
        </h1>
        <p className="mt-1 text-sm text-sand-500">
          One question before you start. Individual is the ordinary choice — you
          can become a business later from settings.
        </p>
      </div>

      {!choosing ? (
        <div className="grid gap-3 sm:grid-cols-2">
          <Card className="flex flex-col gap-3 p-5">
            <UserIcon className="size-6 text-sand-500" aria-hidden />
            <div className="flex-1">
              <h2 className="font-medium text-sand-900">An individual</h2>
              <p className="mt-1 text-sm text-sand-500">
                You explore the city and post the odd thing under your own name.
              </p>
            </div>
            <Button
              variant="secondary"
              onClick={() => stayIndividual.mutate()}
              loading={stayIndividual.isPending}
            >
              I&rsquo;m an individual
            </Button>
          </Card>

          <Card className="flex flex-col gap-3 p-5">
            <Building2 className="size-6 text-sand-500" aria-hidden />
            <div className="flex-1">
              <h2 className="font-medium text-sand-900">A business</h2>
              <p className="mt-1 text-sm text-sand-500">
                A hotel, cafe, venue or anywhere people visit. Your profile becomes the
                business, and you can invite staff to help run it.
              </p>
            </div>
            <Button onClick={() => setChoosing(true)}>This is a business</Button>
          </Card>
        </div>
      ) : (
        <>
          <BecomeBusinessForm onDone={(slug) => navigate(`/businesses/${slug}`)} />
          <Button variant="ghost" size="sm" onClick={() => setChoosing(false)}>
            Not now
          </Button>
        </>
      )}
    </div>
  )
}
