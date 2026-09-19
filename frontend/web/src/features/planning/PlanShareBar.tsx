/**
 * Who can open this plan by link — same pattern as collection ShareBar.
 *
 * Unlisted is what "share" usually means: send a friend the URL. Private
 * answers 404 to strangers so guessing an id does not confirm the plan exists.
 */

import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Check, Globe, Link2, Lock } from 'lucide-react'

import { api } from '@/lib/api'
import type { Itinerary, PlanVisibility } from '@/lib/types'
import { Button, Card, Input } from '@/design-system/primitives'

const OPTIONS: {
  value: PlanVisibility
  label: string
  description: string
  icon: typeof Lock
}[] = [
  {
    value: 'private',
    label: 'Only you',
    description: 'Nobody else can open it.',
    icon: Lock,
  },
  {
    value: 'unlisted',
    label: 'Anyone with the link',
    description: 'Not listed anywhere. Share the link and it works.',
    icon: Link2,
  },
  {
    value: 'public',
    label: 'Public link',
    description: 'Anyone with the link can open it. Same as unlisted for now.',
    icon: Globe,
  },
]

export function PlanShareBar({ itinerary }: { itinerary: Itinerary }) {
  const queryClient = useQueryClient()
  const [copied, setCopied] = useState(false)
  const visibility = itinerary.visibility ?? 'private'

  const setVisibility = useMutation({
    mutationFn: (next: PlanVisibility) => api.setItineraryVisibility(itinerary.id, next),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['itinerary', itinerary.id] })
      void queryClient.invalidateQueries({ queryKey: ['itineraries'] })
    },
  })

  const shareUrl = `${window.location.origin}/plans/${itinerary.id}`

  return (
    <Card className="mt-5 p-5">
      <p className="font-medium text-sand-900">Who can see this</p>
      <div className="mt-3 space-y-2">
        {OPTIONS.map((option) => {
          const Icon = option.icon
          const active = visibility === option.value
          return (
            <label
              key={option.value}
              className="flex cursor-pointer items-start gap-3 rounded-lg px-2 py-2 hover:bg-sand-100"
            >
              <input
                type="radio"
                name="plan-visibility"
                checked={active}
                disabled={setVisibility.isPending}
                onChange={() => setVisibility.mutate(option.value)}
                className="mt-1 accent-brand-600"
              />
              <span className="min-w-0">
                <span className="flex items-center gap-1.5 font-medium text-sand-900">
                  <Icon className="size-4" aria-hidden />
                  {option.label}
                </span>
                <span className="block text-sm text-sand-600">{option.description}</span>
              </span>
            </label>
          )
        })}
      </div>

      {setVisibility.isError && (
        <p className="mt-2 text-sm text-red-600" role="alert">
          {(setVisibility.error as Error).message}
        </p>
      )}

      {visibility !== 'private' && (
        <div className="mt-4 flex flex-wrap items-center gap-2">
          <Input readOnly value={shareUrl} aria-label="Share link" className="min-w-[16rem] flex-1" />
          <Button
            variant="secondary"
            onClick={() => {
              void navigator.clipboard.writeText(shareUrl)
              setCopied(true)
              window.setTimeout(() => setCopied(false), 2000)
            }}
          >
            {copied ? <Check className="size-4" aria-hidden /> : null}
            {copied ? 'Copied' : 'Copy link'}
          </Button>
        </div>
      )}
    </Card>
  )
}
