/**
 * "I forgot my password" (spec SECURITY-01 s13).
 *
 * The important behaviour here is what the page refuses to tell you. It says
 * the same thing whether or not the address has an account, because the backend
 * answers the same way and the interface must not undo that: a form that
 * responds differently to a registered address is a way to find out who has an
 * account, which is the first step of a credential-stuffing run.
 *
 * That makes the confirmation deliberately hedged - "if there is an account".
 * It reads as slightly evasive, and it is, on purpose.
 */

import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation } from '@tanstack/react-query'
import { MailCheck } from 'lucide-react'

import { api } from '@/lib/api'
import { Button, Card, Input } from '@/design-system/primitives'

export function ForgotPasswordPage() {
  const [email, setEmail] = useState('')

  const ask = useMutation({
    mutationFn: () => api.forgotPassword(email.trim()),
  })

  return (
    <div className="mx-auto max-w-md px-4 py-16">
      <Card className="p-8">
        {ask.isSuccess ? (
          <div className="text-center">
            <MailCheck className="mx-auto size-10 text-brand-600" aria-hidden />
            <h1 className="mt-4 text-xl font-semibold text-sand-900">Check your email</h1>
            <p className="mt-2 text-sand-600">
              If there is an account for {email.trim()}, a reset link is on its way.
              It works for one hour.
            </p>
            <p className="mt-4 text-sm text-sand-500">
              Nothing arrived? Look in spam, and check the address for typos - we
              cannot tell you whether it matched an account.
            </p>
            <Link to="/signin" className="mt-6 inline-block">
              <Button variant="secondary">Back to sign in</Button>
            </Link>
          </div>
        ) : (
          <>
            <h1 className="text-xl font-semibold text-sand-900">Reset your password</h1>
            <p className="mt-1 text-sand-600">
              Type the address you signed up with and we will send you a link.
            </p>

            <form
              className="mt-6 space-y-4"
              onSubmit={(event) => {
                event.preventDefault()
                ask.mutate()
              }}
            >
              <Input
                type="email"
                autoComplete="email"
                required
                aria-label="Email address"
                placeholder="you@example.com"
                value={email}
                onChange={(event) => setEmail(event.target.value)}
              />
              <Button type="submit" className="w-full" disabled={ask.isPending || !email.trim()}>
                {ask.isPending ? 'Sending…' : 'Send reset link'}
              </Button>
            </form>

            {ask.isError && (
              <p className="mt-3 text-sm text-red-700" role="alert">
                {(ask.error as Error).message}
              </p>
            )}

            <p className="mt-6 text-center text-sm text-sand-600">
              <Link to="/signin" className="text-brand-700 underline">
                Back to sign in
              </Link>
            </p>
          </>
        )}
      </Card>
    </div>
  )
}
