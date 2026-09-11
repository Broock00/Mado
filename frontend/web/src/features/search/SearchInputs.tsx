/**
 * Speaking and photographing a search (spec SRCH-004, SRCH-005).
 *
 * Both are input methods for the search that already exists, not new kinds of
 * search - which is why they live inside the search field and hand their result
 * to the same `runSearch`. Anything the text path gains, these gain.
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

const iconActionClass =
  'grid size-9 place-items-center rounded-full text-black transition-colors hover:bg-black/5 disabled:opacity-60'

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
      <span className="px-1 text-[11px] leading-tight text-sand-400" title={t('search.voice.unsupportedLanguage')}>
        {t('search.voice.unsupportedLanguage')}
      </span>
    )
  }

  const listening = state === 'listening'

  return (
    <div className="relative">
      <button
        type="button"
        onClick={listening ? stop : start}
        aria-pressed={listening}
        aria-label={listening ? t('search.voice.stop') : t('search.voice.start')}
        className={cn(
          iconActionClass,
          listening && 'bg-red-50 text-red-600 hover:bg-red-100 hover:text-red-700',
        )}
      >
        {state === 'denied' ? (
          <MicOff className="size-4 text-black" aria-hidden />
        ) : (
          <Mic className={cn('size-4 text-black', listening && 'animate-pulse text-red-600')} aria-hidden />
        )}
      </button>
      {state === 'denied' && (
        <p className="absolute right-0 top-full z-10 mt-1 w-max max-w-44 rounded-md bg-sand-100 px-2 py-1 text-xs text-sand-600 shadow-sm">
          {t('search.voice.denied')}
        </p>
      )}
      {listening && (
        <p
          className="absolute right-0 top-full z-10 mt-1 w-max rounded-md bg-sand-100 px-2 py-1 text-xs text-sand-600 shadow-sm"
          role="status"
        >
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
    <div className="relative">
      <button
        type="button"
        onClick={() => input.current?.click()}
        disabled={search.isPending}
        aria-label={t('search.visual.button')}
        className={iconActionClass}
      >
        {search.isPending ? (
          <Loader2 className="size-4 animate-spin text-black" aria-hidden />
        ) : (
          <Camera className="size-4 text-black" aria-hidden />
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
        <p className="absolute right-0 top-full z-10 mt-1 w-max max-w-44 rounded-md bg-sand-100 px-2 py-1 text-xs text-red-600 shadow-sm" role="alert">
          {t('search.visual.tooBig')}
        </p>
      )}
      {search.isError && (
        <p className="absolute right-0 top-full z-10 mt-1 w-max max-w-52 rounded-md bg-sand-100 px-2 py-1 text-xs text-red-600 shadow-sm" role="alert">
          {(search.error as Error).message}
        </p>
      )}
    </div>
  )
}
