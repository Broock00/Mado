/**
 * The developer platform (spec DEV-001, DEV-003).
 *
 * Two things a publisher sets up once and then forgets about: a key for their
 * scripts, and an endpoint for Mado to tell them when something happened.
 *
 * The design problem here is a secret shown exactly once. Most interfaces
 * handle it badly - a modal that can be dismissed by clicking outside, a value
 * that scrolls off the page, no copy button. Losing a key is not a disaster
 * (revoke it, make another) but it is a needless one, so the reveal is a panel
 * that stays until it is dismissed on purpose, says plainly that it will not be
 * shown again, and has a copy button as its primary action.
 */

import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, Copy, Download, KeyRound, Radio, Send, Trash2 } from 'lucide-react'

import { api } from '@/lib/api'
import { useAppStore } from '@/app/store'
import type { ApiKey, Sdk, WebhookDelivery, WebhookEndpoint } from '@/lib/types'
import {
  Badge,
  Button,
  Card,
  EmptyState,
  Input,
  SectionHeading,
} from '@/design-system/primitives'
import { buttonClasses } from '@/design-system/button-styles'
import { cn } from '@/lib/utils'

function when(iso?: string | null): string {
  if (!iso) return 'never'
  return new Date(iso).toLocaleString([], {
    day: 'numeric',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
  })
}

/**
 * A secret, shown once.
 *
 * Deliberately not a modal. A modal can be dismissed by a stray click, and the
 * value is unrecoverable - so this is an inline panel that only closes when its
 * own button is pressed.
 */
function RevealedSecret({
  label,
  value,
  why,
  onDismiss,
}: {
  label: string
  value: string
  /**
   * Why it will not be shown again - which is a different reason for a key
   * than for a signing secret, so it is a prop rather than fixed text. Saying
   * "only a digest is stored" about a value we demonstrably keep in full would
   * be a lie in the one place a reader is most likely to believe it.
   */
  why: string
  onDismiss: () => void
}) {
  const [copied, setCopied] = useState(false)

  async function copy() {
    await navigator.clipboard.writeText(value)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  return (
    <Card className="mt-3 border-brand-300 bg-brand-50 p-4">
      <p className="text-sm font-medium text-sand-900">{label}</p>
      <p className="mt-1 text-sm text-sand-700">Copy it now. {why}</p>
      <code className="mt-3 block overflow-x-auto rounded-lg border border-sand-300 bg-white px-3 py-2 font-mono text-sm">
        {value}
      </code>
      <div className="mt-3 flex flex-wrap gap-2">
        <Button size="sm" onClick={copy}>
          {copied ? <Check className="size-4" aria-hidden /> : <Copy className="size-4" aria-hidden />}
          {copied ? 'Copied' : 'Copy'}
        </Button>
        <Button variant="ghost" size="sm" onClick={onDismiss}>
          I have it
        </Button>
      </div>
    </Card>
  )
}

/* --------------------------------------------------------------- API keys */

function KeyRow({ apiKey }: { apiKey: ApiKey }) {
  const queryClient = useQueryClient()
  const [confirming, setConfirming] = useState(false)

  const revoke = useMutation({
    mutationFn: () => api.revokeApiKey(apiKey.id),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['api-keys'] }),
  })

  const dead = apiKey.state !== 'active'

  return (
    <div className={cn('py-4', dead && 'opacity-60')}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="flex flex-wrap items-center gap-2 font-medium text-sand-900">
            {apiKey.name}
            {apiKey.state === 'revoked' && <Badge tone="neutral">Revoked</Badge>}
            {apiKey.state === 'expired' && <Badge tone="neutral">Expired</Badge>}
          </p>
          <code className="mt-1 block font-mono text-sm text-sand-600">{apiKey.preview}</code>
          <p className="mt-1 flex flex-wrap gap-1.5">
            {apiKey.scopes.map((scope) => (
              <Badge key={scope} tone="brand">
                {scope}
              </Badge>
            ))}
          </p>
          <p className="mt-1.5 text-xs text-sand-500">
            Created {when(apiKey.createdAt)} · Last used {when(apiKey.lastUsedAt)}
            {apiKey.expiresAt && ` · Expires ${when(apiKey.expiresAt)}`}
          </p>
        </div>

        {!dead &&
          (confirming ? (
            <div className="flex flex-wrap gap-2">
              <Button
                variant="danger"
                size="sm"
                disabled={revoke.isPending}
                onClick={() => revoke.mutate()}
              >
                Revoke for good
              </Button>
              <Button variant="ghost" size="sm" onClick={() => setConfirming(false)}>
                Cancel
              </Button>
            </div>
          ) : (
            <Button variant="ghost" size="sm" onClick={() => setConfirming(true)}>
              Revoke
            </Button>
          ))}
      </div>

      {confirming && (
        <p className="mt-2 text-xs text-sand-500">
          Anything using this key stops working immediately, and it cannot be brought back.
        </p>
      )}
      {revoke.isError && (
        <p className="mt-2 text-sm text-red-700" role="alert">
          {(revoke.error as Error).message}
        </p>
      )}
    </div>
  )
}

function NewKey({ onCreated }: { onCreated: (secret: string) => void }) {
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(false)
  const [name, setName] = useState('')
  const [chosen, setChosen] = useState<string[]>([])

  const { data: scopes } = useQuery({
    queryKey: ['developer-scopes'],
    queryFn: () => api.developerScopes(),
    staleTime: Infinity,
  })

  const create = useMutation({
    mutationFn: () => api.createApiKey({ name: name.trim(), scopes: chosen }),
    onSuccess: (result) => {
      onCreated(result.secret)
      setName('')
      setChosen([])
      setOpen(false)
      void queryClient.invalidateQueries({ queryKey: ['api-keys'] })
    },
  })

  if (!open) {
    return (
      <Button variant="secondary" size="sm" onClick={() => setOpen(true)}>
        <KeyRound className="size-4" aria-hidden />
        New key
      </Button>
    )
  }

  return (
    <Card className="mt-3 p-4">
      <Input
        aria-label="What the key is for"
        placeholder="What is it for? e.g. Nightly event sync"
        value={name}
        onChange={(event) => setName(event.target.value)}
      />
      <fieldset className="mt-3">
        <legend className="text-sm font-medium text-sand-800">What may it do?</legend>
        <div className="mt-2 space-y-2">
          {scopes?.map((scope) => (
            <label key={scope.key} className="flex cursor-pointer items-start gap-2 text-sm">
              <input
                type="checkbox"
                className="mt-0.5 size-4 rounded border-sand-300 accent-brand-600"
                checked={chosen.includes(scope.key)}
                onChange={(event) =>
                  setChosen((current) =>
                    event.target.checked
                      ? [...current, scope.key]
                      : current.filter((s) => s !== scope.key),
                  )
                }
              />
              <span>
                <code className="text-sand-900">{scope.key}</code>
                <span className="block text-sand-600">{scope.description}</span>
              </span>
            </label>
          ))}
        </div>
      </fieldset>
      <div className="mt-3 flex flex-wrap gap-2">
        <Button
          size="sm"
          disabled={create.isPending || !name.trim() || chosen.length === 0}
          onClick={() => create.mutate()}
        >
          {create.isPending ? 'Creating…' : 'Create key'}
        </Button>
        <Button variant="ghost" size="sm" onClick={() => setOpen(false)}>
          Cancel
        </Button>
      </div>
      {/* Said before they press the button, not after. Somebody who expects to
          find the key on this page later will not copy it now. */}
      <p className="mt-2 text-xs text-sand-500">
        The key is shown once, on the next screen.
      </p>
      {create.isError && (
        <p className="mt-2 text-sm text-red-700" role="alert">
          {(create.error as Error).message}
        </p>
      )}
    </Card>
  )
}

function Keys() {
  const [revealed, setRevealed] = useState<string | null>(null)
  const { data: keys } = useQuery({ queryKey: ['api-keys'], queryFn: () => api.apiKeys() })

  return (
    <section>
      <div className="flex flex-wrap items-end justify-between gap-3">
        <SectionHeading
          title="API keys"
          subtitle="For scripts. Send one as the X-Mado-Api-Key header."
        />
        <NewKey onCreated={setRevealed} />
      </div>

      {revealed && (
        <RevealedSecret
          label="Your new API key"
          value={revealed}
          why="Only a digest is stored, so there is no way to show it again - if it is lost, revoke it and make another."
          onDismiss={() => setRevealed(null)}
        />
      )}

      {keys && keys.length > 0 ? (
        <Card className="mt-3 divide-y divide-sand-200 px-5">
          {keys.map((key) => (
            <KeyRow key={key.id} apiKey={key} />
          ))}
        </Card>
      ) : (
        <Card className="mt-3 p-5">
          <EmptyState
            icon={<KeyRound className="size-8" />}
            title="No keys yet"
            description="Make one when you want a script to read or manage your listings."
          />
        </Card>
      )}
    </section>
  )
}

/* ------------------------------------------------------------------- sdks */

/**
 * Client libraries (spec DEV-002).
 *
 * Generated from the API's own description every time one is downloaded, which
 * is why there is no "last updated" here and no list of past releases: there is
 * only ever one version, and it is the one the API is serving right now. A
 * download page offering three historical builds invites somebody to pick the
 * wrong one.
 *
 * The version shown is the API version plus a digest of the endpoint surface,
 * so a partner can tell at a glance whether the copy in their repository is
 * still current without diffing it.
 */
function Sdks() {
  const { data: sdks } = useQuery({ queryKey: ['sdks'], queryFn: () => api.sdks() })

  return (
    <section className="mt-12">
      <SectionHeading
        title="Client libraries"
        subtitle="Generated from this API, so they cannot describe endpoints it does not have."
      />

      {sdks && sdks.length > 0 ? (
        <Card className="mt-3 divide-y divide-sand-200 px-5">
          {sdks.map((sdk: Sdk) => (
            <div
              key={sdk.language}
              className="flex flex-wrap items-center justify-between gap-3 py-4"
            >
              <div className="min-w-0">
                <p className="font-medium text-sand-900">{sdk.label}</p>
                <p className="mt-0.5 text-sm text-sand-600">
                  {sdk.files.join(', ')}
                </p>
                <p className="mt-1 font-mono text-xs text-sand-500">{sdk.version}</p>
              </div>
              {/*
                A plain link, not a fetch-and-blob. The browser already knows
                how to download a file - progress, cancel, and the filename the
                server chose in Content-Disposition. `download` is deliberately
                absent so that server-chosen name wins.
              */}
              <a className={buttonClasses('secondary')} href={api.sdkUrl(sdk.language)}>
                <Download className="size-4" />
                Download
              </a>
            </div>
          ))}
        </Card>
      ) : (
        <Card className="mt-3 p-5">
          <EmptyState
            icon={<Download className="size-8" />}
            title="No libraries yet"
            description="Use the API directly in the meantime - it is the same surface."
          />
        </Card>
      )}

      <p className="mt-3 text-sm text-sand-600">
        Each one covers exactly what an API key can reach. Anything the website
        does that is missing is missing on purpose: it needs a signed-in person
        rather than a key.
      </p>
    </section>
  )
}

/* --------------------------------------------------------------- webhooks */

/**
 * Unlike an API key, this one is genuinely stored - it has to be, to compute an
 * HMAC on every delivery. So the reason it is not shown again is a product
 * decision rather than a cryptographic fact, and the text says so.
 */
const SIGNING_SECRET_WHY =
  'It is not shown again from this page - rotate it if you lose it, which invalidates the old one immediately.'

const DELIVERY_TONE: Record<string, 'success' | 'danger' | 'neutral'> = {
  delivered: 'success',
  failed: 'danger',
  retrying: 'neutral',
  pending: 'neutral',
}

function Deliveries({ endpoint }: { endpoint: WebhookEndpoint }) {
  const queryClient = useQueryClient()
  const { data: rows } = useQuery({
    queryKey: ['webhook-deliveries', endpoint.id],
    queryFn: () => api.webhookDeliveries(endpoint.id),
    // Something queued is about to change state, so this is one of the few
    // places in the app where polling earns its keep.
    refetchInterval: (query) =>
      (query.state.data as WebhookDelivery[] | undefined)?.some(
        (d) => d.status === 'pending' || d.status === 'retrying',
      )
        ? 5000
        : false,
  })

  const retry = useMutation({
    mutationFn: (deliveryId: string) => api.retryWebhookDelivery(endpoint.id, deliveryId),
    onSuccess: () =>
      void queryClient.invalidateQueries({ queryKey: ['webhook-deliveries', endpoint.id] }),
  })

  if (!rows || rows.length === 0) {
    return <p className="mt-3 text-sm text-sand-500">Nothing sent yet.</p>
  }

  return (
    <ul className="mt-3 space-y-2">
      {rows.map((row) => (
        <li
          key={row.id}
          className="flex flex-wrap items-center justify-between gap-2 rounded-lg bg-sand-100 px-3 py-2"
        >
          <span className="min-w-0 text-sm text-sand-800">
            <code>{row.eventType}</code>
            {row.isTest && <span className="ml-1.5 text-sand-500">(test)</span>}
            <span className="ml-2 text-sand-500">{when(row.createdAt)}</span>
          </span>
          <span className="flex flex-wrap items-center gap-2">
            {row.attempts > 1 && (
              <span className="text-xs text-sand-500">{row.attempts} attempts</span>
            )}
            {row.error && <span className="text-xs text-red-700">{row.error}</span>}
            <Badge tone={DELIVERY_TONE[row.status] ?? 'neutral'}>{row.status}</Badge>
            {row.status === 'failed' && (
              <Button
                variant="ghost"
                size="sm"
                disabled={retry.isPending}
                onClick={() => retry.mutate(row.id)}
              >
                Retry
              </Button>
            )}
          </span>
        </li>
      ))}
    </ul>
  )
}

function EndpointRow({ endpoint }: { endpoint: WebhookEndpoint }) {
  const queryClient = useQueryClient()
  const [showing, setShowing] = useState(false)
  const [rotated, setRotated] = useState<string | null>(null)
  const [confirming, setConfirming] = useState(false)

  const refresh = () => void queryClient.invalidateQueries({ queryKey: ['webhooks'] })

  const update = useMutation({
    mutationFn: (patch: { status?: string }) => api.updateWebhook(endpoint.id, patch),
    onSuccess: refresh,
  })
  const remove = useMutation({
    mutationFn: () => api.deleteWebhook(endpoint.id),
    onSuccess: refresh,
  })
  const rotate = useMutation({
    mutationFn: () => api.rotateWebhookSecret(endpoint.id),
    onSuccess: (result) => {
      setRotated(result.secret)
      refresh()
    },
  })
  const test = useMutation({
    mutationFn: () => api.testWebhook(endpoint.id),
    onSuccess: () => {
      setShowing(true)
      void queryClient.invalidateQueries({ queryKey: ['webhook-deliveries', endpoint.id] })
    },
  })

  return (
    <div className="py-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="flex flex-wrap items-center gap-2">
            <code className="break-all font-mono text-sm text-sand-900">{endpoint.url}</code>
            {endpoint.status === 'active' && <Badge tone="success">Active</Badge>}
            {endpoint.status === 'paused' && <Badge tone="neutral">Paused</Badge>}
            {endpoint.status === 'suspended' && <Badge tone="danger">Suspended</Badge>}
          </p>
          <p className="mt-1.5 flex flex-wrap gap-1.5">
            {endpoint.events.map((event) => (
              <Badge key={event} tone="brand">
                {event}
              </Badge>
            ))}
          </p>
          <p className="mt-1.5 text-xs text-sand-500">
            Secret <code>{endpoint.secretPreview}</code> · Last delivered{' '}
            {when(endpoint.lastSuccessAt)}
          </p>
        </div>
      </div>

      {endpoint.status === 'suspended' && (
        <div className="mt-3 rounded-lg bg-red-50 px-3 py-2">
          <p className="text-sm text-red-800">
            Nothing is being sent here. {endpoint.lastError ?? 'Deliveries kept failing.'}
          </p>
          <p className="mt-1 text-xs text-red-700">
            Fix the endpoint, then set it back to active - that clears the failure count.
          </p>
        </div>
      )}

      <div className="mt-3 flex flex-wrap gap-2">
        <Button
          variant="secondary"
          size="sm"
          disabled={test.isPending}
          onClick={() => test.mutate()}
        >
          <Send className="size-4" aria-hidden />
          {test.isPending ? 'Sending…' : 'Send test'}
        </Button>
        <Button variant="ghost" size="sm" onClick={() => setShowing((open) => !open)}>
          {showing ? 'Hide history' : 'History'}
        </Button>
        <Button
          variant="ghost"
          size="sm"
          disabled={update.isPending}
          onClick={() =>
            update.mutate({ status: endpoint.status === 'active' ? 'paused' : 'active' })
          }
        >
          {endpoint.status === 'active' ? 'Pause' : 'Activate'}
        </Button>
        <Button variant="ghost" size="sm" disabled={rotate.isPending} onClick={() => rotate.mutate()}>
          Rotate secret
        </Button>
        {confirming ? (
          <>
            <Button
              variant="danger"
              size="sm"
              disabled={remove.isPending}
              onClick={() => remove.mutate()}
            >
              Delete
            </Button>
            <Button variant="ghost" size="sm" onClick={() => setConfirming(false)}>
              Cancel
            </Button>
          </>
        ) : (
          <Button variant="ghost" size="sm" onClick={() => setConfirming(true)}>
            <Trash2 className="size-4" aria-hidden />
            Remove
          </Button>
        )}
      </div>

      {rotated && (
        <RevealedSecret
          label="New signing secret"
          value={rotated}
          why={SIGNING_SECRET_WHY}
          onDismiss={() => setRotated(null)}
        />
      )}
      {/* The old secret stops working the moment this is pressed, so it is said
          before the button rather than in the confirmation after it. */}
      {rotate.isPending && (
        <p className="mt-2 text-xs text-sand-500">The old secret stops working immediately.</p>
      )}

      {showing && <Deliveries endpoint={endpoint} />}

      {(update.isError || remove.isError || rotate.isError || test.isError) && (
        <p className="mt-2 text-sm text-red-700" role="alert">
          {
            ((update.error ?? remove.error ?? rotate.error ?? test.error) as Error)
              .message
          }
        </p>
      )}
    </div>
  )
}

function NewEndpoint({ onCreated }: { onCreated: (secret: string) => void }) {
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(false)
  const [url, setUrl] = useState('')
  const [chosen, setChosen] = useState<string[]>([])

  const { data: types } = useQuery({
    queryKey: ['webhook-event-types'],
    queryFn: () => api.webhookEventTypes(),
    staleTime: Infinity,
  })

  const create = useMutation({
    mutationFn: () => api.createWebhook(url.trim(), chosen),
    onSuccess: (result) => {
      onCreated(result.secret)
      setUrl('')
      setChosen([])
      setOpen(false)
      void queryClient.invalidateQueries({ queryKey: ['webhooks'] })
    },
  })

  if (!open) {
    return (
      <Button variant="secondary" size="sm" onClick={() => setOpen(true)}>
        <Radio className="size-4" aria-hidden />
        Add endpoint
      </Button>
    )
  }

  return (
    <Card className="mt-3 p-4">
      <Input
        aria-label="Endpoint URL"
        placeholder="https://your-site.example/hooks/mado"
        value={url}
        onChange={(event) => setUrl(event.target.value)}
      />
      <fieldset className="mt-3">
        <legend className="text-sm font-medium text-sand-800">Tell me about</legend>
        <div className="mt-2 space-y-2">
          {types?.map((type) => (
            <label key={type.type} className="flex cursor-pointer items-start gap-2 text-sm">
              <input
                type="checkbox"
                className="mt-0.5 size-4 rounded border-sand-300 accent-brand-600"
                checked={chosen.includes(type.type)}
                onChange={(event) =>
                  setChosen((current) =>
                    event.target.checked
                      ? [...current, type.type]
                      : current.filter((t) => t !== type.type),
                  )
                }
              />
              <span>
                <code className="text-sand-900">{type.type}</code>
                <span className="block text-sand-600">{type.description}</span>
              </span>
            </label>
          ))}
        </div>
      </fieldset>
      <div className="mt-3 flex flex-wrap gap-2">
        <Button
          size="sm"
          disabled={create.isPending || !url.trim() || chosen.length === 0}
          onClick={() => create.mutate()}
        >
          {create.isPending ? 'Checking the URL…' : 'Add endpoint'}
        </Button>
        <Button variant="ghost" size="sm" onClick={() => setOpen(false)}>
          Cancel
        </Button>
      </div>
      <p className="mt-2 text-xs text-sand-500">
        Must be https and reachable from the internet. We check before accepting it.
      </p>
      {create.isError && (
        <p className="mt-2 text-sm text-red-700" role="alert">
          {(create.error as Error).message}
        </p>
      )}
    </Card>
  )
}

/**
 * How to check a signature.
 *
 * On the page rather than in a document somewhere, because a receiver that
 * skips this is an open endpoint on the internet that anybody who learns the
 * URL can post to.
 */
function VerifyingDeliveries() {
  return (
    <Card className="mt-3 p-5">
      <p className="text-sm font-medium text-sand-900">Check the signature</p>
      <p className="mt-1 text-sm text-sand-600">
        Every delivery carries <code>X-Mado-Signature</code> as{' '}
        <code>t=&lt;unix seconds&gt;,v1=&lt;hex&gt;</code>. The signed material is the
        timestamp, a dot, and the raw request body - not the parsed JSON, which will not
        re-serialise byte for byte.
      </p>
      <pre className="mt-3 overflow-x-auto rounded-lg bg-sand-900 px-3 py-2 text-xs text-sand-100">
        <code>{`t, v1 = parse(header)
if abs(now() - t) > 300: reject          # replay
expected = hmac_sha256(secret, f"{t}." + raw_body)
if not constant_time_equal(v1, expected): reject
if seen(body["id"]): ignore              # at-least-once delivery`}</code>
      </pre>
      <p className="mt-2 text-xs text-sand-500">
        The timestamp is inside the signature, so a captured delivery cannot be replayed
        later. Deduplicate on <code>id</code>: a retry after a network failure carries the
        same one.
      </p>
    </Card>
  )
}

function Webhooks() {
  const [revealed, setRevealed] = useState<string | null>(null)
  const { data: endpoints } = useQuery({ queryKey: ['webhooks'], queryFn: () => api.webhooks() })

  return (
    <section className="mt-12">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <SectionHeading
          title="Webhooks"
          subtitle="We tell your server when something happens, so it does not have to ask."
        />
        <NewEndpoint onCreated={setRevealed} />
      </div>

      {revealed && (
        <RevealedSecret
          label="Signing secret for this endpoint"
          value={revealed}
          why={SIGNING_SECRET_WHY}
          onDismiss={() => setRevealed(null)}
        />
      )}

      {endpoints && endpoints.length > 0 ? (
        <>
          <Card className="mt-3 divide-y divide-sand-200 px-5">
            {endpoints.map((endpoint) => (
              <EndpointRow key={endpoint.id} endpoint={endpoint} />
            ))}
          </Card>
          <VerifyingDeliveries />
        </>
      ) : (
        <Card className="mt-3 p-5">
          <EmptyState
            icon={<Radio className="size-8" />}
            title="No endpoints"
            description="Add one to be told when your listings are published or somebody reserves a place."
          />
        </Card>
      )}
    </section>
  )
}

export function DeveloperPage() {
  const user = useAppStore((s) => s.user)

  if (!user) {
    return (
      <div className="mx-auto max-w-3xl px-4 py-16">
        <EmptyState
          icon={<KeyRound className="size-8" />}
          title="Sign in"
          description="Keys and webhooks belong to an account."
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
    <div className="mx-auto w-full max-w-3xl px-4 pb-24 pt-6 sm:px-6">
      <h1 className="text-2xl font-semibold tracking-tight text-sand-900">Developers</h1>
      <p className="mt-1 text-sand-600">
        For getting your listings in and out of Mado without a browser.
      </p>

      <div className="mt-8">
        <Keys />
        <Sdks />
        <Webhooks />
      </div>
    </div>
  )
}
