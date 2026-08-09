/**
 * The language context and the hook that reads it.
 *
 * Split from the provider so that file exports a component and nothing else -
 * React Fast Refresh can only replace a module whose exports are all
 * components, and a file mixing a provider with a hook silently degrades to a
 * full reload on every edit.
 */

import { createContext, useContext } from 'react'

import type { Language } from '@/lib/i18n'

export interface LanguageValue {
  language: Language
  setLanguage: (next: Language) => void
  /** True while a change is being written to the profile. */
  saving: boolean
  /** Look up a message. */
  t: (key: string, params?: Record<string, string | number>) => string
  /** Look up a counted message, choosing the plural form for this language. */
  n: (key: string, count: number, params?: Record<string, string | number>) => string
  date: (value: Date | string, options?: Intl.DateTimeFormatOptions) => string
  time: (value: Date | string) => string
  relative: (value: Date | string) => string
  number: (value: number) => string
  money: (amount: number, currency: string) => string
  list: (items: string[]) => string
  locale: string
}

export const LanguageContext = createContext<LanguageValue | null>(null)

export function useLanguage(): LanguageValue {
  const value = useContext(LanguageContext)
  if (!value) throw new Error('useLanguage must be used inside LanguageProvider')
  return value
}
