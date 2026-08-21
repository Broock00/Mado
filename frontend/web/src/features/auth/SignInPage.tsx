/**
 * Sign in / register (spec 10.01.01).
 *
 * One screen for both, because the spec asks that authentication stay
 * low-friction and appear only when it unlocks value. Anonymous browsing remains
 * fully available, so this is never a wall.
 */

import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Compass } from 'lucide-react'
import { api, tokenStore, ApiError } from '@/lib/api'
import { useAppStore } from '@/app/store'
import { Button, Card, Input } from '@/design-system/primitives'

type Mode = 'signin' | 'register'

export function SignInPage() {
  const [mode, setMode] = useState<Mode>('signin')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [error, setError] = useState<string | null>(null)

  const navigate = useNavigate()
  const setUser = useAppStore((s) => s.setUser)
  const queryClient = useQueryClient()

  const submit = useMutation({
    mutationFn: async () => {
      if (mode === 'register') {
        return api.register({ email, password, displayName })
      }
      return api.login({ email, password })
    },
    onSuccess: (result) => {
      tokenStore.set(result.tokens.accessToken, result.tokens.refreshToken)
      setUser(result.user)
      // Saved state and personalization differ once signed in, so drop the
      // anonymous caches rather than showing stale unsaved cards.
      queryClient.clear()
      // A new account is asked what kind it is before it reaches anything else.
      // It is asked once, here, because that is the only moment the answer is
      // genuinely needed and the only moment somebody is expecting to be set
      // up - a prompt on the discovery page later reads as an interruption.
      // Signing in never asks: the account already answered, or is old enough
      // to have been created before the question existed.
      navigate(mode === 'register' ? '/welcome' : '/')
    },
    onError: (err) => {
      setError(
        err instanceof ApiError ? err.message : 'Something went wrong. Please try again.',
      )
    },
  })

  return (
    <div className="mx-auto flex min-h-[80vh] max-w-md flex-col justify-center px-4 py-12">
      <div className="mb-8 text-center">
        <span className="mx-auto mb-3 grid size-12 place-items-center rounded-2xl bg-brand-700 text-white">
          <Compass className="size-6" aria-hidden />
        </span>
        <h1 className="text-2xl font-semibold tracking-tight text-sand-900">
          {mode === 'signin' ? 'Welcome back' : 'Create your account'}
        </h1>
        <p className="mt-1.5 text-sm text-sand-500">
          {mode === 'signin'
            ? 'Sign in to keep your saved experiences and plans.'
            : 'Save experiences, build plans, and get better recommendations.'}
        </p>
      </div>

      <Card className="p-6">
        <form
          onSubmit={(event) => {
            event.preventDefault()
            setError(null)
            submit.mutate()
          }}
          className="space-y-4"
        >
          {mode === 'register' && (
            <div>
              <label htmlFor="displayName" className="mb-1.5 block text-sm font-medium text-sand-700">
                Your name
              </label>
              <Input
                id="displayName"
                value={displayName}
                onChange={(event) => setDisplayName(event.target.value)}
                required
                autoComplete="name"
              />
            </div>
          )}

          <div>
            <label htmlFor="email" className="mb-1.5 block text-sm font-medium text-sand-700">
              Email
            </label>
            <Input
              id="email"
              type="email"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              required
              autoComplete="email"
            />
          </div>

          <div>
            <div className="mb-1.5 flex items-baseline justify-between gap-3">
              <label htmlFor="password" className="block text-sm font-medium text-sand-700">
                Password
              </label>
              {/* Only when signing in. Offering "forgot password" beside a field
                  someone is inventing a password for is noise. */}
              {mode === 'signin' && (
                <Link to="/forgot-password" className="text-xs text-sand-600 hover:underline">
                  Forgot it?
                </Link>
              )}
            </div>
            <Input
              id="password"
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              required
              minLength={mode === 'register' ? 10 : undefined}
              autoComplete={mode === 'register' ? 'new-password' : 'current-password'}
            />
            {mode === 'register' && (
              <p className="mt-1.5 text-xs text-sand-500">
                At least 10 characters, mixing letters with numbers or symbols.
              </p>
            )}
          </div>

          {error && (
            <p role="alert" className="rounded-lg bg-red-950 px-3 py-2 text-sm text-red-300">
              {error}
            </p>
          )}

          <Button type="submit" className="w-full" size="lg" loading={submit.isPending}>
            {mode === 'signin' ? 'Sign in' : 'Create account'}
          </Button>
        </form>

        <p className="mt-5 text-center text-sm text-sand-500">
          {mode === 'signin' ? "Don't have an account? " : 'Already have an account? '}
          <button
            type="button"
            onClick={() => {
              setMode(mode === 'signin' ? 'register' : 'signin')
              setError(null)
            }}
            className="font-medium text-brand-700 hover:underline"
          >
            {mode === 'signin' ? 'Create one' : 'Sign in'}
          </button>
        </p>
      </Card>

      <p className="mt-6 text-center text-sm text-sand-500">
        <Link to="/" className="hover:underline">
          Keep browsing without an account
        </Link>
      </p>
    </div>
  )
}
