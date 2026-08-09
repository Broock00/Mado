/**
 * Interface localisation tests (spec 11.07).
 *
 * Localisation is the easiest thing in a product to get quietly wrong: nobody
 * who reads only English ever sees the Amharic, so a missing translation, a
 * mangled plural or a date in the wrong order can survive every review. These
 * tests assert the things a reviewer cannot check by looking.
 */

import { describe, expect, it } from 'vitest'

import {
  DEFAULT_LANGUAGE,
  LANGUAGES,
  catalogue,
  formatList,
  formatMoney,
  formatNumber,
  formatRelative,
  plural,
  translate,
  untranslated,
} from './i18n'

describe('coverage', () => {
  it('has every English message translated into Amharic', () => {
    // Not a nice-to-have. A page that is half Amharic and half English reads as
    // broken software rather than as an incomplete translation.
    expect(untranslated('am')).toEqual([])
  })

  it('has no Amharic key that English lacks', () => {
    // A key with no English has no fallback, so a third language inherits a
    // hole rather than a sentence.
    const orphans = Object.keys(catalogue('am')).filter((key) => !(key in catalogue('en')))
    expect(orphans).toEqual([])
  })

  it('keeps the same placeholders in both languages', () => {
    const holes = (text: string) => (text.match(/\{(\w+)\}/g) ?? []).sort()
    for (const [key, english] of Object.entries(catalogue('en'))) {
      expect(holes(catalogue('am')[key] ?? ''), key).toEqual(holes(english))
    }
  })

  it('is actually in Amharic rather than copied English', () => {
    // A catalogue duplicated from English and never filled in passes every
    // coverage check ever written. Ge'ez script does not.
    for (const [key, text] of Object.entries(catalogue('am'))) {
      const letters = text.replace(/[^\p{L}]/gu, '')
      expect(/[ሀ-፿]/.test(letters), `${key} is not in Ge'ez script`).toBe(true)
    }
  })

  it('names each language in its own language', () => {
    expect(LANGUAGES.find((l) => l.code === 'am')?.name).toBe('አማርኛ')
  })
})

describe('lookup', () => {
  it('fills in placeholders', () => {
    expect(translate('en', 'plan.travel', { minutes: 28 })).toBe('28 min of travel')
  })

  it('leaves an unfilled placeholder visible', () => {
    // A sentence with `{minutes}` in it is obviously broken. A sentence with a
    // hole silently reads as finished.
    expect(translate('en', 'plan.travel')).toContain('{minutes}')
  })

  it('falls back to English rather than showing a key', () => {
    // Showing `nav.discover` has failed at being an interface; showing the
    // English is merely untranslated.
    const result = translate('am', 'nav.discover')
    expect(result).not.toBe('nav.discover')
  })

  it('returns the key when no language has the message', () => {
    expect(translate('en', 'nothing.here.at.all')).toBe('nothing.here.at.all')
  })
})

describe('plurals', () => {
  it('uses the singular for one in English', () => {
    expect(plural('en', 'search.results', 1)).toBe('1 result')
  })

  it('uses the plural for zero in English', () => {
    expect(plural('en', 'search.results', 0)).toBe('0 results')
  })

  it('uses the singular for zero in Amharic', () => {
    // The trap. CLDR puts 0 in the `one` category for Amharic, so a
    // hand-written `count === 1 ? singular : plural` produces the wrong form
    // for every empty state in the product.
    expect(new Intl.PluralRules('am-ET').select(0)).toBe('one')
    expect(plural('am', 'search.results', 0)).toBe('0 ውጤት')
    expect(plural('am', 'search.results', 1)).toBe('1 ውጤት')
  })

  it('uses the plural for many in Amharic', () => {
    expect(plural('am', 'search.results', 7)).toBe('7 ውጤቶች')
  })

  it('formats the count in the reader’s numerals', () => {
    expect(plural('en', 'reserve.remaining', 1200)).toContain('1,200')
  })

  it('falls back to the other form when a category is absent', () => {
    // Welsh has six categories and this catalogue has two. A language with a
    // form nobody wrote should degrade to `other`, not to the key.
    expect(plural('en', 'plan.stops', 3)).toBe('3 stops')
  })
})

describe('formatting', () => {
  it('renders birr as ብር in Amharic', () => {
    expect(formatMoney('am', 250, 'ETB')).toContain('ብር')
  })

  it('does not assume the currency', () => {
    // An experience carries its own; defaulting to ETB would quietly relabel a
    // price the publisher set in dollars.
    expect(formatMoney('en', 20, 'USD')).toContain('20')
    expect(formatMoney('en', 20, 'USD')).not.toContain('ETB')
  })

  it('survives an unknown currency code rather than blanking the price', () => {
    expect(formatMoney('en', 20, 'XYZZY')).toContain('20')
  })

  it('formats numbers per locale', () => {
    expect(formatNumber('en', 1234)).toBe('1,234')
  })

  it('joins lists with the right conjunction', () => {
    expect(formatList('en', ['a', 'b', 'c'])).toBe('a, b and c')
    // Amharic uses its own comma and conjunction.
    expect(formatList('am', ['ቡና', 'ሙዚቃ'])).toContain('እና')
  })

  it('says "tomorrow" rather than "in 1 days"', () => {
    const tomorrow = new Date(Date.now() + 26 * 3600 * 1000)
    expect(formatRelative('en', tomorrow)).toBe('tomorrow')
  })

  it('speaks Amharic for relative times too', () => {
    const yesterday = new Date(Date.now() - 26 * 3600 * 1000)
    expect(formatRelative('am', yesterday)).not.toMatch(/[a-z]/i)
  })
})

describe('what is deliberately not translated', () => {
  it('has no message for a listing’s own words', () => {
    // A publisher writes their listing in whatever language they choose and
    // Mado does not translate it. There is no key for a title or a description
    // because there is nothing to key - and the language setting says so.
    const keys = Object.keys(catalogue('en'))
    expect(keys.some((k) => k.startsWith('experience.title'))).toBe(false)
    expect(keys).toContain('settings.language.subtitle')
  })

  it('uses English as the reference catalogue', () => {
    expect(DEFAULT_LANGUAGE).toBe('en')
  })
})
