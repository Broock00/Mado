/**
 * Choosing a language (spec 11.07).
 *
 * Two things this is careful about:
 *
 * - **Each option is written in its own language.** "Amharic" is a word in
 *   English, and offering it to somebody who does not read English is asking
 *   them to find their language in a list they cannot read. አማርኛ is legible to
 *   exactly the people who want it.
 * - **It says what it does not change.** A publisher writes a listing in
 *   whatever language they choose, and Mado does not translate it. Somebody who
 *   switches to Amharic and still sees English listings should have been told
 *   that would happen, rather than concluding the setting is broken.
 *
 * The change applies immediately and saves in the background - waiting on a
 * round trip to relabel a button is worse than the save failing, and the
 * recovery from a failed save is choosing again.
 */

import { Check, Languages } from 'lucide-react'

import { useLanguage } from '@/app/language-context'
import { LANGUAGES, type Language } from '@/lib/i18n'
import { Card, SectionHeading } from '@/design-system/primitives'
import { cn } from '@/lib/utils'

export function LanguageChoice() {
  const { language, setLanguage, saving, t } = useLanguage()

  return (
    <section className="mt-10">
      <SectionHeading
        title={t('settings.language.title')}
        subtitle={t('settings.language.subtitle')}
      />
      <Card className="mt-3 divide-y divide-sand-200">
        {LANGUAGES.map((option) => {
          const chosen = option.code === language
          return (
            <button
              key={option.code}
              type="button"
              // `lang` on the option itself, so a screen reader switches voice
              // for this word rather than reading አማርኛ with an English one.
              lang={option.code}
              aria-pressed={chosen}
              disabled={saving}
              onClick={() => setLanguage(option.code as Language)}
              className={cn(
                'flex w-full items-center justify-between px-5 py-4 text-left transition-colors',
                'hover:bg-sand-200 disabled:opacity-60',
                chosen && 'bg-brand-900/30',
              )}
            >
              <span className="flex items-center gap-3">
                <Languages
                  className={cn('size-4', chosen ? 'text-brand-700' : 'text-sand-400')}
                  aria-hidden
                />
                <span className="text-sand-900">{option.name}</span>
              </span>
              {chosen && <Check className="size-4 text-brand-700" aria-hidden />}
            </button>
          )
        })}
      </Card>
    </section>
  )
}
