/**
 * Landing page for the confirmation link (spec SECURITY-01).
 *
 * The whole page is one automatic action, so it exists mainly to say what
 * happened. Three states worth distinguishing, because "it didn't work" tells
 * someone nothing about what to do next:
 *
 * - confirmed, and they can carry on
 * - the link is stale or already used, and they need a fresh one
 * - they are signed out, so the "send another" button cannot be offered here
 *
 * Confirmation does not require being signed in. The link may well be opened on
 * a phone that has never seen this account - the token is the proof, which is
 * the entire point of mailing it to an address only they can read.
 */

import { useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery } from '@tanstack/react-query'
import { CheckCircle2, MailWarning } from 'lucide-react'

import { api } from '@/lib/api'
import { useAppStore } from '@/app/store'
import { Button, Card } from '@/design-system/primitives'

export function VerifyEmailPage() {
  const [searchParams] = useSearchParams()
  const token = searchParams.get('token')
  const user = useAppStore((s) => s.user)
  const setUser = useAppStore((s) => s.setUser)
  const [resent, setResent] = useState(false)

  /**
   * A query rather than a mutation fired from an effect, which is what this was
   * first written as and why it hung.
   *
   * A mutation's state belongs to the component that started it. React mounts
   * twice in development, and the remount throws away the observer that was
   * waiting - so the confirmation succeeded on the server while the page sat on
   * "Confirming…" forever. Guarding the effect with a ref stopped the double
   * request but made the hang permanent rather than fixing it.
   *
   * A query is cached by key instead. The second mount finds the in-flight
   * request under the same key and attaches to it, so the token is spent once
   * and the result is displayed either way. `retry: false` matters: these
   * tokens are single-use, and a retry would spend a fresh one against a
   * failure that is never transient.
   */
  const confirm = useQuery({
    queryKey: ['confirm-email', token],
    queryFn: () => api.confirmEmail(token as string),
    enabled: Boolean(token),
    retry: false,
    staleTime: Infinity,
    gcTime: Infinity,
  })

  useEffect(() => {
    if (confirm.data) setUser(confirm.data)
  }, [confirm.data, setUser])

  const resend = useMutation({
    mutationFn: () => api.sendVerificationEmail(),
    onSuccess: () => setResent(true),
  })

  const succeeded = confirm.isSuccess

  return (
    <div className="mx-auto flex max-w-lg flex-col items-center px-4 py-20 text-center">
      <Card className="w-full p-8">
        {!token ? (
          <>
            <MailWarning className="mx-auto size-10 text-sand-400" aria-hidden />
            <h1 className="mt-4 text-xl font-semibold text-sand-900">
              This link is incomplete
            </h1>
            <p className="mt-2 text-sand-600">
              Open the link from the email exactly as it was sent - copying it by
              hand usually drops the end of it.
            </p>
          </>
        ) : confirm.isPending ? (
          <p className="text-sand-600">Confirming…</p>
        ) : succeeded ? (
          <>
            <CheckCircle2 className="mx-auto size-10 text-brand-600" aria-hidden />
            <h1 className="mt-4 text-xl font-semibold text-sand-900">
              Your email is confirmed
            </h1>
            <p className="mt-2 text-sand-600">
              That is everything. You can publish, and if you ever lose your
              password we have somewhere to send a reset link.
            </p>
            <Link to="/" className="mt-6 inline-block">
              <Button>Start exploring</Button>
            </Link>
          </>
        ) : (
          <>
            <MailWarning className="mx-auto size-10 text-sand-400" aria-hidden />
            <h1 className="mt-4 text-xl font-semibold text-sand-900">
              This link no longer works
            </h1>
            <p className="mt-2 text-sand-600">
              Confirmation links last a day and can only be used once. This one
              has expired, or it has already done its job.
            </p>

            {user ? (
              <div className="mt-6">
                {resent ? (
                  <p className="text-sm text-sand-700">
                    Sent. Check your inbox for a new link.
                  </p>
                ) : (
                  <Button onClick={() => resend.mutate()} disabled={resend.isPending}>
                    {resend.isPending ? 'Sending…' : 'Send me another'}
                  </Button>
                )}
                {resend.isError && (
                  <p className="mt-2 text-sm text-red-300" role="alert">
                    {(resend.error as Error).message}
                  </p>
                )}
              </div>
            ) : (
              <p className="mt-6 text-sm text-sand-600">
                <Link to="/signin" className="text-brand-700 underline">
                  Sign in
                </Link>{' '}
                and we can send you a fresh one.
              </p>
            )}
          </>
        )}
      </Card>
    </div>
  )
}
