/**
 * The current language, and how to change it (spec 11.07).
 *
 * **Where the preference lives depends on who is asking.** A signed-in explorer
 * has it on their profile, so it follows them to another device - which is the
 * whole point of a stated preference. Anonymous browsing keeps it in
 * `localStorage`, because somebody reading the city in Amharic before they have
 * an account should not have to re-choose on every visit.
 *
 * **Nothing waits on the network.** The language is read synchronously from
 * storage on the first render, so the first paint is already in the right
 * language. A profile that arrives later can correct it; a blank screen while
 * a preference loads cannot be corrected.
 *
 * **`<html lang>` is set, not just the text.** It is what a screen reader uses
 * to choose a voice, what the browser uses to pick hyphenation and fonts, and
 * what a translation prompt keys off. Amharic rendered with `lang="en"` is read
 * aloud as gibberish by a screen reader doing exactly what it was told.
 */

import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'

import { api } from '@/lib/api'
import { useAppStore } from '@/app/store'
import { LanguageContext, type LanguageValue } from '@/app/language-context'
import {
  DEFAULT_LANGUAGE,
  formatDate,
  formatList,
  formatMoney,
  formatNumber,
  formatRelative,
  formatTime,
  locale,
  plural,
  translate,
  type Language,
} from '@/lib/i18n'

const STORAGE_KEY = 'mado.language'

function stored(): Language | null {
  try {
    const value = localStorage.getItem(STORAGE_KEY)
    return value === 'en' || value === 'am' ? value : null
  } catch {
    // Private browsing with storage disabled. Not a reason to fail to render.
    return null
  }
}

/**
 * What the browser asks for, narrowed to something we have.
 *
 * Only consulted when nobody has chosen. `navigator.languages` is in the
 * reader's own order of preference, so the first supported entry is the right
 * one rather than the first Amharic one.
 */
function fromBrowser(): Language | null {
  for (const tag of navigator.languages ?? []) {
    const primary = tag.split('-')[0].toLowerCase()
    if (primary === 'am' || primary === 'en') return primary
  }
  return null
}

export function LanguageProvider({ children }: { children: ReactNode }) {
  const user = useAppStore((s) => s.user)
  const queryClient = useQueryClient()
  const [language, setState] = useState<Language>(
    () => stored() ?? fromBrowser() ?? DEFAULT_LANGUAGE,
  )

  // A signed-in explorer's profile is the authority, so it wins once it
  // arrives - but only over a value we guessed from the browser, never over one
  // they just chose on this device. `setLanguage` writes storage first, so the
  // two agree by the time this runs again.
  useEffect(() => {
    const profileLanguage = user?.profile?.language
    if (profileLanguage === 'en' || profileLanguage === 'am') {
      setState(profileLanguage)
      try {
        localStorage.setItem(STORAGE_KEY, profileLanguage)
      } catch {
        /* storage disabled */
      }
    }
  }, [user?.profile?.language])

  useEffect(() => {
    document.documentElement.lang = language
    // Amharic is written left to right, so there is no direction to change -
    // said explicitly because `dir` is the reflex when localisation comes up
    // and setting it to `rtl` here would be wrong.
    document.documentElement.dir = 'ltr'
  }, [language])

  const save = useMutation({
    mutationFn: (next: Language) => api.updateProfile({ language: next }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['me'] }),
  })

  const setLanguage = useCallback(
    (next: Language) => {
      // Applied immediately, saved in the background. Waiting for a round trip
      // to change the language of a button is a worse experience than the
      // change not persisting, and the failure mode is that they set it again.
      setState(next)
      try {
        localStorage.setItem(STORAGE_KEY, next)
      } catch {
        /* storage disabled */
      }
      if (user) save.mutate(next)
    },
    [user, save],
  )

  const value = useMemo<LanguageValue>(
    () => ({
      language,
      setLanguage,
      saving: save.isPending,
      t: (key, params) => translate(language, key, params),
      n: (key, count, params) => plural(language, key, count, params),
      date: (v, options) => formatDate(language, v, options),
      time: (v) => formatTime(language, v),
      relative: (v) => formatRelative(language, v),
      number: (v) => formatNumber(language, v),
      money: (amount, currency) => formatMoney(language, amount, currency),
      list: (items) => formatList(language, items),
      locale: locale(language),
    }),
    [language, setLanguage, save.isPending],
  )

  return <LanguageContext.Provider value={value}>{children}</LanguageContext.Provider>
}
