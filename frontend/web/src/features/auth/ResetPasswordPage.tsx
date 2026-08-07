/**
 * Choosing a new password from a reset link (spec SECURITY-01).
 *
 * Succeeding here signs the explorer in on this device and signs every other
 * device out. The page says so before they submit rather than after: someone
 * resetting a password because a family member uses their laptop deserves to
 * know that laptop is about to be logged out, and someone resetting because an
 * intruder has their account deserves to know that is exactly what will happen.
 *
 * The strength rule is stated up front instead of being sprung as a validation
 * error. The backend applies the same floor here as at signup - a password
 * requirement that can be stepped around by asking for a reset link is not a
 * requirement.
 */

import { useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { useMutation } from '@tanstack/react-query'
import { MailWarning } from 'lucide-react'

import { api, tokenStore } from '@/lib/api'
import { useAppStore } from '@/app/store'
import { Button, Card, Input } from '@/design-system/primitives'

const MIN_LENGTH = 10

export function ResetPasswordPage() {
  const [searchParams] = useSearchParams()
  const token = searchParams.get('token')
  const navigate = useNavigate()
  const setUser = useAppStore((s) => s.setUser)

  const [password, setPassword] = useState('')
  const [confirmation, setConfirmation] = useState('')

  const reset = useMutation({
    mutationFn: () => api.resetPassword(token ?? '', password),
    onSuccess: (result) => {
      tokenStore.set(result.tokens.accessToken, result.tokens.refreshToken)
      setUser(result.user)
      navigate('/', { replace: true })
    },
  })

  const mismatch = confirmation.length > 0 && password !== confirmation
  const tooShort = password.length > 0 && password.length < MIN_LENGTH
  const ready = password.length >= MIN_LENGTH && password === confirmation

  if (!token) {
    return (
      <div className="mx-auto max-w-md px-4 py-16 text-center">
        <Card className="p-8">
          <MailWarning className="mx-auto size-10 text-sand-400" aria-hidden />
          <h1 className="mt-4 text-xl font-semibold text-sand-900">This link is incomplete</h1>
          <p className="mt-2 text-sand-600">
            Open the link from the email exactly as it was sent.
          </p>
          <Link to="/forgot-password" className="mt-6 inline-block">
            <Button variant="secondary">Ask for a new link</Button>
          </Link>
        </Card>
      </div>
    )
  }

  return (
    <div className="mx-auto max-w-md px-4 py-16">
      <Card className="p-8">
        <h1 className="text-xl font-semibold text-sand-900">Choose a new password</h1>
        <p className="mt-1 text-sand-600">
          At least {MIN_LENGTH} characters, mixing letters with numbers or symbols.
        </p>

        <form
          className="mt-6 space-y-4"
          onSubmit={(event) => {
            event.preventDefault()
            if (ready) reset.mutate()
          }}
        >
          <Input
            type="password"
            autoComplete="new-password"
            required
            aria-label="New password"
            placeholder="New password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
          <Input
            type="password"
            autoComplete="new-password"
            required
            aria-label="Repeat new password"
            placeholder="Repeat it"
            value={confirmation}
            onChange={(event) => setConfirmation(event.target.value)}
          />

          {tooShort && (
            <p className="text-sm text-sand-600">
              A few more characters - {MIN_LENGTH} is the minimum.
            </p>
          )}
          {mismatch && <p className="text-sm text-sand-600">Those two do not match.</p>}

          <Button type="submit" className="w-full" disabled={!ready || reset.isPending}>
            {reset.isPending ? 'Saving…' : 'Set password and sign in'}
          </Button>
        </form>

        <p className="mt-4 text-sm text-sand-500">
          Every other signed-in device will be signed out.
        </p>

        {reset.isError && (
          <p className="mt-3 text-sm text-red-700" role="alert">
            {(reset.error as Error).message}{' '}
            <Link to="/forgot-password" className="underline">
              Ask for a new link
            </Link>
          </p>
        )}
      </Card>
    </div>
  )
}
