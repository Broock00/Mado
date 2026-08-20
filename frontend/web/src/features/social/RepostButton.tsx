/**
 * Reposting a listing.
 *
 * The count and the caller's own state both arrive on the experience payload, so
 * this renders correctly on first paint with no request of its own. Pressing it
 * updates optimistically and settles on what the server returns — which is why
 * the API sends back both the new state and the new count rather than a bare
 * 204: a double tap otherwise leaves the button drawn one way and counted the
 * other.
 *
 * Signed out, the button is visible and sends you to sign in. Hiding it would
 * make the feature invisible to exactly the people who have not signed up yet.
 *
 * This was an InteractionBar with like and comment beside it. Both were removed:
 * reviews already carry a rating and a written opinion, and a like plus a
 * comment is a weaker copy of both.
 */

import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Repeat2 } from 'lucide-react'

import { ApiError, api } from '@/lib/api'
import { useAppStore } from '@/app/store'
import type { ExperienceSummary } from '@/lib/types'
import { cn } from '@/lib/utils'

/** A count reads better absent than as a zero under every quiet listing. */
function label(value: number): string {
  if (value <= 0) return ''
  if (value < 1000) return String(value)
  return `${(value / 1000).toFixed(value < 10_000 ? 1 : 0)}k`
}

export function RepostButton({
  experience,
  className,
}: {
  experience: Pick<ExperienceSummary, 'id' | 'repostCount' | 'isReposted'>
  className?: string
}) {
  const user = useAppStore((s) => s.user)
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  const [reposted, setReposted] = useState(experience.isReposted)
  const [count, setCount] = useState(experience.repostCount)
  const [error, setError] = useState<string | null>(null)

  const repost = useMutation({
    mutationFn: () => api.toggleRepost(experience.id),
    onMutate: () => {
      // Optimistic: it should feel instant. The server's answer replaces this,
      // so a rejected one corrects itself rather than lying.
      setReposted((was) => !was)
      setCount((n) => n + (reposted ? -1 : 1))
      setError(null)
    },
    onSuccess: (result) => {
      setReposted(result.active)
      setCount(result.count)
      void queryClient.invalidateQueries({ queryKey: ['canvas'] })
      void queryClient.invalidateQueries({ queryKey: ['experience', experience.id] })
    },
    onError: (caught) => {
      setReposted(experience.isReposted)
      setCount(experience.repostCount)
      setError(caught instanceof ApiError ? caught.message : 'That did not work.')
    },
  })

  return (
    <div className={cn('flex flex-col gap-1', className)}>
      <button
        type="button"
        onClick={() => (user ? repost.mutate() : navigate('/signin'))}
        aria-pressed={reposted}
        aria-label={reposted ? 'Undo repost' : 'Repost'}
        className={cn(
          'flex w-fit items-center gap-1.5 rounded-lg px-2 py-1.5 text-sm transition-colors',
          reposted ? 'text-brand-700 hover:bg-brand-50' : 'text-sand-600 hover:bg-sand-100',
        )}
      >
        <Repeat2 className="size-4" aria-hidden />
        {label(count)}
      </button>

      {error && <p className="px-2 text-xs text-red-700">{error}</p>}
    </div>
  )
}
