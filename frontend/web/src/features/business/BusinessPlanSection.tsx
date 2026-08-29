/**
 * What the business is paying for, and what else it could.
 *
 * A section on the dashboard rather than a page of its own, and beside Earnings
 * rather than beside the profile: both are money, both take `finance:view`, and
 * somebody deciding whether a plan is worth it is usually looking at what the
 * business took in the same breath.
 *
 * **The prices come from the server.** `lib/plans.ts` carries what each plan
 * includes so the comparison renders immediately, but a price list changes
 * without a deploy and a stale number in the bundle is worse than a spinner.
 *
 * **Nothing here claims a plan is active until the server says so.** Starting a
 * purchase hands back a checkout URL and the section keeps showing the old
 * plan, because until the provider confirms, the old plan is what is in force.
 */

import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, CreditCard } from 'lucide-react'

import { ApiError, api } from '@/lib/api'
import type { BusinessPlan } from '@/lib/types'
import { money } from '@/lib/money'
import { paymentMethod } from '@/lib/payment-providers'
import { Badge, Button, Card, Skeleton } from '@/design-system/primitives'
import { buttonClasses } from '@/design-system/button-styles'

function includes(plan: BusinessPlan): string[] {
  const e = plan.entitlements
  return [
    e.maxLiveListings === null
      ? 'Unlimited posts live at once'
      : `${e.maxLiveListings} posts live at once`,
    e.maxTeamMembers === null
      ? 'Unlimited team members'
      : `${e.maxTeamMembers} team ${e.maxTeamMembers === 1 ? 'member' : 'members'}`,
    `${e.analyticsWindowDays} days of analytics`,
    ...(e.aiAssistant ? ['Writing assistant'] : []),
    ...(e.apiAccess ? ['API access'] : []),
  ]
}

export function BusinessPlanSection({ businessId }: { businessId: string }) {
  const queryClient = useQueryClient()
  const [error, setError] = useState<string | null>(null)
  // Null means "whatever the business's own city uses", which is what the
  // server answers with. Only set once somebody deliberately changes it.
  const [currency, setCurrency] = useState<string | null>(null)

  const { data, isLoading } = useQuery({
    queryKey: ['business-plans', businessId, currency],
    queryFn: () => api.businessPlans(businessId, currency ?? undefined),
    enabled: Boolean(businessId),
    // While a purchase is outstanding the server asks the provider on every
    // read, so polling is what turns "come back later" into an answer on
    // screen. It stops the moment the plan settles.
    //
    // This is the difference between a payment that works and one that appears
    // to vanish: a webhook cannot reach a development machine, and in
    // production it can be minutes late, so somebody returning from the
    // provider would otherwise sit looking at the plan they had just paid to
    // leave.
    refetchInterval: (query) =>
      query.state.data?.current.pendingPlan ? 3000 : false,
  })

  const buy = useMutation({
    // The currency that was quoted, so nobody is charged in money they were
    // not shown a price in.
    mutationFn: (plan: string) => api.startSubscription(businessId, plan, data?.currency),
    onSuccess: (subscription) => {
      setError(null)
      // Straight to the provider. The section is not updated to show the new
      // plan first — until the money arrives, the old one is what is in force,
      // and showing otherwise would be the interface inventing a result.
      if (subscription.checkoutUrl) window.location.href = subscription.checkoutUrl
    },
    onError: (err: unknown) => {
      setError(err instanceof ApiError ? err.message : 'That did not work. Try again.')
    },
  })

  const cancel = useMutation({
    mutationFn: () => api.cancelSubscription(businessId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['business-plans', businessId] })
    },
  })

  if (isLoading) return <Skeleton className="h-64 w-full rounded-xl" />
  if (!data) return null

  const current = data.current
  const method = paymentMethod(data.provider)
  const endsOn = current.currentPeriodEnd
    ? new Date(current.currentPeriodEnd).toLocaleDateString()
    : null

  return (
    <Card className="space-y-4 p-5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <CreditCard className="size-5 text-sand-500" aria-hidden />
          <h2 className="text-lg font-semibold text-sand-900">Plan</h2>
        </div>
        <div className="flex items-center gap-2">
          {/* Only when there is a choice to make. A select with one option is a
              control that does nothing. */}
          {data.soldIn.length > 1 && (
            <label className="text-sm text-sand-500">
              <span className="sr-only">Currency</span>
              <select
                value={data.currency}
                onChange={(event) => setCurrency(event.target.value)}
                className="rounded-lg border border-sand-300 bg-sand-50 px-2 py-1 text-sm text-sand-900"
              >
                {data.soldIn.map((code) => (
                  <option key={code} value={code}>
                    {code}
                  </option>
                ))}
              </select>
            </label>
          )}
          <Badge tone={current.plan === 'free' ? 'neutral' : 'brand'}>{current.planName}</Badge>
        </div>
      </div>

      {endsOn && current.plan !== 'free' && (
        <p className="text-sm text-sand-600">
          {current.status === 'cancelled'
            ? `Cancelled — you keep ${current.planName} until ${endsOn}.`
            : `Runs until ${endsOn}. Plans are paid a month at a time and do not renew
               on their own — we will remind you.`}
        </p>
      )}

      {/* Said explicitly, because the plan above deliberately does not change
          until the money arrives — without this the button would look as though
          it had done nothing.

          "Checking" rather than "waiting": the page is actively asking the
          provider every few seconds, and this is the screen somebody stares at
          straight after paying. The one thing it must not do is guess — a
          return link is something a browser followed, not evidence the money
          moved. */}
      {current.pendingPlan && (
        <div className="rounded-xl bg-sand-50 p-4 text-sm text-sand-700">
          <p>
            Checking your payment for{' '}
            {data.plans.find((p) => p.key === current.pendingPlan)?.name ??
              current.pendingPlan}
            . You keep {current.planName} until it goes through.
          </p>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            {current.checkoutUrl && (
              <a className={buttonClasses('secondary', 'sm')} href={current.checkoutUrl}>
                Finish paying
              </a>
            )}
            <span className="text-sand-500">
              This updates on its own — you can leave the page.
            </span>
          </div>
        </div>
      )}

      {/* What paying will actually involve, which the currency decides. */}
      {method && (
        <p className="text-sm text-sand-500">
          Paid with {method.label} — {method.hint}.
        </p>
      )}

      {error && <p className="text-sm text-rust-600">{error}</p>}

      <div className="grid gap-3 sm:grid-cols-2">
        {data.plans.map((plan) => {
          const isCurrent = plan.key === current.plan
          return (
            <div
              key={plan.key}
              className={`rounded-xl border p-4 ${
                isCurrent ? 'border-brand-500 bg-brand-50/40' : 'border-sand-200'
              }`}
            >
              <div className="flex items-baseline justify-between gap-2">
                <h3 className="font-medium text-sand-900">{plan.name}</h3>
                {plan.priceMinor != null && plan.currency ? (
                  <span className="text-sm text-sand-700">
                    {money(plan.priceMinor, plan.currency)}
                    <span className="text-sand-500">/month</span>
                  </span>
                ) : (
                  <span className="text-sm text-sand-500">
                    {plan.key === 'free' ? 'Free' : 'Talk to us'}
                  </span>
                )}
              </div>
              <p className="mt-1 text-sm text-sand-600">{plan.tagline}</p>

              <ul className="mt-3 space-y-1 text-sm text-sand-700">
                {includes(plan).map((line) => (
                  <li key={line} className="flex items-start gap-2">
                    <Check className="mt-0.5 size-4 shrink-0 text-sand-400" aria-hidden />
                    {line}
                  </li>
                ))}
              </ul>

              <div className="mt-4">
                {isCurrent ? (
                  <span className="text-sm font-medium text-brand-700">Your plan</span>
                ) : plan.purchasable ? (
                  <Button
                    size="sm"
                    onClick={() => buy.mutate(plan.key)}
                    disabled={buy.isPending}
                  >
                    {buy.isPending ? 'Opening checkout…' : `Move to ${plan.name}`}
                  </Button>
                ) : plan.key !== 'free' ? (
                  // No price and no button. An enterprise agreement is
                  // negotiated, and a "buy" that cannot complete is worse than
                  // no button at all.
                  <a
                    className="text-sm font-medium text-brand-700 underline"
                    href="mailto:hello@mado.app?subject=Enterprise%20plan"
                  >
                    Get in touch
                  </a>
                ) : null}
              </div>
            </div>
          )
        })}
      </div>

      {current.plan !== 'free' && current.status !== 'cancelled' && (
        <Button
          variant="secondary"
          size="sm"
          onClick={() => cancel.mutate()}
          disabled={cancel.isPending}
        >
          Cancel plan
        </Button>
      )}
    </Card>
  )
}
