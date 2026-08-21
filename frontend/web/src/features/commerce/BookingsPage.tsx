/**
 * How one date is selling, and who is coming (spec COM-002, COM-003).
 *
 * Two questions a publisher asks together, so they are answered on one screen
 * rather than in a dashboard and a door list.
 *
 * **Money held is shown beside money taken, never added to it.** A pending
 * order is holding a seat and may still lapse in twenty minutes. Summing the
 * two would report income that does not exist, and a publisher deciding whether
 * to run the night on those numbers deserves the honest pair.
 *
 * **The seat count includes pending, because the room does.** Those places are
 * gone until the hold expires, and a page that offered them again would sell
 * the same chair twice.
 *
 * Names and nothing else. Somebody who bought a ticket has told this publisher
 * they are coming and expects to be found at the door; their email and whatever
 * else they have booked is not part of that.
 */

import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { ArrowLeft, ScanLine, Ticket as TicketIcon, Users } from 'lucide-react'

import { api } from '@/lib/api'
import type { Bookings } from '@/lib/types'
import { Badge, Button, Card, EmptyState, SectionHeading, Skeleton } from '@/design-system/primitives'
import { money } from '@/lib/money'
import { cn } from '@/lib/utils'

function when(value: string): string {
  // The device's own clock. A publisher looking at their own dates is almost
  // always in the city they are running them in, and the listing carries no
  // timezone of its own to prefer over that.
  return new Date(value).toLocaleString(undefined, {
    weekday: 'short',
    day: 'numeric',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function Stat({
  label,
  value,
  hint,
  tone,
}: {
  label: string
  value: string
  hint?: string
  tone?: 'held'
}) {
  return (
    <Card className="p-4">
      <p className="text-sm text-sand-600">{label}</p>
      <p
        className={cn(
          'mt-1 text-2xl font-semibold tabular-nums',
          tone === 'held' ? 'text-sand-500' : 'text-sand-900',
        )}
      >
        {value}
      </p>
      {hint && <p className="mt-0.5 text-xs text-sand-500">{hint}</p>}
    </Card>
  )
}

export function BookingsPage() {
  const { experienceId } = useParams<{ experienceId: string }>()
  const [occurrenceId, setOccurrenceId] = useState<string | null>(null)

  const { data: post, isLoading: loadingPost } = useQuery({
    queryKey: ['my-post', experienceId],
    queryFn: () => api.myPost(experienceId!),
    enabled: Boolean(experienceId),
  })

  const dates = post?.upcomingEvents ?? []

  // Derived rather than set in an effect. `dates` is a new array on every
  // render, so an effect depending on it runs on every render; falling back to
  // the first date here needs no effect at all, and an explicit choice still
  // wins because it is only null until the publisher makes one.
  const selected = occurrenceId ?? dates[0]?.id ?? null

  const { data, isLoading } = useQuery<Bookings>({
    queryKey: ['bookings', experienceId, selected],
    queryFn: () => api.bookings(experienceId!, selected!),
    enabled: Boolean(experienceId && selected),
    // Somebody is buying while this is open. Stale numbers on a door list are
    // worse than a request.
    refetchInterval: 30_000,
  })

  if (loadingPost) {
    return <Skeleton className="mx-auto mt-8 h-64 w-full max-w-4xl" />
  }

  if (!post) {
    return (
      <EmptyState
        title="That post is not yours"
        description="Only the publisher of a listing can see who booked it."
      />
    )
  }

  return (
    <div className="mx-auto w-full max-w-4xl px-4 py-6 sm:px-6">
      <Link
        to="/posts"
        className="inline-flex items-center gap-1.5 text-sm text-sand-600 hover:text-sand-900"
      >
        <ArrowLeft className="size-4" aria-hidden />
        Back to your posts
      </Link>

      <div className="mt-3">
        <SectionHeading title={post.title} subtitle="What this date has sold, and who is coming" />
      </div>

      {dates.length === 0 ? (
        <EmptyState
          title="This listing has no dates"
          description="Add a date and some tickets to it, and what sells will show up here."
        />
      ) : (
        <>
          <div className="mt-4 flex flex-wrap gap-2">
            {dates.map((date) => (
              <button
                key={date.id}
                type="button"
                onClick={() => setOccurrenceId(date.id)}
                className={cn(
                  'rounded-pill border px-3 py-1.5 text-sm transition-colors',
                  date.id === selected
                    ? 'border-brand-500 bg-brand-900/40 font-medium text-brand-200'
                    : 'border-sand-300 text-sand-700 hover:bg-sand-200',
                )}
              >
                {when(date.startTime)}
              </button>
            ))}
          </div>

          {/* The scanner belongs to the date shown above, and is reached from
              it rather than from a menu: the thing that makes it safe is that
              it can only admit tickets for this one. */}
          {selected && (
            <Link to={`/posts/${experienceId}/events/${selected}/scan`}>
              <Button variant="secondary" size="sm" className="mt-4">
                <ScanLine className="size-4" aria-hidden />
                Scan tickets at the door
              </Button>
            </Link>
          )}

          {isLoading || !data ? (
            <Skeleton className="mt-6 h-56 w-full" />
          ) : (
            <>
              <div className="mt-5 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                <Stat
                  label="Tickets sold"
                  value={String(data.ticketsPaid)}
                  hint={
                    data.ticketsPending > 0
                      ? `${data.ticketsPending} more being paid for`
                      : undefined
                  }
                />
                <Stat
                  label="Taken"
                  value={money(data.revenueMinor, data.currency)}
                  hint={`${data.ordersPaid} paid ${data.ordersPaid === 1 ? 'order' : 'orders'}`}
                />
                {/* Deliberately its own number and greyed. It is not income. */}
                <Stat
                  label="Being paid"
                  value={money(data.pendingMinor, data.currency)}
                  hint={
                    data.ordersPending > 0
                      ? `${data.ordersPending} not finished yet`
                      : 'nothing waiting'
                  }
                  tone="held"
                />
                <Stat
                  label="Seats left"
                  value={
                    data.seatsRemaining == null ? 'No limit' : String(data.seatsRemaining)
                  }
                  hint={
                    data.capacity != null
                      ? `${data.seatsTaken} of ${data.capacity} gone`
                      : `${data.seatsTaken} taken`
                  }
                />
              </div>

              {data.tiers.length > 0 && (
                <Card className="mt-5 overflow-hidden">
                  <div className="flex items-center gap-2 border-b border-sand-200 px-4 py-3">
                    <TicketIcon className="size-4 text-sand-500" aria-hidden />
                    <h3 className="font-medium text-sand-800">By ticket</h3>
                  </div>
                  <div className="overflow-x-auto">
                    <table className="w-full text-sm">
                      <thead className="text-left text-sand-600">
                        <tr className="border-b border-sand-200">
                          <th className="px-4 py-2 font-medium">Ticket</th>
                          <th className="px-4 py-2 font-medium">Price</th>
                          <th className="px-4 py-2 font-medium">Sold</th>
                          <th className="px-4 py-2 font-medium">Taken</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-sand-100">
                        {data.tiers.map((tier) => (
                          <tr key={tier.ticketTypeId}>
                            <td className="px-4 py-2 font-medium text-sand-800">{tier.name}</td>
                            <td className="px-4 py-2 tabular-nums text-sand-700">
                              {tier.priceMinor === 0
                                ? 'Free'
                                : money(tier.priceMinor, data.currency)}
                            </td>
                            <td className="px-4 py-2 tabular-nums text-sand-700">
                              {tier.sold}
                              {tier.quantity != null && (
                                <span className="text-sand-500"> of {tier.quantity}</span>
                              )}
                            </td>
                            <td className="px-4 py-2 tabular-nums text-sand-700">
                              {money(tier.revenueMinor, data.currency)}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </Card>
              )}

              <Card className="mt-5 overflow-hidden">
                <div className="flex items-center gap-2 border-b border-sand-200 px-4 py-3">
                  <Users className="size-4 text-sand-500" aria-hidden />
                  <h3 className="font-medium text-sand-800">
                    Who is coming
                    <span className="ml-1.5 font-normal text-sand-500">
                      {data.buyers.length}
                      {data.buyers.length === 1 ? ' booking' : ' bookings'}
                    </span>
                  </h3>
                </div>

                {data.buyers.length === 0 ? (
                  <p className="px-4 py-6 text-sm text-sand-600">
                    Nobody has booked this date yet.
                  </p>
                ) : (
                  <ul className="divide-y divide-sand-100">
                    {data.buyers.map((buyer) => (
                      <li
                        key={buyer.orderId}
                        className="flex flex-wrap items-center justify-between gap-2 px-4 py-3"
                      >
                        <div className="min-w-0">
                          <p className="font-medium text-sand-900">{buyer.name}</p>
                          <p className="text-sm text-sand-600">
                            {buyer.tiers.join(' · ') || `${buyer.quantity} tickets`}
                          </p>
                        </div>
                        <div className="flex items-center gap-2">
                          <span className="tabular-nums text-sm text-sand-700">
                            {money(buyer.amountMinor, data.currency)}
                          </span>
                          {buyer.status === 'paid' ? (
                            <Badge tone="success">Paid</Badge>
                          ) : (
                            <Badge tone="neutral">Being paid</Badge>
                          )}
                        </div>
                      </li>
                    ))}
                  </ul>
                )}
              </Card>

              {data.ordersFailed > 0 && (
                <p className="mt-3 text-xs text-sand-500">
                  {data.ordersFailed} {data.ordersFailed === 1 ? 'order' : 'orders'} did not
                  complete. They are holding nothing and are not counted above.
                </p>
              )}
            </>
          )}
        </>
      )}
    </div>
  )
}
