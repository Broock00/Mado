/**
 * The action area of a date: price, availability and one button (spec COM-002).
 *
 * The server decides the state and this decides the words. Everything arriving
 * from `/ticketing` is a code - `almost_sold_out`, `get_tickets` - so the
 * wording lives in one place per language instead of being composed on a
 * server that cannot know who is reading.
 *
 * Three things this is careful about:
 *
 * **Leaving Mado is announced before it happens** (spec 55.05 §23). Where the
 * organiser sells on their own site, the button says so and the panel says
 * where you are going. A link that silently leaves is how somebody ends up
 * typing card details into a page they did not choose to visit.
 *
 * **Nothing here claims a purchase happened elsewhere** (§64). For an external
 * link the panel shows no availability, no count and no "purchased" state,
 * because Mado cannot see that server and would be inventing all three.
 *
 * **A price is never rendered as fixed when only a floor is known** (§21).
 * `from` and `range` are separate shapes for exactly that reason.
 */

import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery } from '@tanstack/react-query'
import { ExternalLink, Minus, Plus, Ticket } from 'lucide-react'

import { api, ApiError } from '@/lib/api'
import { useAppStore } from '@/app/store'
import type { EventInstance, TicketTypeSummary } from '@/lib/types'
import { Badge, Button, Card } from '@/design-system/primitives'
import { ReserveButton } from '@/features/reservations/ReserveButton'
import { money } from '@/lib/money'

const AVAILABILITY_LABEL: Record<string, string> = {
  tickets_available: 'Tickets available',
  limited: 'Limited availability',
  almost_sold_out: 'Almost sold out',
  sold_out: 'Sold out',
  registration_open: 'Registration open',
  registration_closed: 'Registration closed',
  free_entry: 'Free entry',
  not_on_sale_yet: 'Not on sale yet',
  cancelled: 'Cancelled',
  ended: 'This has happened',
}

const AVAILABILITY_TONE: Record<string, 'success' | 'danger' | 'neutral'> = {
  tickets_available: 'success',
  registration_open: 'success',
  free_entry: 'success',
  limited: 'neutral',
  almost_sold_out: 'danger',
  sold_out: 'danger',
  registration_closed: 'danger',
  cancelled: 'danger',
}

const UNAVAILABLE_LABEL: Record<string, string> = {
  withdrawn: 'No longer offered',
  not_open_yet: 'Not on sale yet',
  closed: 'Sales closed',
  sold_out: 'Sold out',
}

function priceLine(
  priceType: string,
  min: number | null | undefined,
  max: number | null | undefined,
  currency: string,
): string | null {
  if (priceType === 'free') return 'Free'
  if (min == null) return null
  // "From 200 ETB" and "200 ETB" are different promises, and rendering the
  // first as the second is how somebody arrives with too little money.
  if (priceType === 'from') return `From ${money(min, currency)}`
  if (priceType === 'range' && max != null && max !== min) {
    return `${money(min, currency)} – ${money(max, currency)}`
  }
  return money(min, currency)
}

function TierRow({
  tier,
  quantity,
  onChange,
}: {
  tier: TicketTypeSummary
  quantity: number
  onChange: (next: number) => void
}) {
  // Never more than is actually there. A stepper that lets somebody choose
  // eight of a tier with two left is a refusal waiting to happen after they
  // have decided.
  const ceiling = Math.min(10, tier.remaining ?? 10)

  return (
    <div className="flex flex-wrap items-center justify-between gap-3 py-3">
      <div className="min-w-0">
        <p className="font-medium text-sand-900">{tier.name}</p>
        {tier.description && <p className="text-sm text-sand-600">{tier.description}</p>}
        <p className="mt-0.5 text-sm text-sand-700">
          {tier.priceMinor === 0 ? 'Free' : money(tier.priceMinor, tier.currency)}
          {tier.remaining != null && tier.remaining <= 5 && tier.onSale && (
            <span className="ml-2 text-danger">{tier.remaining} left</span>
          )}
        </p>
      </div>

      {tier.onSale ? (
        <div className="flex items-center gap-2">
          <Button
            variant="ghost"
            size="sm"
            aria-label={`One fewer ${tier.name}`}
            disabled={quantity === 0}
            onClick={() => onChange(quantity - 1)}
          >
            <Minus className="size-4" />
          </Button>
          <span className="w-6 text-center tabular-nums">{quantity}</span>
          <Button
            variant="ghost"
            size="sm"
            aria-label={`One more ${tier.name}`}
            disabled={quantity >= ceiling}
            onClick={() => onChange(quantity + 1)}
          >
            <Plus className="size-4" />
          </Button>
        </div>
      ) : (
        <Badge tone="neutral">
          {UNAVAILABLE_LABEL[tier.unavailableReason ?? ''] ?? 'Unavailable'}
        </Badge>
      )}
    </div>
  )
}

export function TicketPanel({
  experienceId,
  occurrence,
}: {
  experienceId: string
  occurrence: EventInstance
}) {
  const user = useAppStore((s) => s.user)
  const [chosen, setChosen] = useState<Record<string, number>>({})
  const [error, setError] = useState<string | null>(null)

  const { data } = useQuery({
    queryKey: ['ticketing', occurrence.id],
    queryFn: () => api.ticketing(experienceId, occurrence.id),
  })

  const buy = useMutation({
    mutationFn: () =>
      api.startOrder(
        experienceId,
        occurrence.id,
        Object.entries(chosen)
          .filter(([, quantity]) => quantity > 0)
          .map(([ticketTypeId, quantity]) => ({ ticketTypeId, quantity })),
      ),
    onSuccess: (order) => {
      setError(null)
      if (order.checkoutUrl) {
        // Away to the provider's own page. Mado never sees the card.
        window.location.assign(order.checkoutUrl)
        return
      }
      // A free order is already complete, so there is nothing to pay and
      // nowhere to go but the tickets.
      window.location.assign(`/orders/${order.id}`)
    },
    onError: (caught) =>
      setError(caught instanceof ApiError ? caught.message : 'That did not work.'),
  })

  if (!data) return null

  const total = Object.values(chosen).reduce((sum, n) => sum + n, 0)
  const label = AVAILABILITY_LABEL[data.availability]
  const price = priceLine(data.priceType, data.minPriceMinor, data.maxPriceMinor, data.currency)

  // The organiser sells elsewhere. No availability, no count, no claim about
  // what happens over there.
  if (data.cta === 'external_tickets' && data.externalUrl) {
    let host = ''
    try {
      host = new URL(data.externalUrl).host
    } catch {
      host = 'the organiser’s website'
    }
    return (
      <Card className="mt-3 p-4">
        {price && <p className="text-sm text-sand-700">{price}</p>}
        <a
          className="mt-2 inline-flex items-center gap-2 font-medium text-brand-700 hover:underline"
          href={data.externalUrl}
          target="_blank"
          rel="noreferrer noopener"
        >
          <ExternalLink className="size-4" aria-hidden />
          Get tickets
        </a>
        {/* Said before the tap, not after. */}
        <p className="mt-1 text-sm text-sand-600">
          You’ll continue to {host}. Mado doesn’t handle this booking, so your ticket
          won’t appear here.
        </p>
      </Card>
    )
  }

  if (data.cta === 'reserve') {
    // No tiers, but places to hold. COM-001 already does this well.
    return <ReserveButton occurrence={occurrence} />
  }

  if (data.cta === 'directions' || data.cta === 'find_similar' || data.cta === 'none') {
    if (!label) return null
    return (
      <div className="mt-3 flex items-center gap-2">
        <Badge tone={AVAILABILITY_TONE[data.availability] ?? 'neutral'}>{label}</Badge>
        {price && <span className="text-sm text-sand-700">{price}</span>}
      </div>
    )
  }

  const buying = data.cta === 'get_tickets'

  return (
    <Card className="mt-3 p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        {label && <Badge tone={AVAILABILITY_TONE[data.availability] ?? 'neutral'}>{label}</Badge>}
        {price && <span className="text-sm font-medium text-sand-800">{price}</span>}
      </div>

      <div className="mt-2 divide-y divide-sand-200">
        {data.ticketTypes.map((tier) => (
          <TierRow
            key={tier.id}
            tier={tier}
            quantity={chosen[tier.id] ?? 0}
            onChange={(next) => setChosen({ ...chosen, [tier.id]: Math.max(0, next) })}
          />
        ))}
      </div>

      {error && <p className="mt-2 text-sm text-danger">{error}</p>}

      {user ? (
        <Button
          className="mt-3 w-full"
          disabled={total === 0 || buy.isPending}
          loading={buy.isPending}
          onClick={() => buy.mutate()}
        >
          <Ticket className="size-4" aria-hidden />
          {buying ? 'Get tickets' : 'Register'}
          {total > 0 && ` · ${total}`}
        </Button>
      ) : (
        <Link className="mt-3 block" to="/signin">
          <Button className="w-full" variant="secondary">
            Sign in to {buying ? 'buy tickets' : 'register'}
          </Button>
        </Link>
      )}
    </Card>
  )
}
