/**
 * Speaking and photographing a search (spec SRCH-004, SRCH-005).
 *
 * Both are input methods for the search that already exists, not new kinds of
 * search - which is why they live beside the text box and hand their result to
 * the same `runSearch`. Anything the text path gains, these gain.
 *
 * Both are behind flags (`search.voice`, `search.visual`) and both simply do
 * not render when switched off, rather than appearing and refusing. A control
 * that is present and then apologises has wasted the press.
 */

import { useRef, useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { Camera, Loader2, Mic, MicOff } from 'lucide-react'

import { api } from '@/lib/api'
import { useFlag } from '@/app/hooks'
import { useLanguage } from '@/app/language-context'
import type { VisualSearchResult } from '@/lib/types'
import { cn } from '@/lib/utils'
import { useSpeech } from './useSpeech'

/** Photographs bigger than this are refused before upload rather than after. */
const MAX_BYTES = 12 * 1024 * 1024

export function VoiceSearchButton({ onHeard }: { onHeard: (text: string) => void }) {
  const enabled = useFlag('search.voice')
  const { language, t } = useLanguage()
  const { state, start, stop } = useSpeech(language, onHeard)

  if (!enabled) return null

  // No recogniser at all - Firefox, most in-app browsers. Nothing to say about
  // it: the text box is right there and works.
  if (state === 'unsupported') return null

  // The recogniser exists but not for this language. Worth saying rather than
  // hiding, because the explorer can act on it by switching language, and
  // because an Amharic reader silently getting fewer features than an English
  // one should not be invisible.
  if (state === 'unsupported-language') {
    return (
      <p className="mt-2 text-xs text-sand-500">{t('search.voice.unsupportedLanguage')}</p>
    )
  }

  const listening = state === 'listening'

  return (
    <div>
      <button
        type="button"
        onClick={listening ? stop : start}
        aria-pressed={listening}
        aria-label={listening ? t('search.voice.stop') : t('search.voice.start')}
        className={cn(
          'grid size-9 place-items-center rounded-lg border transition-colors',
          listening
            ? 'border-red-500/60 bg-red-950 text-red-300'
            : 'border-sand-300 bg-sand-100 text-sand-600 hover:bg-sand-200',
        )}
      >
        {state === 'denied' ? (
          <MicOff className="size-4" aria-hidden />
        ) : (
          <Mic className={cn('size-4', listening && 'animate-pulse')} aria-hidden />
        )}
      </button>
      {state === 'denied' && (
        <p className="mt-2 text-xs text-sand-500">{t('search.voice.denied')}</p>
      )}
      {listening && (
        <p className="mt-2 text-xs text-sand-500" role="status">
          {t('search.voice.listening')}
        </p>
      )}
    </div>
  )
}

export function VisualSearchButton({
  onResult,
}: {
  onResult: (result: VisualSearchResult) => void
}) {
  const enabled = useFlag('search.visual')
  const { t } = useLanguage()
  const input = useRef<HTMLInputElement>(null)
  const [tooBig, setTooBig] = useState(false)

  const search = useMutation({
    mutationFn: (file: File) => api.visualSearch(file),
    onSuccess: onResult,
  })

  if (!enabled) return null

  return (
    <>
      <button
        type="button"
        onClick={() => input.current?.click()}
        disabled={search.isPending}
        aria-label={t('search.visual.button')}
        className="grid size-9 place-items-center rounded-lg border border-sand-300 bg-sand-100 text-sand-600 transition-colors hover:bg-sand-200 disabled:opacity-60"
      >
        {search.isPending ? (
          <Loader2 className="size-4 animate-spin" aria-hidden />
        ) : (
          <Camera className="size-4" aria-hidden />
        )}
      </button>
      <input
        ref={input}
        type="file"
        accept="image/*"
        // `capture` opens the camera directly on a phone, which is the whole
        // point of the feature, and is ignored on a desktop where it becomes
        // an ordinary file picker.
        capture="environment"
        className="sr-only"
        onChange={(event) => {
          const file = event.target.files?.[0]
          // Reset immediately so choosing the same photograph twice fires
          // again - otherwise a retry after a failure does nothing.
          event.target.value = ''
          if (!file) return
          if (file.size > MAX_BYTES) {
            setTooBig(true)
            return
          }
          setTooBig(false)
          search.mutate(file)
        }}
      />
      {tooBig && (
        <p className="mt-2 text-xs text-red-300" role="alert">
          {t('search.visual.tooBig')}
        </p>
      )}
      {search.isError && (
        <p className="mt-2 text-xs text-red-300" role="alert">
          {(search.error as Error).message}
        </p>
      )}
    </>
  )
}
