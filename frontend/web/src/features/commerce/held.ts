/**
 * What an explorer already holds for one date.
 *
 * Kept apart from `TicketPanel` because it is the part with rules in it - which
 * orders count, which tickets inside them count, and where the number should
 * lead - and because every one of those rules is wrong in a way that looks
 * right on screen. A count that quietly includes an unpaid hold, or a voided
 * ticket, or somebody's tickets for a different evening, renders as a perfectly
 * ordinary "3 tickets bought".
 */

import type { Order } from '@/lib/types'

export interface Held {
  tickets: number
  /**
   * Where "3 tickets bought" goes. The order itself when there is one, so the
   * tap lands on the codes rather than on a list to search - and the list when
   * they bought twice, because deep-linking to one of two orders would hide
   * half of what the number counted.
   */
  href: string
  /**
   * Whether any money changed hands, read from their own orders rather than
   * from the listing's price. The two disagree in a case that is easy to hit: a
   * listing that was free to register for and later added a paid tier, where
   * "bought" would be a claim about a transaction that never happened.
   */
  bought: boolean
}

/**
 * Null when they hold nothing, so a caller renders one thing or no thing.
 *
 * Scoped to the occurrence rather than the listing: the price card is about one
 * date and the button under it sells for that date, so counting every date's
 * tickets would offer "Buy more" beneath a number belonging to another evening.
 *
 * Counted from the issued tickets rather than from `order.quantity`, because a
 * voided ticket is one the explorer no longer holds while the order still
 * records having bought it.
 *
 * `/orders` returns only paid orders today, and this filters for paid anyway -
 * that is a server contract that could widen, and a pending hold shown as a
 * purchase is somebody turning up at a door with nothing that scans.
 */
export function heldFor(orders: Order[], occurrenceId: string): Held | null {
  const mine = orders.filter(
    (order) => order.eventInstanceId === occurrenceId && order.status === 'paid',
  )
  const tickets = mine.reduce(
    (sum, order) => sum + order.tickets.filter((ticket) => ticket.status !== 'void').length,
    0,
  )
  if (tickets === 0) return null
  return {
    tickets,
    href: mine.length === 1 ? `/orders/${mine[0].id}` : '/orders',
    bought: mine.some((order) => order.amountMinor > 0),
  }
}
