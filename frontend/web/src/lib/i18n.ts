/**
 * Interface localisation (spec 11.07 "Language Accessibility", 18 §15).
 *
 * English and Amharic, because Mado's pilot city is Addis Ababa. A third
 * language is a new catalogue file and one entry in `LANGUAGES` - nothing here
 * assumes there are two.
 *
 * **Written here rather than pulled in.** i18next and react-intl are both good
 * and both bring a plugin architecture, a loader, a backend abstraction and a
 * bundle larger than this whole module for the two features actually needed:
 * lookup with interpolation, and plurals. Plurals come from `Intl.PluralRules`,
 * which the platform already ships and which knows the rules for every locale
 * far better than a hand-written table would. The formatting below is
 * `Intl.DateTimeFormat`, `NumberFormat`, `RelativeTimeFormat` and `ListFormat`,
 * for the same reason. Reach for a library the day this needs message
 * extraction tooling or ICU select ordinals.
 *
 * **Amharic is not English with different words.** Two things this file is
 * built around:
 *
 * - *Zero is singular.* `Intl.PluralRules('am').select(0)` is `one`, not
 *   `other` - so "0 places left" takes the singular form. An English speaker
 *   writing a two-branch `n === 1 ? … : …` gets this wrong every time, which is
 *   why plural selection is never done by hand here.
 * - *The verb goes last.* Amharic is subject-object-verb, so a message built by
 *   concatenating clauses in English order reads as broken Amharic. Every
 *   message is therefore a whole sentence with placeholders, never a fragment
 *   that callers glue together.
 *
 * **A missing key falls back to English and says so in development.** Showing
 * `nav.discover` to an explorer has failed at being an interface; showing the
 * English is merely untranslated. The console warning is what stops the gap
 * being invisible to whoever added the key.
 */

import { en } from './messages/en'
import { am } from './messages/am'

export type Language = 'en' | 'am'

export const DEFAULT_LANGUAGE: Language = 'en'

/** Each name in its own language - see the note in `app/core/language.py`. */
export const LANGUAGES: { code: Language; name: string }[] = [
  { code: 'en', name: 'English' },
  { code: 'am', name: 'አማርኛ' },
]

/**
 * The BCP 47 tag used for formatting, which is not the same as the message key.
 *
 * `am` alone formats dates and numbers with no region, so `am-ET` is used to
 * get Ethiopian conventions. English is `en-GB` rather than `en-US`: this is a
 * platform for Addis Ababa, where the day comes before the month.
 */
const LOCALES: Record<Language, string> = {
  en: 'en-GB',
  am: 'am-ET',
}

export type Messages = Record<string, string>

const CATALOGUES: Record<Language, Messages> = { en, am }

const missing = new Set<string>()

/**
 * Look a message up and fill in its placeholders.
 *
 * Placeholders are `{name}`. Anything left unfilled stays visible rather than
 * being blanked - a sentence with `{count}` in it is obviously broken, and a
 * sentence with a hole in it silently reads as finished.
 */
export function translate(
  language: Language,
  key: string,
  params: Record<string, string | number> = {},
): string {
  const template = CATALOGUES[language]?.[key] ?? CATALOGUES[DEFAULT_LANGUAGE][key]

  if (template === undefined) {
    if (import.meta.env.DEV && !missing.has(key)) {
      missing.add(key)
      console.warn(`[i18n] no message for "${key}"`)
    }
    return key
  }

  if (import.meta.env.DEV && CATALOGUES[language]?.[key] === undefined) {
    const gap = `${language}:${key}`
    if (!missing.has(gap)) {
      missing.add(gap)
      console.warn(`[i18n] "${key}" is not translated into ${language}; showing English`)
    }
  }

  return template.replace(/\{(\w+)\}/g, (whole, name: string) =>
    name in params ? String(params[name]) : whole,
  )
}

/**
 * Pick the right plural form, then fill it in.
 *
 * Keys are suffixed by CLDR category - `places.one`, `places.other` - and the
 * category comes from `Intl.PluralRules`, never from comparing to 1. Amharic
 * puts zero in `one`; Welsh has six categories; guessing is how a product ships
 * "1 ቦታዎች".
 */
export function plural(
  language: Language,
  key: string,
  count: number,
  params: Record<string, string | number> = {},
): string {
  const category = new Intl.PluralRules(LOCALES[language]).select(count)
  const withCount = { count: formatNumber(language, count), ...params }

  const catalogue = CATALOGUES[language] ?? CATALOGUES[DEFAULT_LANGUAGE]
  const chosen = `${key}.${category}` in catalogue ? `${key}.${category}` : `${key}.other`
  return translate(language, chosen, withCount)
}

/* ------------------------------------------------------------- formatting */

export function locale(language: Language): string {
  return LOCALES[language]
}

export function formatNumber(language: Language, value: number): string {
  return new Intl.NumberFormat(LOCALES[language]).format(value)
}

/**
 * Money, in the currency it is actually priced in.
 *
 * Amharic renders ETB as ብር, which is what a price tag in Addis says. The
 * currency code is never assumed: an experience carries its own, and defaulting
 * to ETB would quietly relabel a price the publisher set in dollars.
 */
export function formatMoney(language: Language, amount: number, currency: string): string {
  try {
    return new Intl.NumberFormat(LOCALES[language], {
      style: 'currency',
      currency,
      // Whole birr. Nothing in this catalogue is priced in cents, and "250.00
      // ብር" reads as a bill rather than as a ticket price.
      maximumFractionDigits: 0,
    }).format(amount)
  } catch {
    // An unknown currency code should not blank a price.
    return `${formatNumber(language, amount)} ${currency}`
  }
}

export function formatDate(
  language: Language,
  value: Date | string,
  options: Intl.DateTimeFormatOptions = { day: 'numeric', month: 'long', year: 'numeric' },
): string {
  const date = typeof value === 'string' ? new Date(value) : value
  return new Intl.DateTimeFormat(LOCALES[language], options).format(date)
}

export function formatTime(language: Language, value: Date | string): string {
  return formatDate(language, value, { hour: '2-digit', minute: '2-digit' })
}

/**
 * "in 2 hours", "yesterday" - in the reader's language.
 *
 * Chosen over a bare timestamp for anything recent because it is what somebody
 * actually wants to know, and hand-rolling it produces "1 days ago" in English
 * and worse elsewhere.
 */
export function formatRelative(language: Language, value: Date | string): string {
  const date = typeof value === 'string' ? new Date(value) : value
  const seconds = (date.getTime() - Date.now()) / 1000
  const formatter = new Intl.RelativeTimeFormat(LOCALES[language], { numeric: 'auto' })

  const steps: [Intl.RelativeTimeFormatUnit, number][] = [
    ['year', 31_536_000],
    ['month', 2_592_000],
    ['week', 604_800],
    ['day', 86_400],
    ['hour', 3_600],
    ['minute', 60],
  ]
  for (const [unit, size] of steps) {
    if (Math.abs(seconds) >= size) {
      return formatter.format(Math.round(seconds / size), unit)
    }
  }
  return formatter.format(Math.round(seconds), 'second')
}

/** "coffee, music and markets" - with the right conjunction and comma. */
export function formatList(language: Language, items: string[]): string {
  return new Intl.ListFormat(LOCALES[language], { style: 'long', type: 'conjunction' }).format(
    items,
  )
}

/**
 * Which keys are missing from a catalogue.
 *
 * Exported so a test can assert the gap rather than leaving it to be discovered
 * by an explorer reading half a page in English. Translation coverage that
 * nothing measures only ever goes down.
 */
export function untranslated(language: Language): string[] {
  const reference = Object.keys(CATALOGUES[DEFAULT_LANGUAGE])
  const target = CATALOGUES[language] ?? {}
  return reference.filter((key) => !(key in target))
}

export function catalogue(language: Language): Messages {
  return CATALOGUES[language]
}
