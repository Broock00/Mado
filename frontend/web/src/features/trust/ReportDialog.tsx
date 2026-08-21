/**
 * Report content (spec BUSINESS-07).
 *
 * Open publishing needs an easy way for readers to flag what screening missed —
 * the person who turned up and found nothing there is the fastest detector the
 * platform has.
 *
 * The copy is deliberate: it tells the reporter what will and will not happen, so
 * nobody expects an instant takedown, and nobody assumes reporting is pointless.
 */

import { useEffect, useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { Flag, X } from 'lucide-react'
import { ApiError, api } from '@/lib/api'
import { Button, Card } from '@/design-system/primitives'
import { cn } from '@/lib/utils'
import type { ReportReason } from '@/lib/types'

const REASONS: { value: ReportReason; label: string; hint: string }[] = [
  { value: 'inaccurate', label: 'Wrong information', hint: 'Closed, moved, or details are wrong' },
  { value: 'spam', label: 'Spam', hint: 'Advertising or repeated posting' },
  { value: 'scam', label: 'Scam', hint: 'Trying to take money or personal details' },
  { value: 'inappropriate', label: 'Inappropriate', hint: 'Offensive or unsafe content' },
  { value: 'duplicate', label: 'Duplicate', hint: 'Already listed somewhere else' },
  { value: 'other', label: 'Something else', hint: '' },
]

export function ReportDialog({
  experienceId,
  experienceTitle,
  onClose,
}: {
  experienceId: string
  experienceTitle: string
  onClose: () => void
}) {
  const [reason, setReason] = useState<ReportReason | null>(null)
  const [detail, setDetail] = useState('')
  const [error, setError] = useState<string | null>(null)

  const submit = useMutation({
    mutationFn: () => api.reportExperience(experienceId, reason!, detail || undefined),
    onError: (err) => {
      setError(
        err instanceof ApiError
          ? err.code === 'ALREADY_REPORTED'
            ? 'You have already reported this.'
            : err.message
          : 'Could not send that report.',
      )
    },
  })

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div className="fixed inset-0 z-50 grid place-items-center p-4" role="dialog" aria-modal="true">
      <button
        type="button"
        className="absolute inset-0 bg-sand-950/30 backdrop-blur-[2px]"
        onClick={onClose}
        aria-label="Close"
      />

      <Card className="relative w-full max-w-md p-5">
        <div className="mb-4 flex items-start justify-between gap-4">
          <div>
            <h2 className="flex items-center gap-2 text-base font-semibold text-sand-900">
              <Flag className="size-4" aria-hidden />
              Report this
            </h2>
            <p className="mt-1 line-clamp-1 text-sm text-sand-500">{experienceTitle}</p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="grid size-8 place-items-center rounded-full text-sand-500 hover:bg-sand-200"
            aria-label="Close"
          >
            <X className="size-4" aria-hidden />
          </button>
        </div>

        {submit.isSuccess ? (
          <div className="py-4 text-center">
            <p className="text-sm font-medium text-sand-900">Thank you — that helps.</p>
            <p className="mt-1.5 text-sm text-sand-500">
              A moderator will look at this. Reports do not delete anything on their own.
            </p>
            <Button className="mt-5 w-full" onClick={onClose}>
              Done
            </Button>
          </div>
        ) : (
          <>
            <fieldset>
              <legend className="sr-only">Reason</legend>
              <div className="space-y-1.5">
                {REASONS.map((option) => (
                  <button
                    key={option.value}
                    type="button"
                    onClick={() => setReason(option.value)}
                    aria-pressed={reason === option.value}
                    className={cn(
                      'w-full rounded-lg border px-3 py-2 text-left transition-colors',
                      reason === option.value
                        ? 'border-brand-500 bg-brand-900/40'
                        : 'border-sand-300 hover:bg-sand-200',
                    )}
                  >
                    <span className="block text-sm font-medium text-sand-900">{option.label}</span>
                    {option.hint && (
                      <span className="block text-xs text-sand-500">{option.hint}</span>
                    )}
                  </button>
                ))}
              </div>
            </fieldset>

            <textarea
              value={detail}
              onChange={(e) => setDetail(e.target.value)}
              rows={3}
              maxLength={1000}
              placeholder="Anything else worth knowing (optional)"
              aria-label="Extra detail"
              className="mt-3 w-full rounded-lg border border-sand-300 bg-sand-100 px-3 py-2 text-sm placeholder:text-sand-400 focus:border-brand-600 focus:outline-none focus:ring-2 focus:ring-brand-600/20"
            />

            {error && (
              <p role="alert" className="mt-3 rounded-lg bg-red-950 px-3 py-2 text-sm text-red-300">
                {error}
              </p>
            )}

            <p className="mt-3 text-xs text-sand-500">
              Reports are reviewed by a person. Nothing is deleted automatically.
            </p>

            <div className="mt-4 flex gap-2">
              <Button variant="secondary" className="flex-1" onClick={onClose}>
                Cancel
              </Button>
              <Button
                className="flex-1"
                onClick={() => submit.mutate()}
                loading={submit.isPending}
                disabled={!reason}
              >
                Send report
              </Button>
            </div>
          </>
        )}
      </Card>
    </div>
  )
}
