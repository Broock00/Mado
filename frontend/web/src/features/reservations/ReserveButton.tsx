/**
 * Holding a place at one date (spec COM-001).
 *
 * No payment. Reserving here is telling a publisher you are coming and having a
 * place kept - which is what a supper club with twelve seats actually needs.
 *
 * Two things the interface is careful about:
 *
 * - **It says how many are left only when that is a real number.** Most
 *   listings have no capacity, and "unlimited" is shown as nothing at all
 *   rather than as a made-up figure.
 * - **"Limited" means hurry; "available" does not.** The server decides which,
 *   because scarcity is about how many places remain rather than what fraction
 *   of the room they are - four hundred seats free in a stadium is not scarce.
 */

import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, Users } from 'lucide-react'

import { api, ApiError } from '@/lib/api'
import { useAppStore } from '@/app/store'
import type { EventInstance } from '@/lib/types'
import { Badge, Button, Card, Input } from '@/design-system/primitives'

const MAX_PARTY = 10

function whenLabel(iso: string): string {
  return new Date(iso).toLocaleString([], {
    weekday: 'short',
    day: 'numeric',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
  })
}

export function ReserveButton({ occurrence }: { occurrence: EventInstance }) {
  const user = useAppStore((s) => s.user)
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(false)
  const [party, setParty] = useState(1)
  const [note, setNote] = useState('')

  const { data: availability } = useQuery({
    queryKey: ['availability', occurrence.id],
    queryFn: () => api.availability(occurrence.id),
  })

  const { data: mine } = useQuery({
    queryKey: ['my-reservations'],
    queryFn: () => api.myReservations(),
    enabled: Boolean(user),
  })

  const held = mine?.find((r) => r.eventInstanceId === occurrence.id)

  // Taking or returning a place changes the count beside the date, which comes
  // from the experience query rather than from availability. Without
  // refreshing it too, somebody reserves a seat and the "N left" next to it
  // does not move - which reads as the reservation not having worked.
  function refresh() {
    void queryClient.invalidateQueries({ queryKey: ['my-reservations'] })
    void queryClient.invalidateQueries({ queryKey: ['availability', occurrence.id] })
    void queryClient.invalidateQueries({ queryKey: ['experience'] })
  }

  const reserve = useMutation({
    mutationFn: () => api.reserve(occurrence.id, party, note.trim() || undefined),
    onSuccess: () => {
      setOpen(false)
      setNote('')
      refresh()
    },
  })

  const cancel = useMutation({
    mutationFn: () => api.cancelReservation(held!.id),
    onSuccess: refresh,
  })

  if (!availability) return null

  const full = availability.status === 'full'
  const cancelled = availability.status === 'cancelled'

  return (
    <div className="mt-2">
      <div className="flex flex-wrap items-center gap-2">
        {availability.status === 'limited' && availability.remaining != null && (
          <Badge tone="accent">
            Only {availability.remaining} {availability.remaining === 1 ? 'place' : 'places'} left
          </Badge>
        )}
        {full && <Badge tone="neutral">Fully booked</Badge>}
        {cancelled && <Badge tone="danger">Cancelled</Badge>}
      </div>

      {held ? (
        <div className="mt-2 flex flex-wrap items-center gap-3">
          <span className="flex items-center gap-1.5 text-sm text-sand-800">
            <Check className="size-4 text-brand-600" aria-hidden />
            You have {held.partySize === 1 ? 'a place' : `${held.partySize} places`} on{' '}
            {whenLabel(held.startsAt)}
          </span>
          <Button
            variant="ghost"
            size="sm"
            disabled={cancel.isPending}
            onClick={() => cancel.mutate()}
          >
            {cancel.isPending ? 'Cancelling…' : 'Cancel'}
          </Button>
        </div>
      ) : !user ? (
        <p className="mt-2 text-sm text-sand-600">
          <Link to="/signin" className="text-brand-700 underline">
            Sign in
          </Link>{' '}
          to hold a place.
        </p>
      ) : !availability.canReserve ? null : open ? (
        <Card className="mt-2 p-4">
          <label className="block text-sm font-medium text-sand-800" htmlFor="party">
            How many of you?
          </label>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <Input
              id="party"
              type="number"
              min={1}
              max={Math.min(MAX_PARTY, availability.remaining ?? MAX_PARTY)}
              value={party}
              onChange={(event) => setParty(Number(event.target.value) || 1)}
              className="w-20"
            />
            <Input
              aria-label="Anything the host should know"
              placeholder="Anything they should know (optional)"
              value={note}
              onChange={(event) => setNote(event.target.value)}
              className="min-w-[14rem] flex-1"
            />
          </div>
          <div className="mt-3 flex flex-wrap gap-2">
            <Button size="sm" disabled={reserve.isPending} onClick={() => reserve.mutate()}>
              {reserve.isPending ? 'Holding…' : 'Hold my place'}
            </Button>
            <Button variant="ghost" size="sm" onClick={() => setOpen(false)}>
              Cancel
            </Button>
          </div>
          {/* Said plainly: nothing here takes money, and turning up is the
              commitment. Somebody who expects to have paid will not turn up
              with cash. */}
          <p className="mt-2 text-xs text-sand-500">
            Nothing to pay now - this just holds your place.
          </p>
          {reserve.isError && (
            <p className="mt-2 text-sm text-red-700" role="alert">
              {(reserve.error as ApiError).code === 'NOT_ENOUGH_PLACES'
                ? 'Somebody took the last places while you were deciding.'
                : (reserve.error as Error).message}
            </p>
          )}
        </Card>
      ) : (
        <Button variant="secondary" size="sm" className="mt-2" onClick={() => setOpen(true)}>
          <Users className="size-4" aria-hidden />
          Hold a place
        </Button>
      )}
    </div>
  )
}
