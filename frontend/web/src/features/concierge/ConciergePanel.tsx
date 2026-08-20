/**
 * The AI Concierge (spec 57.04).
 *
 * Spec 57.04 s4 is emphatic that the concierge "is not a separate destination" -
 * it is a layer available from anywhere. So it lives in a slide-over reachable on
 * every screen rather than as a route of its own.
 *
 * Two spec requirements shape the rendering:
 *  - s10: results are rendered as cards, not prose. The model writes the framing;
 *    the platform renders the facts, so a hallucinated detail cannot masquerade as
 *    a real listing.
 *  - PRODUCT-00 principle 18: the assistant is visibly AI, never presented as a
 *    person.
 */

import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'

import { RepostButton } from '@/features/social/RepostButton'
import { useMutation, useQuery } from '@tanstack/react-query'
import { MapPin, Send, Sparkles, X } from 'lucide-react'
import { api } from '@/lib/api'
import { useAppStore } from '@/app/store'
import { useRequestLocation } from '@/app/hooks'
import { Badge, Button, Input } from '@/design-system/primitives'
import { cn } from '@/lib/utils'
import type { ConciergeResult, OfferedPlan, SuggestedAction } from '@/lib/types'
import { OfferedPlanCard } from './OfferedPlanCard'

interface Turn {
  role: 'user' | 'assistant'
  text: string
  // Rendered instead of the result cards when present: a plan is a sequence,
  // and showing it as a row of cards discards the order and the timings that
  // make it one.
  plan?: OfferedPlan | null
  planChange?: string | null
  conversationId?: string
  results?: ConciergeResult[]
  actions?: SuggestedAction[]
  clarification?: string | null
}

const OPENERS: SuggestedAction[] = [
  { label: 'What is on tonight?', message: 'What should I do tonight?' },
  { label: 'Free this weekend', message: 'Find me something free this weekend' },
  { label: 'Somewhere for coffee', message: 'Where can I get good traditional coffee?' },
  { label: 'I am free tonight', message: 'I am free this evening, what should I do with it?' },
]

/**
 * Something to say before being asked (spec AI-005).
 *
 * A real listing chosen by a query, not a sentence a model produced - the
 * concierge offers something it can point at. Renders nothing when there is
 * nothing worth saying, which is the common case and the right one: padding the
 * opener with whatever is most popular in town turns a suggestion into an
 * advert, and an explorer learns within a week to ignore it.
 */
function ProactiveSuggestionCard() {
  const { data } = useQuery({
    queryKey: ['proactive-suggestion'],
    queryFn: () => api.proactiveSuggestion(),
    // Nothing here changes minute to minute, and refetching every time the
    // panel opens would spend a query to show the same card.
    staleTime: 30 * 60_000,
    retry: false,
  })

  if (!data) return null

  return (
    <Link
      to={`/experiences/${data.experienceId}`}
      className="block rounded-xl border border-brand-200 bg-brand-50 px-3.5 py-3 transition-colors hover:border-brand-400"
    >
      <p className="flex items-center gap-1.5 text-xs font-medium uppercase tracking-wide text-brand-700">
        <Sparkles className="size-3" aria-hidden />
        You might like
      </p>
      <p className="mt-1 text-sm font-medium text-sand-900">{data.title}</p>
      <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-xs text-sand-500">
        {data.venueName && (
          <span className="flex items-center gap-1">
            <MapPin className="size-3" aria-hidden />
            {data.venueName}
          </span>
        )}
        {data.when && <span>· {new Date(data.when).toLocaleString([], {
          weekday: 'short', hour: '2-digit', minute: '2-digit',
        })}</span>}
      </div>
      {/* The reason is assembled from the same fields that chose it, so it can
          never claim something the query did not check. */}
      <p className="mt-1 text-xs text-brand-800">{data.reason}</p>
    </Link>
  )
}

export function ConciergePanel() {
  const open = useAppStore((s) => s.conciergeOpen)
  const toggle = useAppStore((s) => s.toggleConcierge)
  const citySlug = useAppStore((s) => s.citySlug)
  const location = useAppStore((s) => s.location)
  const place = useAppStore((s) => s.place)
  const requestLocation = useRequestLocation()

  const [turns, setTurns] = useState<Turn[]>([])
  const [input, setInput] = useState('')
  const [conversationId, setConversationId] = useState<string | null>(null)
  const scrollRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)

  const send = useMutation({
    mutationFn: (message: string) =>
      api.concierge({
        message,
        conversationId,
        city: citySlug ?? undefined,
        // The chosen place wins over where the explorer is sitting. Without
        // this the concierge answered about their own city and told anyone who
        // had picked Kenya that it only knows Addis Ababa.
        latitude: place ? place.latitude : location.granted ? location.latitude : null,
        longitude: place ? place.longitude : location.granted ? location.longitude : null,
        radiusKm: place && !place.countryCode && !place.bbox ? place.radiusKm : null,
        bbox: place?.bbox ?? null,
        country: place?.countryCode ?? null,
        placeLabel: place?.label ?? null,
      }),
    onSuccess: (response) => {
      setConversationId(response.conversationId)
      setTurns((current) => [
        ...current,
        {
          role: 'assistant',
          text: response.message,
          plan: response.plan,
          planChange: response.planChange,
          conversationId: response.conversationId,
          results: response.results,
          actions: response.suggestedActions,
          clarification: response.clarification,
        },
      ])
    },
    onError: () => {
      // Spec 56.01 s3.8: an AI failure degrades gracefully and still points the
      // explorer somewhere useful.
      setTurns((current) => [
        ...current,
        {
          role: 'assistant',
          text: 'I could not reach the concierge just then. You can still browse discovery and search while I recover.',
        },
      ])
    },
  })

  function submit(message: string) {
    const text = message.trim()
    if (!text || send.isPending) return
    setTurns((current) => [...current, { role: 'user', text }])
    setInput('')
    send.mutate(text)
  }

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' })
  }, [turns, send.isPending])

  useEffect(() => {
    if (open) inputRef.current?.focus()
  }, [open])

  // Escape closes, matching the dialog convention users already expect.
  useEffect(() => {
    if (!open) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') toggle(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, toggle])

  if (!open) return null

  return (
    <div className="fixed inset-0 z-50 flex justify-end" role="dialog" aria-modal="true" aria-label="Mado concierge">
      <button
        type="button"
        className="absolute inset-0 bg-sand-950/25 backdrop-blur-[2px]"
        onClick={() => toggle(false)}
        aria-label="Close concierge"
      />

      <div className="relative flex h-full w-full max-w-md flex-col bg-white shadow-lifted">
        <header className="flex items-center justify-between border-b border-sand-200 px-4 py-3">
          <div className="flex items-center gap-2">
            <span className="grid size-8 place-items-center rounded-full bg-[var(--color-ai-soft)]">
              <Sparkles className="size-4 text-[var(--color-ai)]" aria-hidden />
            </span>
            <div>
              <p className="text-sm font-semibold text-sand-900">Mado concierge</p>
              <p className="text-xs text-sand-500">AI assistant · answers from live listings</p>
            </div>
          </div>
          <button
            type="button"
            onClick={() => toggle(false)}
            className="grid size-8 place-items-center rounded-full text-sand-500 hover:bg-sand-100"
            aria-label="Close"
          >
            <X className="size-4" aria-hidden />
          </button>
        </header>

        <div ref={scrollRef} className="flex-1 space-y-4 overflow-y-auto px-4 py-4">
          {turns.length === 0 && (
            <div className="space-y-4">
              <p className="text-sm text-sand-600">
                Ask me what is happening, and I will look through what is actually on.
              </p>

              <ProactiveSuggestionCard />
              <div className="flex flex-wrap gap-2">
                {OPENERS.map((opener) => (
                  <button
                    key={opener.label}
                    type="button"
                    onClick={() => submit(opener.message)}
                    className="rounded-pill border border-sand-300 px-3 py-1.5 text-sm text-sand-700 transition-colors hover:border-brand-400 hover:bg-brand-50"
                  >
                    {opener.label}
                  </button>
                ))}
              </div>
            </div>
          )}

          {turns.map((turn, index) => (
            <div key={index} className={cn(turn.role === 'user' && 'flex justify-end')}>
              {turn.role === 'user' ? (
                <p className="max-w-[85%] rounded-2xl rounded-br-sm bg-brand-700 px-3.5 py-2 text-sm text-white">
                  {turn.text}
                </p>
              ) : (
                <div className="space-y-3">
                  <p className="whitespace-pre-line text-sm leading-relaxed text-sand-800">
                    {turn.text}
                  </p>

                  {/* What changed, above the plan. An explorer comparing two
                      lists of four stops will not spot that the third moved. */}
                  {turn.planChange && (
                    <p className="mt-2 rounded-lg bg-sand-100 px-3 py-2 text-xs text-sand-700">
                      {turn.planChange}
                    </p>
                  )}

                  {turn.plan && turn.plan.stops.length > 0 && turn.conversationId && (
                    <OfferedPlanCard
                      plan={turn.plan}
                      conversationId={turn.conversationId}
                      onClose={() => toggle(false)}
                    />
                  )}

                  {/* Cards only when there is no plan. Showing both repeats the
                      same places twice in two shapes. */}
                  {!turn.plan && turn.results && turn.results.length > 0 && (
                    <ul className="space-y-2">
                      {turn.results.map((result) => (
                        <li key={result.id}>
                          <Link
                            to={`/experiences/${result.id}`}
                            onClick={() => toggle(false)}
                            className="block rounded-lg border border-sand-200 bg-sand-50 p-3 transition-colors hover:border-brand-300 hover:bg-brand-50"
                          >
                            <p className="text-sm font-medium text-sand-900">{result.title}</p>
                            <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-xs text-sand-500">
                              {result.venueName && (
                                <span className="flex items-center gap-1">
                                  <MapPin className="size-3" aria-hidden />
                                  {result.venueName}
                                </span>
                              )}
                              {result.when && <span>· {result.when}</span>}
                              {result.price && <span>· {result.price}</span>}
                            </div>
                            {result.publisherType === 'organization' &&
                              result.publisherName && (
                                <p className="mt-1 flex items-center gap-1.5 text-xs text-sand-600">
                                  <span className="line-clamp-1">{result.publisherName}</span>
                                  <span className="shrink-0 rounded-full bg-sand-200 px-1.5 py-0.5 text-[0.625rem] font-medium uppercase tracking-wide text-sand-600">
                                    Business
                                  </span>
                                </p>
                              )}
                            {result.reason && (
                              <p className="mt-1 text-xs text-brand-800">{result.reason}</p>
                            )}
                          </Link>
                          {/* Outside the link, like on a card: this is a
                              button, and nesting one inside an anchor makes the
                              whole row navigate on every tap. */}
                          <RepostButton
                            experience={{
                              id: result.id,
                              repostCount: result.repostCount,
                              isReposted: result.isReposted,
                            }}
                            className="px-1"
                          />
                        </li>
                      ))}
                    </ul>
                  )}

                  {/* A low-confidence turn asks rather than guesses (spec 56.02 s13). */}
                  {turn.clarification && (
                    <p className="rounded-lg bg-accent-100 px-3 py-2 text-sm text-accent-700">
                      {turn.clarification}
                    </p>
                  )}

                  {turn.actions && turn.actions.length > 0 && (
                    <div className="flex flex-wrap gap-2">
                      {turn.actions.map((action) => (
                        <button
                          key={action.label}
                          type="button"
                          onClick={() => submit(action.message)}
                          className="rounded-pill border border-sand-300 px-3 py-1 text-xs text-sand-700 transition-colors hover:border-brand-400 hover:bg-brand-50"
                        >
                          {action.label}
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>
          ))}

          {send.isPending && (
            <div className="flex items-center gap-2 text-sm text-sand-500" aria-live="polite">
              <span className="flex gap-1">
                {[0, 150, 300].map((delay) => (
                  <span
                    key={delay}
                    className="size-1.5 animate-bounce rounded-full bg-sand-400"
                    style={{ animationDelay: `${delay}ms` }}
                  />
                ))}
              </span>
              Looking through what is on…
            </div>
          )}
        </div>

        <form
          onSubmit={(event) => {
            event.preventDefault()
            submit(input)
          }}
          className="border-t border-sand-200 p-3"
        >
          <div className="flex gap-2">
            <Input
              ref={inputRef}
              value={input}
              onChange={(event) => setInput(event.target.value)}
              placeholder="Ask about tonight, this weekend, nearby…"
              aria-label="Message the concierge"
              disabled={send.isPending}
            />
            <Button type="submit" disabled={!input.trim() || send.isPending} aria-label="Send">
              <Send className="size-4" aria-hidden />
            </Button>
          </div>
          {/* Somewhere to look, when there is nowhere. The concierge cannot
              answer anything without a place and now says so - which was a
              sentence with no button next to it, leaving the explorer told what
              was wrong and given no way to fix it. A chosen place counts, so
              this is only offered when there is neither. */}
          {!location.granted && !place && (
            <button
              type="button"
              onClick={() => requestLocation()}
              className="mt-2 text-xs text-brand-700 underline underline-offset-2 hover:text-brand-800"
            >
              Share your location for answers about where you are
            </button>
          )}
        </form>
      </div>
    </div>
  )
}

/** Persistent entry point. Spec 57.04 s5: reachable from every surface. */
export function ConciergeLauncher() {
  const toggle = useAppStore((s) => s.toggleConcierge)
  const open = useAppStore((s) => s.conciergeOpen)

  if (open) return null

  return (
    <button
      type="button"
      onClick={() => toggle(true)}
      className="fixed bottom-6 right-5 z-40 flex items-center gap-2 rounded-pill bg-brand-700 py-3 pl-4 pr-5 text-sm font-medium text-white shadow-lifted transition-transform hover:bg-brand-800 active:scale-95"
    >
      <Sparkles className="size-4" aria-hidden />
      Ask Mado
    </button>
  )
}

export function AiBadge() {
  return (
    <Badge tone="ai" icon={<Sparkles className="size-3" aria-hidden />}>
      AI
    </Badge>
  )
}
