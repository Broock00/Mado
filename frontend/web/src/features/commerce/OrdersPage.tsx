/**
 * Tickets somebody holds, and the moment after they pay (spec COM-003).
 *
 * The interesting screen is the pending one. An explorer comes back from a
 * provider's page with no idea whether it worked, and two things can be true:
 * the payment succeeded and the webhook has not arrived yet, or it failed. The
 * page cannot tell the difference and must not guess, so it asks the server -
 * which asks the provider - and says "checking" until there is an answer.
 *
 * It never reads the outcome out of the URL. A return link is something a
 * browser followed; `?status=success` is a claim anybody can make by editing
 * the address bar, and a page that believed it would show a ticket for a
 * payment that never happened.
 */

import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Clock, Ticket as TicketIcon, TriangleAlert } from 'lucide-react'

import { api, ApiError } from '@/lib/api'
import { useAppStore } from '@/app/store'
import type { Order } from '@/lib/types'
import { Badge, Button, Card, EmptyState, SectionHeading } from '@/design-system/primitives'
import { money } from '@/lib/money'
import { TicketQr } from '@/features/commerce/TicketQr'

const STATUS_TONE: Record<string, 'success' | 'danger' | 'neutral'> = {
  paid: 'success',
  pending: 'neutral',
  failed: 'danger',
  expired: 'danger',
  cancelled: 'neutral',
}

const STATUS_LABEL: Record<string, string> = {
  paid: 'Paid',
  pending: 'Waiting for payment',
  failed: 'Payment failed',
  expired: 'Hold expired',
  cancelled: 'Cancelled',
}

function when(iso: string): string {
  return new Date(iso).toLocaleString([], {
    weekday: 'short',
    day: 'numeric',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function Countdown({ expiresAt }: { expiresAt: string }) {
  const [left, setLeft] = useState(() => Date.parse(expiresAt) - Date.now())

  useEffect(() => {
    const timer = setInterval(() => setLeft(Date.parse(expiresAt) - Date.now()), 1000)
    return () => clearInterval(timer)
  }, [expiresAt])

  if (left <= 0) return <span>Your places have been released.</span>
  const minutes = Math.floor(left / 60000)
  const seconds = Math.floor((left % 60000) / 1000)
  return (
    <span>
      Your places are held for {minutes}:{String(seconds).padStart(2, '0')}
    </span>
  )
}

function Tickets({ order, detailed = false }: { order: Order; detailed?: boolean }) {
  if (order.tickets.length === 0) return null

  // The square only on the ticket's own page. A list of six orders is a list,
  // and six QR codes in it is a wall - the one being shown at a door is opened
  // deliberately.
  if (!detailed) {
    return (
      <ul className="mt-3 space-y-2">
        {order.tickets.map((ticket) => (
          <li
            key={ticket.id}
            className="flex items-center justify-between rounded-lg border border-sand-300 bg-white px-3 py-2"
          >
            <span className="text-sm text-sand-700">{ticket.ticketTypeName}</span>
            {/* Monospace and spaced, because this gets read aloud at a door. */}
            <span className="font-mono text-sm tracking-widest text-sand-900">{ticket.code}</span>
          </li>
        ))}
      </ul>
    )
  }

  return (
    <ul className="mt-3 grid gap-3 sm:grid-cols-2">
      {order.tickets.map((ticket) => (
        <li
          key={ticket.id}
          className="flex flex-col items-center gap-2 rounded-xl border border-sand-300 bg-white p-4"
        >
          <span className="text-sm font-medium text-sand-800">{ticket.ticketTypeName}</span>
          <TicketQr code={ticket.code} />
          {/* Said plainly, because somebody turned away at a door with a ticket
              that reads "issued" on their phone has no idea why. */}
          {ticket.checkedInAt && (
            <Badge tone="neutral">Used {when(ticket.checkedInAt)}</Badge>
          )}
          {ticket.status === 'void' && <Badge tone="danger">Not valid</Badge>}
        </li>
      ))}
    </ul>
  )
}

function OrderCard({ order, detailed = false }: { order: Order; detailed?: boolean }) {
  return (
    <Card className="p-4">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <Link
            className="font-medium text-sand-900 hover:underline"
            to={`/experiences/${order.experienceId}`}
          >
            {order.experienceTitle}
          </Link>
          <p className="text-sm text-sand-600">{when(order.startsAt)}</p>
        </div>
        <Badge tone={STATUS_TONE[order.status] ?? 'neutral'}>
          {STATUS_LABEL[order.status] ?? order.status}
        </Badge>
      </div>

      <p className="mt-2 text-sm text-sand-700">
        {order.quantity} {order.quantity === 1 ? 'ticket' : 'tickets'} ·{' '}
        {order.amountMinor === 0 ? 'Free' : money(order.amountMinor, order.currency)}
      </p>

      {order.outcomeReason && <p className="mt-1 text-sm text-sand-600">{order.outcomeReason}</p>}

      {detailed && <Tickets order={order} detailed={detailed} />}

      {!detailed && order.tickets.length > 0 && (
        <Link className="mt-2 inline-block text-sm text-brand-700 hover:underline" to={`/orders/${order.id}`}>
          Show {order.tickets.length === 1 ? 'ticket' : 'tickets'}
        </Link>
      )}
    </Card>
  )
}

export function OrderDetailPage() {
  const { orderId } = useParams<{ orderId: string }>()
  const queryClient = useQueryClient()
  const [error, setError] = useState<string | null>(null)

  const { data: order } = useQuery({
    queryKey: ['order', orderId],
    queryFn: () => api.order(orderId!),
    enabled: Boolean(orderId),
    // While it is pending the server is asking the provider on every read, so
    // polling here is what turns "come back later" into an answer on screen.
    // It stops the moment the order settles.
    refetchInterval: (query) => (query.state.data?.status === 'pending' ? 3000 : false),
  })

  const simulate = useMutation({
    mutationFn: (paid: boolean) => api.simulatePayment(order!.reference, paid),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['order', orderId] }),
    onError: (caught) =>
      setError(caught instanceof ApiError ? caught.message : 'That did not work.'),
  })

  const cancel = useMutation({
    mutationFn: () => api.cancelOrder(orderId!),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['order', orderId] }),
  })

  if (!order) return null

  return (
    <div className="mx-auto w-full max-w-2xl px-4 pb-24 pt-6 sm:px-6">
      <h1 className="text-2xl font-semibold tracking-tight text-sand-900">Your order</h1>

      <div className="mt-4">
        <OrderCard order={order} detailed />
      </div>

      {order.status === 'pending' && (
        <Card className="mt-3 p-4">
          <p className="flex items-center gap-2 text-sand-800">
            <Clock className="size-4 animate-pulse" aria-hidden />
            Checking with the payment provider.
          </p>
          <p className="mt-1 text-sm text-sand-600">
            We ask them directly rather than taking the browser’s word for it, so this can
            take a moment. {order.expiresAt && <Countdown expiresAt={order.expiresAt} />}
          </p>

          <div className="mt-3 flex flex-wrap gap-2">
            {order.checkoutUrl && (
              <a href={order.checkoutUrl}>
                <Button variant="secondary">Back to payment</Button>
              </a>
            )}
            <Button variant="ghost" onClick={() => cancel.mutate()} loading={cancel.isPending}>
              Give up on this order
            </Button>
          </div>

          {/*
            Development only, and it is meant to look like scaffolding. The stub
            provider refuses to report a payment nobody made, so somebody has to
            say what the imaginary explorer did. The endpoint refuses outright
            when a real provider is configured.
          */}
          {import.meta.env.DEV && (
            <div className="mt-4 rounded-lg border border-dashed border-sand-400 p-3">
              <p className="flex items-center gap-2 text-sm font-medium text-sand-700">
                <TriangleAlert className="size-4" aria-hidden />
                Development: no real provider is configured
              </p>
              <div className="mt-2 flex gap-2">
                <Button size="sm" onClick={() => simulate.mutate(true)}>
                  Simulate paying
                </Button>
                <Button size="sm" variant="secondary" onClick={() => simulate.mutate(false)}>
                  Simulate failing
                </Button>
              </div>
              {error && <p className="mt-2 text-sm text-danger">{error}</p>}
            </div>
          )}
        </Card>
      )}

      {order.status === 'paid' && (
        <p className="mt-3 text-sm text-sand-600">
          Show a code at the door. Each one admits one person.
        </p>
      )}
    </div>
  )
}

export function OrdersPage() {
  const user = useAppStore((s) => s.user)
  const { data: orders } = useQuery({
    queryKey: ['my-orders'],
    queryFn: () => api.myOrders(),
    enabled: Boolean(user),
  })

  if (!user) {
    return (
      <div className="mx-auto max-w-3xl px-4 py-16">
        <EmptyState
          icon={<TicketIcon className="size-8" />}
          title="Sign in"
          description="Tickets belong to an account."
          action={
            <Link to="/signin">
              <Button>Sign in</Button>
            </Link>
          }
        />
      </div>
    )
  }

  return (
    <div className="mx-auto w-full max-w-2xl px-4 pb-24 pt-6 sm:px-6">
      <SectionHeading title="Your tickets" subtitle="Everything you have paid for." />

      {orders && orders.length > 0 ? (
        <div className="mt-4 space-y-3">
          {orders.map((order) => (
            <OrderCard key={order.id} order={order} />
          ))}
        </div>
      ) : (
        <Card className="mt-4 p-5">
          <EmptyState
            icon={<TicketIcon className="size-8" />}
            title="No tickets yet"
            description="Anything you buy will be here, with a code to show at the door."
          />
        </Card>
      )}
    </div>
  )
}
