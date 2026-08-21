/**
 * Account security in settings (spec SECURITY-01 s6, s13).
 *
 * Three things, in the order someone needs them:
 *
 * - **Confirm your email**, shown only while unconfirmed. It is the one thing
 *   here that has to happen and it is easy to skip during signup.
 * - **Change your password**, which requires the current one. Being signed in
 *   is not proof of ownership - a browser left open on a shared machine is
 *   signed in too, and that should not be enough to take an account for good.
 * - **Signed-in devices**, which is how most people discover their account has
 *   been taken. A row nobody recognises is the whole feature.
 *
 * Every device is listed with when it signed in, because "Chrome on Windows"
 * describes half the internet and the timestamp is what makes a row identifiable
 * as yours or not.
 */

import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { KeyRound, MailCheck, Monitor } from 'lucide-react'

import { api, tokenStore } from '@/lib/api'
import { useAppStore } from '@/app/store'
import type { AuthSession } from '@/lib/types'
import { Badge, Button, Card, Input, SectionHeading } from '@/design-system/primitives'

const MIN_LENGTH = 10

function describe(session: AuthSession): string {
  const agent = session.userAgent ?? ''
  if (!agent) return session.platform ?? 'Unknown device'

  // Deliberately coarse. A full user-agent parser is a dependency that needs
  // updating forever, and the question this answers is only "is this me?".
  const browser =
    /Edg\//.test(agent) ? 'Edge'
    : /OPR\/|Opera/.test(agent) ? 'Opera'
    : /Firefox\//.test(agent) ? 'Firefox'
    : /Chrome\//.test(agent) ? 'Chrome'
    : /Safari\//.test(agent) ? 'Safari'
    : null
  const system =
    /Android/.test(agent) ? 'Android'
    : /iPhone|iPad|iOS/.test(agent) ? 'iOS'
    : /Windows/.test(agent) ? 'Windows'
    : /Mac OS X|Macintosh/.test(agent) ? 'macOS'
    : /Linux/.test(agent) ? 'Linux'
    : null

  if (browser && system) return `${browser} on ${system}`
  if (browser || system) return browser ?? system ?? agent
  // Not a browser at all - a script or a tool. Shown raw, trimmed, because that
  // is precisely the row somebody should look twice at.
  return agent.slice(0, 40)
}

function when(iso: string): string {
  return new Date(iso).toLocaleString([], {
    day: 'numeric',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function VerifyEmailRow() {
  const user = useAppStore((s) => s.user)
  const [sent, setSent] = useState(false)

  const resend = useMutation({
    mutationFn: () => api.sendVerificationEmail(),
    onSuccess: () => setSent(true),
  })

  if (!user || user.isVerified) return null

  return (
    <Card className="mt-3 flex flex-wrap items-center justify-between gap-4 p-5">
      <div className="min-w-0">
        <p className="font-medium text-sand-900">Confirm your email address</p>
        <p className="mt-0.5 text-sm text-sand-600">
          Until you do, there is nowhere to send a reset link if you lose your
          password - and publishing may be held back.
        </p>
      </div>
      {sent ? (
        <span className="flex items-center gap-1.5 text-sm text-sand-700">
          <MailCheck className="size-4" aria-hidden />
          Sent - check your inbox
        </span>
      ) : (
        <Button size="sm" onClick={() => resend.mutate()} disabled={resend.isPending}>
          {resend.isPending ? 'Sending…' : 'Send me the link'}
        </Button>
      )}
      {resend.isError && (
        <p className="w-full text-sm text-red-300" role="alert">
          {(resend.error as Error).message}
        </p>
      )}
    </Card>
  )
}

function ChangePassword() {
  const [open, setOpen] = useState(false)
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [done, setDone] = useState(false)

  const change = useMutation({
    mutationFn: () => api.changePassword(current, next),
    onSuccess: (tokens) => {
      // The change revoked every session including this one, and handed back a
      // fresh pair. Storing them keeps the explorer signed in rather than
      // bouncing them to a login form for doing the responsible thing.
      tokenStore.set(tokens.accessToken, tokens.refreshToken)
      setCurrent('')
      setNext('')
      setOpen(false)
      setDone(true)
    },
  })

  if (!open) {
    return (
      <Card className="mt-3 flex flex-wrap items-center justify-between gap-4 p-5">
        <div>
          <p className="font-medium text-sand-900">Password</p>
          <p className="mt-0.5 text-sm text-sand-600">
            {done
              ? 'Changed. Every other device was signed out.'
              : 'Changing it signs out every other device.'}
          </p>
        </div>
        <Button variant="secondary" size="sm" onClick={() => setOpen(true)}>
          <KeyRound className="size-4" aria-hidden />
          Change password
        </Button>
      </Card>
    )
  }

  return (
    <Card className="mt-3 p-5">
      <p className="font-medium text-sand-900">Change your password</p>
      <form
        className="mt-3 space-y-3"
        onSubmit={(event) => {
          event.preventDefault()
          change.mutate()
        }}
      >
        <Input
          type="password"
          autoComplete="current-password"
          aria-label="Current password"
          placeholder="Current password"
          value={current}
          onChange={(event) => setCurrent(event.target.value)}
        />
        <Input
          type="password"
          autoComplete="new-password"
          aria-label="New password"
          placeholder={`New password (${MIN_LENGTH}+ characters)`}
          value={next}
          onChange={(event) => setNext(event.target.value)}
        />
        <div className="flex flex-wrap gap-2">
          <Button
            type="submit"
            size="sm"
            disabled={change.isPending || !current || next.length < MIN_LENGTH}
          >
            {change.isPending ? 'Saving…' : 'Save'}
          </Button>
          <Button variant="ghost" size="sm" onClick={() => setOpen(false)}>
            Cancel
          </Button>
        </div>
        {change.isError && (
          <p className="text-sm text-red-300" role="alert">
            {(change.error as Error).message}
          </p>
        )}
      </form>
    </Card>
  )
}

function Sessions() {
  const queryClient = useQueryClient()

  const { data: sessions, isLoading } = useQuery({
    queryKey: ['sessions'],
    queryFn: () => api.sessions(),
  })

  const revoke = useMutation({
    mutationFn: (id: string) => api.revokeSession(id),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['sessions'] }),
  })

  return (
    <>
      {isLoading && <Card className="mt-3 p-5 text-sm text-sand-500">Loading…</Card>}

      {sessions && sessions.length > 0 && (
        <Card className="mt-3 divide-y divide-sand-200 px-5">
          {sessions.map((session) => (
            <div key={session.id} className="flex items-center justify-between gap-4 py-4">
              <div className="flex min-w-0 items-start gap-3">
                <Monitor className="mt-0.5 size-4 shrink-0 text-sand-400" aria-hidden />
                <div className="min-w-0">
                  <p className="truncate text-sand-900">
                    {describe(session)}
                    {session.isCurrent && (
                      <Badge tone="neutral" className="ml-2">
                        This device
                      </Badge>
                    )}
                  </p>
                  <p className="mt-0.5 text-xs text-sand-500">
                    Signed in {when(session.createdAt)}
                  </p>
                </div>
              </div>
              {!session.isCurrent && (
                <Button
                  variant="ghost"
                  size="sm"
                  disabled={revoke.isPending}
                  onClick={() => revoke.mutate(session.id)}
                >
                  Sign out
                </Button>
              )}
            </div>
          ))}
        </Card>
      )}

      {revoke.isError && (
        <p className="mt-2 text-sm text-red-300" role="alert">
          {(revoke.error as Error).message}
        </p>
      )}
    </>
  )
}

export function AccountSecurity() {
  return (
    <section className="mt-10">
      <SectionHeading
        title="Account security"
        subtitle="Your password, and the devices currently signed in."
      />
      <VerifyEmailRow />
      <ChangePassword />
      <Sessions />
    </section>
  )
}
