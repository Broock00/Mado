/**
 * Setting the prices on one date (spec COM-002).
 *
 * This is the piece that connects a listing to the checkout. Until it existed,
 * the whole payment path was reachable only by calling the API directly: the
 * composer let a publisher add dates but never prices, so no date ever had a
 * tier, so the explorer's ticket panel always fell back to the free "reserve"
 * button and no payment provider was ever contacted.
 *
 * **Money is typed in birr and sent in santim.** The conversion is integer
 * arithmetic on the digits either side of the decimal point, not
 * `Math.round(value * 100)` - 19.99 * 100 is 1998.9999999999998 in IEEE 754,
 * and rounding it happens to work while other values do not. Parsing the string
 * cannot drift.
 *
 * **Withdrawing is not deleting.** A tier somebody already bought has to keep
 * reading on their receipt, so it stops being offered and stays in the record.
 */

import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Plus, Ticket, X } from 'lucide-react'

import { ApiError, api } from '@/lib/api'
import { money, toMinor } from '@/lib/money'
import { Badge, Button, Input } from '@/design-system/primitives'

export function TicketTypesEditor({
  experienceId,
  occurrenceId,
  currency,
}: {
  experienceId: string
  occurrenceId: string
  currency: string
}) {
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(false)
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [price, setPrice] = useState('')
  const [quantity, setQuantity] = useState('')
  const [error, setError] = useState<string | null>(null)

  const { data: tiers } = useQuery({
    queryKey: ['ticket-types', occurrenceId],
    queryFn: () => api.ticketTypes(experienceId, occurrenceId),
  })

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['ticket-types', occurrenceId] })
    // The explorer-facing panel reads a different endpoint, and a price the
    // publisher just set that does not appear on the listing looks broken.
    void queryClient.invalidateQueries({ queryKey: ['ticketing', occurrenceId] })
  }

  const add = useMutation({
    mutationFn: () =>
      api.createTicketType(experienceId, occurrenceId, {
        name: name.trim(),
        // Sent as undefined rather than as an empty string: the column is
        // nullable and "" would render as a tier with a blank line under it.
        description: description.trim() || undefined,
        priceMinor: toMinor(price || '0', currency),
        quantity: quantity.trim() === '' ? null : Number(quantity),
      }),
    onSuccess: () => {
      setName('')
      setDescription('')
      setPrice('')
      setQuantity('')
      setOpen(false)
      setError(null)
      invalidate()
    },
    onError: (caught) =>
      setError(caught instanceof ApiError ? caught.message : 'That did not work.'),
  })

  const withdraw = useMutation({
    mutationFn: (id: string) => api.withdrawTicketType(experienceId, occurrenceId, id),
    onSuccess: invalidate,
  })

  const live = (tiers ?? []).filter((tier) => tier.unavailableReason !== 'withdrawn')

  return (
    <div className="mt-2 rounded-lg border border-sand-200 bg-sand-100 p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="flex items-center gap-1.5 text-sm font-medium text-sand-700">
          <Ticket className="size-4" aria-hidden />
          Tickets
          {live.length === 0 && (
            <span className="font-normal text-sand-500">
              — none, so this date is free to attend
            </span>
          )}
        </p>
        {!open && (
          <Button variant="ghost" size="sm" onClick={() => setOpen(true)}>
            <Plus className="size-4" aria-hidden />
            Add a ticket
          </Button>
        )}
      </div>

      {live.length > 0 && (
        <ul className="mt-2 space-y-1.5">
          {live.map((tier) => (
            <li
              key={tier.id}
              className="flex items-center justify-between gap-2 rounded-md bg-sand-100 px-3 py-2 text-sm"
            >
              <span className="min-w-0">
                <span className="font-medium text-sand-800">{tier.name}</span>{' '}
                <span className="text-sand-600">
                  {tier.priceMinor === 0 ? 'Free' : money(tier.priceMinor, tier.currency)}
                  {tier.quantity != null && ` · ${tier.remaining ?? 0} of ${tier.quantity} left`}
                </span>
                {tier.description && (
                  <span className="block text-xs text-sand-500">{tier.description}</span>
                )}
              </span>
              <span className="flex shrink-0 items-center gap-1">
                {!tier.onSale && <Badge tone="neutral">Not on sale</Badge>}
                <Button
                  variant="ghost"
                  size="sm"
                  aria-label={`Stop selling ${tier.name}`}
                  onClick={() => withdraw.mutate(tier.id)}
                >
                  <X className="size-4" aria-hidden />
                </Button>
              </span>
            </li>
          ))}
        </ul>
      )}

      {open && (
        <div className="mt-3 space-y-2">
          <Input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="General admission, VIP, VVIP…"
            aria-label="Ticket name"
          />
          <Input
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="What this ticket includes (optional)"
            aria-label="What this ticket includes"
            maxLength={300}
          />
          <div className="flex gap-2">
            <Input
              value={price}
              onChange={(e) => setPrice(e.target.value)}
              inputMode="decimal"
              placeholder={`Price in ${currency}`}
              aria-label={`Price in ${currency}`}
            />
            <Input
              value={quantity}
              onChange={(e) => setQuantity(e.target.value)}
              inputMode="numeric"
              placeholder="How many (optional)"
              aria-label="How many of this ticket exist"
            />
          </div>
          <p className="text-xs text-sand-500">
            Add as many tiers as you like - VIP, VVIP, early bird. Say what each one
            includes and people will see it next to the price. Leave the price at zero
            for a free ticket people still have to register for, and the number blank if
            the only limit is the capacity of the date.
          </p>
          {error && <p className="text-sm text-danger">{error}</p>}
          <div className="flex gap-2">
            <Button
              size="sm"
              onClick={() => add.mutate()}
              loading={add.isPending}
              disabled={!name.trim()}
            >
              Add
            </Button>
            <Button
              size="sm"
              variant="ghost"
              onClick={() => {
                setOpen(false)
                setError(null)
              }}
            >
              Cancel
            </Button>
          </div>
        </div>
      )}
    </div>
  )
}
