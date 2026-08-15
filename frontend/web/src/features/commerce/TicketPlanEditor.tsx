/**
 * The tickets a listing sells, written once (spec COM-002).
 *
 * Tiers live on a date in the database, because inventory does - last Friday
 * sold out and next Friday has not. Nobody authors them that way. The publisher
 * of a six-night run was being asked to type the same VIP ticket six times, and
 * any night they missed went on sale with nothing but general admission and
 * never said so.
 *
 * So this edits the plan: name it once, and it is applied to every date still to
 * come, including dates added afterwards.
 *
 * **Money is typed in the listing's currency and sent in minor units.** The
 * conversion reads the digits either side of the decimal point rather than
 * multiplying by a hundred, because 19.99 * 100 is 1998.9999999999998 and
 * rounding that happens to work while other values do not.
 *
 * **Withdrawing is not deleting.** A ticket somebody bought has to keep reading
 * on their receipt, so it stops being offered and stays in the record.
 */

import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Info, Pencil, Plus, Ticket, X } from 'lucide-react'

import { ApiError, api } from '@/lib/api'
import { money, toMajorInput, toMinor } from '@/lib/money'
import type { PlanEntry } from '@/lib/types'
import { Badge, Button, Input } from '@/design-system/primitives'

const BLANK = { name: '', description: '', price: '', quantity: '' }

export function TicketPlanEditor({
  experienceId,
  currency,
  dateCount,
}: {
  experienceId: string
  currency: string
  /** Nothing can be sold without one, and saying so beats a failed request. */
  dateCount: number
}) {
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(false)
  const [draft, setDraft] = useState(BLANK)
  const [error, setError] = useState<string | null>(null)

  const { data: plan } = useQuery({
    queryKey: ['ticket-plan', experienceId],
    queryFn: () => api.ticketPlan(experienceId),
  })

  const settle = () => {
    setDraft(BLANK)
    setOpen(false)
    setError(null)
    void queryClient.invalidateQueries({ queryKey: ['ticket-plan', experienceId] })
    // The explorer-facing panel reads per date, and a price the publisher just
    // set that does not appear on the listing looks broken.
    void queryClient.invalidateQueries({ queryKey: ['ticketing'] })
  }

  const sell = useMutation({
    mutationFn: () =>
      api.sellTicket(experienceId, {
        name: draft.name.trim(),
        description: draft.description.trim() || null,
        priceMinor: toMinor(draft.price || '0', currency),
        quantity: draft.quantity.trim() === '' ? null : Number(draft.quantity),
        currency,
      }),
    onSuccess: settle,
    onError: (caught) =>
      setError(caught instanceof ApiError ? caught.message : 'That did not work.'),
  })

  const stop = useMutation({
    mutationFn: (name: string) => api.stopSellingTicket(experienceId, name),
    onSuccess: settle,
    onError: (caught) =>
      setError(caught instanceof ApiError ? caught.message : 'That did not work.'),
  })

  const edit = (entry: PlanEntry) => {
    setDraft({
      name: entry.name,
      description: entry.description ?? '',
      // Back to what somebody types, from what the server stores. The inverse
      // of toMinor, so loading a tier and saving it unchanged cannot move the
      // price - and it respects zero-decimal currencies, which dividing by a
      // hundred would not.
      price: entry.priceMinor === 0 ? '' : toMajorInput(entry.priceMinor, entry.currency),
      quantity: entry.quantity == null ? '' : String(entry.quantity),
    })
    setOpen(true)
  }

  const entries = plan ?? []

  return (
    <div className="rounded-xl border border-sand-200 bg-sand-50/60 p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="flex items-center gap-1.5 text-sm font-medium text-sand-800">
          <Ticket className="size-4 text-sand-500" aria-hidden />
          Tickets
        </p>
        {!open && dateCount > 0 && (
          <Button variant="ghost" size="sm" onClick={() => setOpen(true)}>
            <Plus className="size-4" aria-hidden />
            Add a ticket
          </Button>
        )}
      </div>

      {dateCount === 0 ? (
        <p className="mt-2 flex items-start gap-1.5 text-sm text-sand-600">
          <Info className="mt-0.5 size-4 shrink-0 text-sand-400" aria-hidden />
          Add a date first. A ticket is sold for a date, so there is nowhere to put
          one yet.
        </p>
      ) : entries.length === 0 ? (
        <p className="mt-2 text-sm text-sand-600">
          None yet, so this is free to attend. Add one for general admission, and
          more for VIP or anything else you offer.
        </p>
      ) : (
        <>
          <ul className="mt-2 space-y-1.5">
            {entries.map((entry) => (
              <li
                key={entry.name}
                className="flex flex-wrap items-center justify-between gap-2 rounded-lg bg-white px-3 py-2"
              >
                <span className="min-w-0">
                  <span className="text-sm font-medium text-sand-900">{entry.name}</span>{' '}
                  <span className="text-sm text-sand-600">
                    {entry.priceMinor === 0 ? 'Free' : money(entry.priceMinor, entry.currency)}
                    {entry.quantity != null && ` · ${entry.quantity} per date`}
                  </span>
                  {entry.description && (
                    <span className="block text-xs text-sand-500">{entry.description}</span>
                  )}
                  <span className="block text-xs text-sand-400">
                    On {entry.dates} {entry.dates === 1 ? 'date' : 'dates'}
                    {entry.sold > 0 && ` · ${entry.sold} sold`}
                  </span>
                </span>
                <span className="flex shrink-0 items-center gap-1">
                  {/* Said out loud rather than papered over: showing one date's
                      price as though it were all of them would be a quiet lie. */}
                  {entry.varies && <Badge tone="neutral">Varies by date</Badge>}
                  <Button
                    variant="ghost"
                    size="sm"
                    aria-label={`Edit ${entry.name}`}
                    onClick={() => edit(entry)}
                  >
                    <Pencil className="size-4" aria-hidden />
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    aria-label={`Stop selling ${entry.name}`}
                    onClick={() => stop.mutate(entry.name)}
                  >
                    <X className="size-4" aria-hidden />
                  </Button>
                </span>
              </li>
            ))}
          </ul>
          {entries.length > 0 && dateCount > 1 && (
            <p className="mt-2 text-xs text-sand-500">
              Sold on all {dateCount} dates, and on any you add later.
            </p>
          )}
        </>
      )}

      {open && (
        <div className="mt-3 space-y-2 border-t border-sand-200 pt-3">
          <Input
            value={draft.name}
            onChange={(e) => setDraft({ ...draft, name: e.target.value })}
            placeholder="General admission, VIP, VVIP…"
            aria-label="Ticket name"
            maxLength={80}
          />
          <Input
            value={draft.description}
            onChange={(e) => setDraft({ ...draft, description: e.target.value })}
            placeholder="What this ticket includes (optional)"
            aria-label="What this ticket includes"
            maxLength={300}
          />
          <div className="flex gap-2">
            <Input
              value={draft.price}
              onChange={(e) => setDraft({ ...draft, price: e.target.value })}
              inputMode="decimal"
              placeholder={`Price in ${currency}`}
              aria-label={`Price in ${currency}`}
            />
            <Input
              value={draft.quantity}
              onChange={(e) => setDraft({ ...draft, quantity: e.target.value })}
              inputMode="numeric"
              placeholder="How many per date"
              aria-label="How many of this ticket exist on each date"
            />
          </div>
          <p className="text-xs text-sand-500">
            Leave the price empty for a free ticket people still have to book. Leave
            the number blank if the only limit is how many the place holds.
          </p>
          {error && <p className="text-sm text-danger">{error}</p>}
          <div className="flex gap-2">
            <Button
              size="sm"
              onClick={() => sell.mutate()}
              loading={sell.isPending}
              disabled={!draft.name.trim()}
            >
              Save this ticket
            </Button>
            <Button
              size="sm"
              variant="ghost"
              onClick={() => {
                setDraft(BLANK)
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
