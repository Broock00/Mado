/**
 * Asking to be verified (spec TRST-001).
 *
 * Lives on "Your posts" rather than in settings, because verification is
 * something a *publisher* wants and this is the only page where someone is
 * thinking of themselves as one.
 *
 * The copy works hard to say what the badge is not. A verified publisher is one
 * whose identity somebody checked - it is not an endorsement, and it does not
 * make their listings rank higher in any way an explorer would notice. Letting
 * publishers believe otherwise turns a trust signal into something worth gaming.
 *
 * Renders nothing at all until the publisher exists. Verification applies to a
 * publisher, so someone who has never posted has nothing to verify, and offering
 * them a button that can only fail is worse than offering nothing.
 */

import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { BadgeCheck, Clock } from 'lucide-react'

import { api } from '@/lib/api'
import { Button, Card, Input } from '@/design-system/primitives'

export function VerificationCard() {
  const queryClient = useQueryClient()
  const [asking, setAsking] = useState(false)
  const [note, setNote] = useState('')

  const { data: verification } = useQuery({
    queryKey: ['my-verification'],
    queryFn: () => api.myVerification(),
    // A 404 means they have no publisher yet, which is a normal state for a new
    // account rather than a failure worth retrying.
    retry: false,
  })

  const ask = useMutation({
    mutationFn: () => api.requestVerification(note.trim() || undefined),
    onSuccess: () => {
      setAsking(false)
      setNote('')
      void queryClient.invalidateQueries({ queryKey: ['my-verification'] })
    },
  })

  if (!verification) return null

  if (verification.verificationStatus === 'verified') {
    return (
      <Card className="mb-5 flex items-start gap-3 p-4">
        <BadgeCheck className="mt-0.5 size-5 shrink-0 text-brand-600" aria-hidden />
        <div>
          <p className="font-medium text-sand-900">You are verified</p>
          <p className="mt-0.5 text-sm text-sand-600">
            Explorers can see that someone checked who you are. It is a statement
            about your identity, not about your listings.
          </p>
        </div>
      </Card>
    )
  }

  if (verification.verificationStatus === 'requested') {
    return (
      <Card className="mb-5 flex items-start gap-3 p-4">
        <Clock className="mt-0.5 size-5 shrink-0 text-sand-500" aria-hidden />
        <div>
          <p className="font-medium text-sand-900">Verification is being reviewed</p>
          <p className="mt-0.5 text-sm text-sand-600">
            A person reads every request, so this takes a little while. Nothing about
            your posts changes in the meantime.
          </p>
        </div>
      </Card>
    )
  }

  return (
    <Card className="mb-5 p-4">
      <div className="flex items-start gap-3">
        <BadgeCheck className="mt-0.5 size-5 shrink-0 text-sand-400" aria-hidden />
        <div className="min-w-0 flex-1">
          <p className="font-medium text-sand-900">Get verified</p>
          <p className="mt-0.5 text-sm text-sand-600">
            A verified badge tells explorers that someone checked who you are. It
            does not affect how your posts are ranked.
          </p>

          {asking ? (
            <div className="mt-3 space-y-2">
              <Input
                aria-label="What should a reviewer know about you?"
                placeholder="A licence number, a website, anything that shows who you are"
                value={note}
                onChange={(event) => setNote(event.target.value)}
              />
              <div className="flex flex-wrap gap-2">
                <Button size="sm" disabled={ask.isPending} onClick={() => ask.mutate()}>
                  {ask.isPending ? 'Sending…' : 'Send request'}
                </Button>
                <Button variant="ghost" size="sm" onClick={() => setAsking(false)}>
                  Cancel
                </Button>
              </div>
              {ask.isError && (
                <p className="text-sm text-red-700" role="alert">
                  {(ask.error as Error).message}
                </p>
              )}
            </div>
          ) : (
            <Button variant="secondary" size="sm" className="mt-3" onClick={() => setAsking(true)}>
              Ask to be verified
            </Button>
          )}
        </div>
      </div>
    </Card>
  )
}
