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
 *
 * **What somebody already holds is shown before what they can buy.** Once
 * tickets exist for this date the panel leads with them, linked to the codes,
 * and the button underneath says "Buy more" - a page that offers "Get tickets"
 * to somebody holding three reads as though the first purchase had not landed,
 * and leaves them hunting the navigation for where their ticket went.
 */

import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery } from '@tanstack/react-query'
import { ExternalLink, Minus, Plus, Ticket } from 'lucide-react'

import { api, ApiError } from '@/lib/api'
import { useAppStore } from '@/app/store'
import type { EventInstance, TicketTypeSummary } from '@/lib/types'
import { Badge, Button, Card } from '@/design-system/primitives'
import { ReserveButton } from '@/features/reservations/ReserveButton'
import { heldFor, type Held } from './held'
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
    <div className="flex flex-wrap items-center justify-between gap-3 py-3.5">
      <div className="min-w-0">
        <p className="font-medium text-sand-900">{tier.name}</p>
        {tier.description && <p className="mt-0.5 text-sm text-sand-600">{tier.description}</p>}
        <p className="mt-1 text-sm font-medium text-sand-800">
          {tier.priceMinor === 0 ? 'Free' : money(tier.priceMinor, tier.currency)}
          {tier.remaining != null && tier.remaining <= 5 && tier.onSale && (
            <span className="ml-2 font-normal text-danger">{tier.remaining} left</span>
          )}
        </p>
      </div>

      {tier.onSale ? (
        <div className="flex items-center gap-1 rounded-xl border border-sand-200 bg-sand-50 p-1">
          <Button
            variant="ghost"
            size="sm"
            aria-label={`One fewer ${tier.name}`}
            disabled={quantity === 0}
            onClick={() => onChange(quantity - 1)}
            className="size-9 !px-0"
          >
            <Minus className="size-4" />
          </Button>
          <span className="w-7 text-center text-sm font-medium tabular-nums">{quantity}</span>
          <Button
            variant="ghost"
            size="sm"
            aria-label={`One more ${tier.name}`}
            disabled={quantity >= ceiling}
            onClick={() => onChange(quantity + 1)}
            className="size-9 !px-0"
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

/**
 * Tickets already held for this date, above the button that sells more.
 *
 * Shown wherever the panel appears, including when the date has since sold out
 * - somebody who bought a ticket still needs to reach it, and "Sold out" with
 * nothing else on the card reads as though theirs went with it.
 *
 * Never rendered for an external seller: Mado cannot see that server, and a
 * count there would be an invention (spec 55.05 §64).
 */
function HeldTickets({ held }: { held: Held }) {
  const plural = held.tickets === 1 ? '' : 's'
  return (
    <Link
      to={held.href}
      className="mt-3 flex items-center gap-2 rounded-xl bg-brand-900/30 px-3 py-2.5 text-sm font-medium text-brand-300 transition-colors hover:bg-brand-900/50"
    >
      <Ticket className="size-4 shrink-0" aria-hidden />
      {held.bought
        ? `${held.tickets} ticket${plural} bought`
        : `${held.tickets} place${plural} registered`}
    </Link>
  )
}

export function TicketPanel({
  experienceId,
  occurrence,
  /** Drop the outer card when the parent already frames the panel. */
  embedded = false,
}: {
  experienceId: string
  occurrence: EventInstance
  embedded?: boolean
}) {
  const user = useAppStore((s) => s.user)
  const [chosen, setChosen] = useState<Record<string, number>>({})
  const [error, setError] = useState<string | null>(null)

  const { data } = useQuery({
    queryKey: ['ticketing', occurrence.id],
    queryFn: () => api.ticketing(experienceId, occurrence.id),
  })

  // The same key the tickets page uses, so arriving here after buying reads
  // from the cache the receipt already filled rather than asking again.
  const { data: orders } = useQuery({
    queryKey: ['my-orders'],
    queryFn: () => api.myOrders(),
    enabled: Boolean(user),
  })

  const held = useMemo(() => heldFor(orders ?? [], occurrence.id), [orders, occurrence.id])

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
    const body = (
      <>
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
      </>
    )
    if (embedded) return <div>{body}</div>
    return <Card className="mt-3 p-4">{body}</Card>
  }

  if (data.cta === 'reserve') {
    // No tiers, but places to hold. COM-001 already does this well.
    return <ReserveButton occurrence={occurrence} />
  }

  if (data.cta === 'directions' || data.cta === 'find_similar' || data.cta === 'none') {
    // Nothing left to sell, which is not the same as nothing to show: the date
    // may have sold out or closed after this explorer bought, and their ticket
    // is still the thing they came back to the page for.
    if (!label && !held) return null
    return (
      <div className={embedded ? '' : 'mt-3'}>
        {label && (
          <div className="flex items-center gap-2">
            <Badge tone={AVAILABILITY_TONE[data.availability] ?? 'neutral'}>{label}</Badge>
            {price && <span className="text-sm text-sand-700">{price}</span>}
          </div>
        )}
        {held && <HeldTickets held={held} />}
      </div>
    )
  }

  const buying = data.cta === 'get_tickets'

  const body = (
    <>
      <div className="flex flex-wrap items-center justify-between gap-2">
        {label && <Badge tone={AVAILABILITY_TONE[data.availability] ?? 'neutral'}>{label}</Badge>}
        {price && <span className="text-sm font-medium text-sand-800">{price}</span>}
      </div>

      <div className="mt-1 divide-y divide-sand-200">
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

      {/* Above the button rather than below it, so what they already hold is
          read before the offer to buy again - and so the button's own wording
          has been explained by the time it says "more". */}
      {held && <HeldTickets held={held} />}

      {user ? (
        <Button
          className="mt-4 w-full"
          size="lg"
          disabled={total === 0 || buy.isPending}
          loading={buy.isPending}
          onClick={() => buy.mutate()}
        >
          <Ticket className="size-4" aria-hidden />
          {/* "Get tickets" under "3 tickets bought" reads as though the first
              purchase had not registered. */}
          {held ? (buying ? 'Buy more' : 'Register more') : buying ? 'Get tickets' : 'Register'}
          {total > 0 && ` · ${total}`}
        </Button>
      ) : (
        <Link className="mt-4 block" to="/signin">
          <Button className="w-full" size="lg" variant="secondary">
            Sign in to {buying ? 'buy tickets' : 'register'}
          </Button>
        </Link>
      )}
    </>
  )

  if (embedded) return <div>{body}</div>
  return <Card className="mt-3 p-4">{body}</Card>
}
