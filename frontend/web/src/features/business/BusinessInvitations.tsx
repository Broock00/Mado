/**
 * Invitations waiting for you (TEAM-003/004/005).
 *
 * Rendered inline wherever it belongs rather than as a page of its own: an
 * invitation is something you answer once, and a screen you have to go looking
 * for is a screen nobody visits. It renders nothing at all when there is
 * nothing pending, so it costs no space in the common case.
 *
 * Expired invitations never arrive here — the server filters them, because an
 * invitation you cannot accept is not something to offer somebody.
 */

import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Building2, Check, X } from 'lucide-react'

import { ApiError, api } from '@/lib/api'
import { Button, Card } from '@/design-system/primitives'

export function BusinessInvitations() {
  const queryClient = useQueryClient()
  const [error, setError] = useState<string | null>(null)

  const { data: invitations } = useQuery({
    queryKey: ['business-invitations'],
    queryFn: () => api.myBusinessInvitations(),
  })

  const settle = (fn: (id: string) => Promise<unknown>) => ({
    mutationFn: fn,
    onSuccess: () => {
      setError(null)
      void queryClient.invalidateQueries({ queryKey: ['business-invitations'] })
      // Accepting adds a business you can now act for, so what you may post as
      // changes - and the composer's selector appears for the first time.
      void queryClient.invalidateQueries({ queryKey: ['publishing-identities'] })
    },
    onError: (caught: unknown) =>
      setError(caught instanceof ApiError ? caught.message : 'That did not work.'),
  })

  const accept = useMutation(settle((id: string) => api.acceptBusinessInvitation(id)))
  const decline = useMutation(settle((id: string) => api.declineBusinessInvitation(id)))

  if (!invitations || invitations.length === 0) return null

  return (
    <Card className="space-y-3 p-5">
      <h2 className="text-sm font-medium text-sand-700">Invitations</h2>

      {error && <p className="text-sm text-red-700">{error}</p>}

      <ul className="space-y-3">
        {invitations.map((invitation) => (
          <li key={invitation.id} className="flex flex-wrap items-center gap-3">
            <div className="grid size-10 shrink-0 place-items-center rounded-lg bg-sand-100">
              <Building2 className="size-5 text-sand-500" aria-hidden />
            </div>
            <div className="min-w-0 flex-1">
              <p className="truncate text-sand-900">{invitation.businessName}</p>
              <p className="text-xs text-sand-500">
                invited you to help as {invitation.roleLabel.toLowerCase()}
              </p>
            </div>
            <Button
              size="sm"
              onClick={() => accept.mutate(invitation.id)}
              loading={accept.isPending}
            >
              <Check className="size-4" aria-hidden /> Accept
            </Button>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => decline.mutate(invitation.id)}
              loading={decline.isPending}
              aria-label={`Decline the invitation from ${invitation.businessName}`}
            >
              <X className="size-4" aria-hidden />
            </Button>
          </li>
        ))}
      </ul>
    </Card>
  )
}
