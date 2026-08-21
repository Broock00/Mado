/**
 * The tickets a listing sells (spec COM-002).
 *
 * Controlled, and deliberately so. Tiers live on a date in the database and a
 * date lives on a saved listing, so an editor that talked to the server
 * directly could only work in that order - price, then save, then date, then
 * ticket, four visits to the server before anyone could buy anything. Somebody
 * writing a post does not think in that order and should not have to.
 *
 * So this edits a list. The composer holds it, shows it beside the price it
 * belongs with, and writes the whole thing down in dependency order when the
 * post is saved or published. Nothing here knows whether the listing exists yet.
 *
 * **Money is typed in the listing's currency and held in minor units.** The
 * conversion reads the digits either side of the decimal point rather than
 * multiplying by a hundred, because 19.99 * 100 is 1998.9999999999998 and
 * rounding that happens to work while other values do not.
 */

import { useState } from 'react'
import { Info, Pencil, Plus, Ticket, X } from 'lucide-react'

import { money, toMajorInput, toMinor } from '@/lib/money'
import { Badge, Button, Input } from '@/design-system/primitives'

export interface DraftTicket {
  name: string
  description: string
  priceMinor: number
  quantity: number | null
  /** How many dates already sell it, when it came from a saved listing. */
  dates?: number
  sold?: number
  /** True when the saved dates disagree - somebody edited one by hand. */
  varies?: boolean
}

const BLANK = { name: '', description: '', price: '', quantity: '' }

export function TicketPlanEditor({
  tickets,
  onChange,
  currency,
  dateCount,
}: {
  tickets: DraftTicket[]
  onChange: (next: DraftTicket[]) => void
  currency: string
  /** Only to say what will happen, never to prevent it. */
  dateCount: number
}) {
  const [open, setOpen] = useState(false)
  const [draft, setDraft] = useState(BLANK)
  const [editing, setEditing] = useState<string | null>(null)

  const commit = () => {
    const name = draft.name.trim()
    if (!name) return
    const entry: DraftTicket = {
      name,
      description: draft.description.trim(),
      priceMinor: toMinor(draft.price || '0', currency),
      quantity: draft.quantity.trim() === '' ? null : Number(draft.quantity),
    }
    // Keyed by name, like the server is. Editing one replaces it in place so a
    // corrected price does not become a second ticket.
    const without = tickets.filter((t) => t.name !== (editing ?? name))
    const previous = tickets.find((t) => t.name === (editing ?? name))
    onChange([...without, { ...previous, ...entry }])
    setDraft(BLANK)
    setEditing(null)
    setOpen(false)
  }

  const edit = (ticket: DraftTicket) => {
    setDraft({
      name: ticket.name,
      description: ticket.description ?? '',
      price: ticket.priceMinor === 0 ? '' : toMajorInput(ticket.priceMinor, currency),
      quantity: ticket.quantity == null ? '' : String(ticket.quantity),
    })
    setEditing(ticket.name)
    setOpen(true)
  }

  return (
    <div className="rounded-xl border border-sand-200 bg-sand-50/60 p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="flex items-center gap-1.5 text-sm font-medium text-sand-800">
          <Ticket className="size-4 text-sand-500" aria-hidden />
          Tickets
        </p>
        {!open && (
          <Button variant="ghost" size="sm" onClick={() => setOpen(true)}>
            <Plus className="size-4" aria-hidden />
            Add
          </Button>
        )}
      </div>

      {tickets.length === 0 ? (
        <p className="mt-1.5 text-sm text-sand-600">
          None, so this is free to attend. Add one for general admission, and more
          for VIP or anything else you offer.
        </p>
      ) : (
        <ul className="mt-2 space-y-1.5">
          {tickets.map((ticket) => (
            <li
              key={ticket.name}
              className="flex flex-wrap items-start justify-between gap-2 rounded-lg bg-sand-100 px-3 py-2"
            >
              <span className="min-w-0">
                <span className="text-sm font-medium text-sand-900">{ticket.name}</span>{' '}
                <span className="text-sm text-sand-600">
                  {ticket.priceMinor === 0 ? 'Free' : money(ticket.priceMinor, currency)}
                  {ticket.quantity != null && ` · ${ticket.quantity} per date`}
                </span>
                {ticket.description && (
                  <span className="block text-xs text-sand-500">{ticket.description}</span>
                )}
                {ticket.sold ? (
                  <span className="block text-xs text-sand-400">{ticket.sold} sold</span>
                ) : null}
              </span>
              <span className="flex shrink-0 items-center gap-0.5">
                {ticket.varies && <Badge tone="neutral">Varies</Badge>}
                <Button
                  variant="ghost"
                  size="sm"
                  aria-label={`Edit ${ticket.name}`}
                  onClick={() => edit(ticket)}
                >
                  <Pencil className="size-4" aria-hidden />
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  aria-label={`Remove ${ticket.name}`}
                  onClick={() => onChange(tickets.filter((t) => t.name !== ticket.name))}
                >
                  <X className="size-4" aria-hidden />
                </Button>
              </span>
            </li>
          ))}
        </ul>
      )}

      {/* Said as what will happen, not as a blocker. A ticket needs a date to be
          sold for, and the composer writes both down together - so this is a
          note about order of operations, not a door. */}
      {tickets.length > 0 && dateCount === 0 && (
        <p className="mt-2 flex items-start gap-1.5 text-xs text-sand-500">
          <Info className="mt-0.5 size-3.5 shrink-0" aria-hidden />
          Add a date and these go on sale for it.
        </p>
      )}
      {tickets.length > 0 && dateCount > 0 && (
        <p className="mt-2 text-xs text-sand-500">
          Sold on {dateCount === 1 ? 'the date' : `all ${dateCount} dates`} above, and on
          any you add later.
        </p>
      )}

      {open && (
        <div className="mt-3 space-y-2 border-t border-sand-200 pt-3">
          <Input
            value={draft.name}
            onChange={(e) => setDraft({ ...draft, name: e.target.value })}
            placeholder="General admission, VIP, VVIP…"
            aria-label="Ticket name"
            maxLength={80}
            autoFocus
          />
          <Input
            value={draft.description}
            onChange={(e) => setDraft({ ...draft, description: e.target.value })}
            placeholder="What it includes (optional)"
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
              placeholder="How many"
              aria-label="How many of this ticket exist on each date"
            />
          </div>
          <p className="text-xs text-sand-500">
            Empty price means free but still booked. Empty number means the only
            limit is how many the place holds.
          </p>
          <div className="flex gap-2">
            <Button size="sm" onClick={commit} disabled={!draft.name.trim()}>
              {editing ? 'Update' : 'Add this ticket'}
            </Button>
            <Button
              size="sm"
              variant="ghost"
              onClick={() => {
                setDraft(BLANK)
                setEditing(null)
                setOpen(false)
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
