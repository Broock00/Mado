/**
 * Writing help for a draft (spec PUB-005).
 *
 * Suggestions, never edits. Each one is shown next to what the publisher wrote
 * with an explicit "use this" - nothing changes until they press it. Spec AI-26
 * §2: AI enhances human decisions rather than replacing them, and a listing is
 * the publisher's word about their own place.
 *
 * The questions are the most valuable part and are deliberately placed last,
 * where they read as a checklist rather than as criticism. A model is far
 * better at noticing that nobody said what it costs than at guessing the price -
 * and the server will not let it guess.
 *
 * Renders nothing when no model is configured. An assistant that returns
 * politely empty results is worse than an absent one, because the publisher
 * keeps pressing it.
 */

import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { Check, HelpCircle, Sparkles } from 'lucide-react'

import { api } from '@/lib/api'
import type { AssistSuggestions } from '@/lib/types'
import { Button, Card } from '@/design-system/primitives'

function Suggestion({
  label,
  value,
  onUse,
  used,
}: {
  label: string
  value: string
  onUse: () => void
  used: boolean
}) {
  return (
    <div className="border-t border-sand-200 pt-3 first:border-t-0 first:pt-0">
      <p className="text-xs uppercase tracking-wide text-sand-500">{label}</p>
      <p className="mt-1 whitespace-pre-wrap text-sm text-sand-800">{value}</p>
      <Button
        variant={used ? 'ghost' : 'secondary'}
        size="sm"
        className="mt-2"
        disabled={used}
        onClick={onUse}
      >
        {used ? (
          <>
            <Check className="size-4" aria-hidden />
            Used
          </>
        ) : (
          'Use this'
        )}
      </Button>
    </div>
  )
}

export function WritingHelp({
  title,
  description,
  summary,
  onApply,
}: {
  title: string
  description: string
  summary?: string | null
  onApply: (patch: { summary?: string; description?: string; categorySlug?: string }) => void
}) {
  const [used, setUsed] = useState<Record<string, boolean>>({})
  const [unavailable, setUnavailable] = useState(false)

  const ask = useMutation({
    mutationFn: () => api.assistDraft({ title, description, summary: summary ?? undefined }),
    onSuccess: (result: AssistSuggestions) => {
      setUsed({})
      if (!result.available) setUnavailable(true)
    },
  })

  // Hidden entirely once the server says there is no model behind this, rather
  // than left as a button that returns nothing.
  if (unavailable) return null

  const data = ask.data
  const hasSuggestions =
    data && (data.summary || data.description || data.categorySlug || data.missing.length > 0)

  const ready = title.trim().length >= 4 && description.trim().length >= 20

  return (
    <Card className="p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="flex items-center gap-1.5 font-medium text-sand-900">
            <Sparkles className="size-4 text-brand-600" aria-hidden />
            Help me write this
          </p>
          <p className="mt-0.5 text-sm text-sand-600">
            Tidies up what you wrote and points out what a reader would still ask.
            It will not add anything you did not say.
          </p>
        </div>
        <Button
          variant="secondary"
          size="sm"
          disabled={!ready || ask.isPending}
          onClick={() => ask.mutate()}
        >
          {ask.isPending ? 'Reading…' : data ? 'Try again' : 'Have a look'}
        </Button>
      </div>

      {!ready && (
        <p className="mt-3 text-xs text-sand-500">
          Write a title and a sentence or two first.
        </p>
      )}

      {ask.isError && (
        <p className="mt-3 text-sm text-red-700" role="alert">
          {(ask.error as Error).message}
        </p>
      )}

      {data && !hasSuggestions && (
        <p className="mt-3 text-sm text-sand-600">
          Nothing to suggest - this reads well as it is.
        </p>
      )}

      {hasSuggestions && (
        <div className="mt-4 space-y-3">
          {data.summary && (
            <Suggestion
              label="One line"
              value={data.summary}
              used={Boolean(used.summary)}
              onUse={() => {
                onApply({ summary: data.summary! })
                setUsed((u) => ({ ...u, summary: true }))
              }}
            />
          )}

          {data.description && (
            <Suggestion
              label="Description"
              value={data.description}
              used={Boolean(used.description)}
              onUse={() => {
                onApply({ description: data.description! })
                setUsed((u) => ({ ...u, description: true }))
              }}
            />
          )}

          {data.categorySlug && (
            <Suggestion
              label="Category"
              value={data.categorySlug}
              used={Boolean(used.category)}
              onUse={() => {
                onApply({ categorySlug: data.categorySlug! })
                setUsed((u) => ({ ...u, category: true }))
              }}
            />
          )}

          {data.missing.length > 0 && (
            <div className="border-t border-sand-200 pt-3">
              <p className="flex items-center gap-1.5 text-xs uppercase tracking-wide text-sand-500">
                <HelpCircle className="size-3.5" aria-hidden />
                A reader would still ask
              </p>
              <ul className="mt-2 space-y-1">
                {data.missing.map((question) => (
                  <li key={question} className="text-sm text-sand-700">
                    · {question}
                  </li>
                ))}
              </ul>
              {/* Answers go in the description in the publisher's own words.
                  There is nothing to "apply" here on purpose - the platform
                  does not know these answers and must not appear to. */}
              <p className="mt-2 text-xs text-sand-500">
                Add the answers to your description in your own words.
              </p>
            </div>
          )}
        </div>
      )}
    </Card>
  )
}
