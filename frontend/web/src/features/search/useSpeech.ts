/**
 * Speaking a search instead of typing it (spec SRCH-004).
 *
 * **This is the browser's recogniser, not Mado's.** `SpeechRecognition` is
 * built into Chrome and Safari, costs nothing, and needs no upload from us -
 * but in Chrome it does send audio to Google to be transcribed. That is a
 * different promise from the rest of the platform, where a search never leaves
 * our own stack, so the interface says whose ears they are rather than
 * presenting a microphone and letting people assume.
 *
 * **Amharic is the reason this hook is careful.** Mado's pilot city is Addis
 * Ababa and the interface is translated into Amharic, but browser speech
 * recognition does not support `am-ET`. Handing Amharic speech to an
 * English-language recogniser does not fail - it returns confident nonsense,
 * which then gets searched. So the language is checked first and the control is
 * simply absent where it cannot work, with a sentence saying why. An
 * English-only voice feature on an Amharic product is worth naming out loud
 * rather than shipping quietly.
 *
 * **Nothing is auto-submitted.** The transcript lands in the search box for the
 * explorer to read and correct. Speech recognition mishears, and a query that
 * runs before anybody has seen it turns a misheard word into results nobody
 * asked for - with no obvious way to tell what went wrong.
 */

import { useCallback, useEffect, useRef, useState } from 'react'

/** Minimal shape of the vendor-prefixed API, which TypeScript does not ship. */
interface SpeechRecognitionLike extends EventTarget {
  lang: string
  continuous: boolean
  interimResults: boolean
  start: () => void
  stop: () => void
  abort: () => void
  onresult: ((event: { results: ArrayLike<ArrayLike<{ transcript: string }>> }) => void) | null
  onerror: ((event: { error: string }) => void) | null
  onend: (() => void) | null
}

type Constructor = new () => SpeechRecognitionLike

function constructor(): Constructor | null {
  const w = window as unknown as {
    SpeechRecognition?: Constructor
    webkitSpeechRecognition?: Constructor
  }
  return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null
}

/**
 * Languages the browser recogniser can actually handle, of the ones Mado
 * speaks.
 *
 * Amharic is absent because no shipping browser supports it. Listed as a map
 * rather than assumed, so adding a language to Mado does not silently imply
 * that it can be spoken.
 */
const RECOGNISER_LOCALES: Record<string, string | null> = {
  en: 'en-GB',
  am: null,
}

export type SpeechState = 'unsupported' | 'unsupported-language' | 'idle' | 'listening' | 'denied'

export interface Speech {
  state: SpeechState
  /** What was heard, for the explorer to check before searching. */
  transcript: string
  start: () => void
  stop: () => void
}

export function useSpeech(language: string, onTranscript: (text: string) => void): Speech {
  const [state, setState] = useState<SpeechState>('idle')
  const [transcript, setTranscript] = useState('')
  const recognition = useRef<SpeechRecognitionLike | null>(null)
  // Held in a ref so restarting does not need a new recogniser, and so the
  // latest callback is used rather than the one captured when listening began.
  const handler = useRef(onTranscript)
  handler.current = onTranscript

  const locale = RECOGNISER_LOCALES[language] ?? null

  useEffect(() => {
    const Ctor = constructor()
    if (!Ctor) {
      setState('unsupported')
      return
    }
    if (!locale) {
      setState('unsupported-language')
      return
    }

    const instance = new Ctor()
    instance.lang = locale
    // One utterance, final results only. Continuous dictation is a different
    // feature and interim results make the box flicker with words that are
    // about to be revised.
    instance.continuous = false
    instance.interimResults = false

    instance.onresult = (event) => {
      const heard = event.results?.[0]?.[0]?.transcript ?? ''
      setTranscript(heard)
      handler.current(heard)
    }
    instance.onerror = (event) => {
      // `not-allowed` is the explorer declining the microphone, which is a
      // choice rather than a fault and should not be reported as an error.
      setState(event.error === 'not-allowed' ? 'denied' : 'idle')
    }
    instance.onend = () => setState((current) => (current === 'listening' ? 'idle' : current))

    recognition.current = instance
    setState('idle')

    return () => {
      instance.onresult = null
      instance.onerror = null
      instance.onend = null
      // `abort` rather than `stop`: stop delivers whatever it heard, and a
      // component going away should not fire a search on its way out.
      instance.abort()
      recognition.current = null
    }
  }, [locale])

  const start = useCallback(() => {
    if (!recognition.current) return
    setTranscript('')
    try {
      recognition.current.start()
      setState('listening')
    } catch {
      // Already running. Harmless, and throwing here would break the button.
    }
  }, [])

  const stop = useCallback(() => {
    recognition.current?.stop()
    setState('idle')
  }, [])

  return { state, transcript, start, stop }
}
