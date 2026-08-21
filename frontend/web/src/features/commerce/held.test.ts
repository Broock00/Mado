/**
 * The rules behind "3 tickets bought".
 *
 * Every one of these failing renders as a perfectly ordinary count - the wrong
 * number, or the right number attached to the wrong evening - so none of them
 * is visible by looking at the page. That is the whole reason this is a pure
 * function with tests rather than a `useMemo` nobody can reach.
 */

import { describe, expect, it } from 'vitest'

import { heldFor } from './held'
import type { Order } from '@/lib/types'

const THIS_DATE = 'occurrence-1'
const ANOTHER_DATE = 'occurrence-2'

function order(patch: Partial<Order> & { id: string }): Order {
  return {
    reference: `REF-${patch.id}`,
    status: 'paid',
    experienceId: 'experience-1',
    experienceTitle: 'Arat Kilo Music Night',
    eventInstanceId: THIS_DATE,
    startsAt: '2026-08-21T18:22:00Z',
    amountMinor: 60000,
    currency: 'ETB',
    quantity: 0,
    lines: [],
    tickets: [],
    ...patch,
  }
}

function ticket(status = 'issued') {
  return { id: crypto.randomUUID(), code: 'ABC123', ticketTypeName: 'General', status }
}

describe('heldFor', () => {
  it('is null when nothing has been bought', () => {
    expect(heldFor([], THIS_DATE)).toBeNull()
  })

  it('counts the issued tickets of one order', () => {
    const held = heldFor([order({ id: 'a', tickets: [ticket(), ticket(), ticket()] })], THIS_DATE)
    expect(held?.tickets).toBe(3)
  })

  it('adds up two orders for the same date', () => {
    const held = heldFor(
      [
        order({ id: 'a', tickets: [ticket(), ticket()] }),
        order({ id: 'b', tickets: [ticket()] }),
      ],
      THIS_DATE,
    )
    expect(held?.tickets).toBe(3)
  })

  it('ignores tickets bought for another date', () => {
    // The card is about one evening and the button under the count sells for
    // that evening. Counting every date would offer "Buy more" beneath a number
    // belonging to a different one.
    const held = heldFor(
      [
        order({ id: 'a', tickets: [ticket()] }),
        order({ id: 'b', eventInstanceId: ANOTHER_DATE, tickets: [ticket(), ticket()] }),
      ],
      THIS_DATE,
    )
    expect(held?.tickets).toBe(1)
  })

  it('ignores an order that has not been paid for', () => {
    // A hold is not a purchase. Shown as one, somebody arrives at a door with
    // nothing that scans.
    const held = heldFor(
      [order({ id: 'a', status: 'pending', tickets: [ticket(), ticket()] })],
      THIS_DATE,
    )
    expect(held).toBeNull()
  })

  it('ignores a voided ticket while keeping the rest of its order', () => {
    // `order.quantity` would still say three. A void is a ticket the explorer
    // no longer holds.
    const held = heldFor(
      [order({ id: 'a', quantity: 3, tickets: [ticket(), ticket(), ticket('void')] })],
      THIS_DATE,
    )
    expect(held?.tickets).toBe(2)
  })

  it('counts a ticket already checked in', () => {
    // Still bought, and still the thing they want to look at again.
    const held = heldFor([order({ id: 'a', tickets: [ticket('checked_in')] })], THIS_DATE)
    expect(held?.tickets).toBe(1)
  })

  it('links to the order itself when there is only one', () => {
    const held = heldFor([order({ id: 'a', tickets: [ticket()] })], THIS_DATE)
    expect(held?.href).toBe('/orders/a')
  })

  it('links to the list when the count spans two orders', () => {
    // Deep-linking to one of them would hide half of what the number counted.
    const held = heldFor(
      [order({ id: 'a', tickets: [ticket()] }), order({ id: 'b', tickets: [ticket()] })],
      THIS_DATE,
    )
    expect(held?.href).toBe('/orders')
  })

  it('says registered rather than bought when nothing was paid', () => {
    const held = heldFor([order({ id: 'a', amountMinor: 0, tickets: [ticket()] })], THIS_DATE)
    expect(held?.bought).toBe(false)
  })

  it('says bought when any of the orders cost money', () => {
    const held = heldFor(
      [
        order({ id: 'a', amountMinor: 0, tickets: [ticket()] }),
        order({ id: 'b', amountMinor: 20000, tickets: [ticket()] }),
      ],
      THIS_DATE,
    )
    expect(held?.bought).toBe(true)
  })
})
