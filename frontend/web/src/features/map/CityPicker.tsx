/**
 * Which city a post is filed under, chosen from the place provider's list.
 *
 * The city field came back, but not the one that was removed. That one was a
 * dropdown of the ten cities somebody had already typed into a table, which was
 * a ceiling on where the platform could be used: a publisher in a city nobody
 * had added simply could not pick one. This asks the same provider that answers
 * every other location question, so every inhabited place on earth is in the
 * list and none of them needs a row first.
 *
 * **It is an accelerator, not the answer.** The pin below still decides where
 * the post is, and the server still works the city out by reverse-geocoding the
 * coordinates - a client-named city would let two venues on the same street
 * file under different ones. What choosing a city here does is move the pin to
 * it, which moves the map there and biases the venue search that follows. A
 * publisher who knows only the city has said something true and is not blocked;
 * one who then finds their building refines it, and the city follows the pin
 * rather than the other way round.
 *
 * Predictions are narrowed to inhabited places, because here a street is not an
 * answer. Everywhere else that narrowing would be a category somebody cannot
 * find, which is why it is a parameter and not the default.
 */

import { useEffect, useRef, useState } from 'react'
import { Building2, LoaderCircle } from 'lucide-react'

import { api, newPlaceSessionToken } from '@/lib/api'
import type { Place, PlaceSuggestion } from '@/lib/types'
import { Input } from '@/design-system/primitives'

export interface CityPickerProps {
  /**
   * The city currently in force, worked out from wherever the pin is. Shown
   * whenever the box is not being typed in, so the field and the map cannot
   * disagree about which city the post is in - the pin is the one answer, and
   * this reflects it rather than holding a second one.
   */
  value: string | null
  /** Where to bias predictions towards, so a common name resolves nearby. */
  centre: { latitude: number; longitude: number }
  onChange: (place: Place) => void
}

export function CityPicker({ value, centre, onChange }: CityPickerProps) {
  // Null means "not being edited", which is what lets the field show `value`
  // without a second effect copying one into the other on every render - and
  // without the box fighting somebody mid-word each time the pin resolves.
  const [draft, setDraft] = useState<string | null>(null)
  const [debounced, setDebounced] = useState('')
  const [suggestions, setSuggestions] = useState<PlaceSuggestion[]>([])
  const [resolving, setResolving] = useState(false)
  const [problem, setProblem] = useState<string | null>(null)
  const session = useRef(newPlaceSessionToken())

  const query = draft ?? value ?? ''

  // Typing is not a search: each keystroke would be a request to somebody
  // else's service, and on Google a separately billed one.
  useEffect(() => {
    const timer = setTimeout(() => setDebounced((draft ?? '').trim()), 350)
    return () => clearTimeout(timer)
  }, [draft])

  useEffect(() => {
    if (debounced.length < 2) {
      setSuggestions([])
      return
    }
    let current = true
    api
      .autocompletePlaces(
        debounced,
        { lat: centre.latitude, lng: centre.longitude },
        { sessionToken: session.current, citiesOnly: true },
      )
      .then((found) => {
        if (current) setSuggestions(found)
      })
      .catch(() => {
        // Losing suggestions leaves the map below, which is how the location is
        // actually set. Nothing here is required.
        if (current) setSuggestions([])
      })
    return () => {
      current = false
    }
  }, [debounced, centre.latitude, centre.longitude])

  async function choose(suggestion: PlaceSuggestion) {
    setResolving(true)
    setProblem(null)
    try {
      const context = await api.placeDetails(suggestion.placeId, session.current)
      const found = context.place
      if (!found) {
        setProblem('Could not find that city. Place the pin on the map instead.')
        return
      }
      setSuggestions([])
      // Back to showing `value`, which the chosen city is about to become. The
      // typed fragment is not kept: it was a way of finding the place, not a
      // second opinion about what it is called.
      setDraft(null)
      onChange(found)
    } catch {
      setProblem('Could not find that city. Place the pin on the map instead.')
    } finally {
      // The details call closes the session, and reusing a spent token silently
      // loses the grouping that makes the search one billed lookup.
      session.current = newPlaceSessionToken()
      setResolving(false)
    }
  }

  return (
    <div>
      <label htmlFor="city" className="mb-1.5 block text-sm font-medium text-sand-700">
        City
      </label>
      <div className="relative flex gap-2">
        <Input
          id="city"
          value={query}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            // Enter takes the first suggestion. Submitting the raw text would
            // mean guessing at which city was meant, and a guessed city files
            // the post somewhere nobody can find it.
            if (event.key === 'Enter') {
              event.preventDefault()
              if (suggestions[0]) void choose(suggestions[0])
            }
            if (event.key === 'Escape') {
              setSuggestions([])
              setDraft(null)
            }
          }}
          placeholder="Which city is this in?"
          aria-label="City"
          autoComplete="off"
        />
        <span className="grid w-9 shrink-0 place-items-center text-sand-500">
          {resolving ? (
            <LoaderCircle className="size-4 animate-spin" aria-hidden />
          ) : (
            <Building2 className="size-4" aria-hidden />
          )}
        </span>

        {suggestions.length > 0 && (
          <ul
            className="absolute top-full left-0 z-30 mt-1 w-full overflow-hidden rounded-xl border border-sand-200 bg-white shadow-lifted"
            aria-label="Cities matching what you typed"
          >
            {suggestions.map((suggestion) => (
              <li key={suggestion.placeId}>
                <button
                  type="button"
                  disabled={resolving}
                  onClick={() => void choose(suggestion)}
                  className="block w-full px-3 py-2 text-left text-sm hover:bg-sand-100 disabled:opacity-60"
                >
                  <span className="block truncate text-sand-900">
                    {suggestion.primary || suggestion.text}
                  </span>
                  {suggestion.secondary && (
                    <span className="block truncate text-xs text-sand-500">
                      {suggestion.secondary}
                    </span>
                  )}
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>

      <p className="mt-1.5 text-xs text-sand-500">
        Optional — the map below answers this on its own. Pick one to start there.
      </p>

      {problem && (
        <p className="mt-1.5 text-sm text-red-700" role="alert">
          {problem}
        </p>
      )}
    </div>
  )
}

export default CityPicker
