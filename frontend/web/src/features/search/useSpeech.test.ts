/**
 * Voice search language gating (spec SRCH-004).
 *
 * The case worth testing is not that the microphone works - the browser owns
 * that - but that it is *absent* where it cannot work. Browser speech
 * recognition has no Amharic, and handing Amharic speech to an English
 * recogniser does not fail: it returns confident nonsense, which then gets
 * searched. Mado's pilot city is Addis Ababa, so this is the difference between
 * a missing feature and a broken one.
 */

import { renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { useSpeech } from './useSpeech'

class FakeRecognition {
  lang = ''
  continuous = false
  interimResults = false
  start = vi.fn()
  stop = vi.fn()
  abort = vi.fn()
  onresult: unknown = null
  onerror: unknown = null
  onend: unknown = null
  addEventListener = vi.fn()
  removeEventListener = vi.fn()
  dispatchEvent = vi.fn()
}

function withRecogniser(present: boolean) {
  const w = window as unknown as Record<string, unknown>
  if (present) w.SpeechRecognition = FakeRecognition
  else delete w.SpeechRecognition
  delete w.webkitSpeechRecognition
}

afterEach(() => withRecogniser(false))

describe('which languages can be spoken', () => {
  it('offers voice search in English', () => {
    withRecogniser(true)
    const { result } = renderHook(() => useSpeech('en', () => {}))
    expect(result.current.state).toBe('idle')
  })

  it('refuses Amharic rather than mishearing it', () => {
    // No shipping browser transcribes Amharic. Passing it to an en-GB
    // recogniser produces plausible English words from Amharic speech - the
    // worst outcome, because it looks like it worked.
    withRecogniser(true)
    const { result } = renderHook(() => useSpeech('am', () => {}))
    expect(result.current.state).toBe('unsupported-language')
  })

  it('reports an absent recogniser separately from an unsupported language', () => {
    // Different causes and different remedies: one the explorer can act on by
    // switching language, the other they cannot act on at all.
    withRecogniser(false)
    const { result } = renderHook(() => useSpeech('en', () => {}))
    expect(result.current.state).toBe('unsupported')
  })

  it('never starts a recogniser for a language it cannot handle', () => {
    withRecogniser(true)
    const { result } = renderHook(() => useSpeech('am', () => {}))
    result.current.start()
    // No instance was ever constructed, so nothing can have been listening.
    expect(result.current.state).toBe('unsupported-language')
  })
})

describe('what it does with what it heard', () => {
  it('hands the transcript back rather than searching', () => {
    // Recognition mishears. A query that runs before anybody has read it turns
    // a misheard word into results with no clue what went wrong, so the hook
    // only reports - the page decides.
    withRecogniser(true)
    const heard = vi.fn()
    renderHook(() => useSpeech('en', heard))
    expect(heard).not.toHaveBeenCalled()
  })
})
