/**
 * Choosing where something is, on a map.
 *
 * Replaces typing an address, which was the wrong interaction in both
 * directions: a publisher standing outside their own cafe does not know the
 * postal address a geocoder wants, and a text box gives no feedback at all until
 * after they have committed to it. Worse, a geocoder returns a confident,
 * precise-looking point for a bad query - so the failure mode was a listing
 * pinned somewhere plausible and wrong, with nobody able to tell.
 *
 * A map inverts that. The publisher sees where the pin is the entire time, and
 * the coordinates come from a deliberate act rather than an interpretation of
 * one. Three ways to place it, in the order people reach for them:
 *
 * 1. **Use my location** - the common case, because most people add a place
 *    while standing in it.
 * 2. **Tap or drag** - for adding somewhere they are not.
 * 3. **Search** - kept, but demoted to a way of *moving the map*, not a way of
 *    setting the answer. The pin still has to be placed.
 *
 * Search suggests as you type rather than taking one guess at whatever was
 * submitted. That matters more here than on the discovery filter: a publisher
 * searching their own venue by name gets it as a suggestion, and the map lands
 * on the building instead of the street. The identifier of whatever was chosen
 * travels out with the pin so the venue can be saved against it - and is dropped
 * the moment the pin is moved by hand, because a pin somewhere else is no longer
 * that place.
 *
 * The address is reverse-geocoded and shown underneath, purely as confirmation.
 * The coordinates are the truth; a missing label is cosmetic.
 *
 * Which vendor draws the map is not this component's business - `PinMap` picks
 * one. Everything here is about what a pin *means*, which does not change with
 * who renders it.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { Crosshair, LoaderCircle, MapPin, Search } from 'lucide-react'

import { api, newPlaceSessionToken } from '@/lib/api'
import { DEVICE_LOCATION_OPTIONS } from '@/lib/deviceLocation'
import type { PlaceSuggestion } from '@/lib/types'
import { Button, Input } from '@/design-system/primitives'
import { PinMap } from './PinMap'

export interface PickedLocation {
  latitude: number
  longitude: number
  label: string | null
  /**
   * The place provider's identifier, when the pin came from a search result
   * rather than a tap. Null once the pin has been moved by hand: the identifier
   * describes a specific place, and a pin dragged fifty metres away is no longer
   * at it. Saved with the venue, and read by nothing.
   */
  placeId: string | null
  /**
   * The wider area this point is in - "Brooklyn", "Nairobi". Shown so a
   * publisher can see which city their post will be filed under, which is the
   * question the city dropdown used to ask them and now answers for them.
   */
  area?: string | null
  /**
   * What the provider says this is - "locality", "restaurant", "route". Carried
   * because a pin standing for a whole city is not the same thing as a pin on a
   * building, and only one of them has a name that could be a venue's: falling
   * back to the label of a city pick would create a venue called "Nairobi".
   */
  kind?: string | null
  /**
   * The currency in official use where this is, so a post is priced in the money
   * of wherever it actually is rather than of whichever city a developer had
   * typed in.
   */
  currency?: string | null
  /**
   * Credits the place provider requires be shown wherever this place's data is
   * displayed. Usually empty. Not stored with the venue: the obligation attaches
   * to showing the provider's description of a place, and once the pin is saved
   * what is shown is Mado's own venue record.
   */
  attributions?: string[]
}

export interface LocationPickerProps {
  /**
   * Where to open, and what search is biased towards. The explorer's city
   * centre if they have not shared location.
   */
  centre: { latitude: number; longitude: number }
  value: PickedLocation | null
  onChange: (location: PickedLocation) => void
  className?: string
}

export function LocationPicker({
  centre,
  value,
  onChange,
  className,
}: LocationPickerProps) {
  // Held in a ref as well as state: the map's event handlers are bound once and
  // would otherwise close over the first render's callback forever.
  const onChangeRef = useRef(onChange)
  onChangeRef.current = onChange

  const [locating, setLocating] = useState(false)
  const [labelling, setLabelling] = useState(false)
  const [query, setQuery] = useState('')
  const [debounced, setDebounced] = useState('')
  const [suggestions, setSuggestions] = useState<PlaceSuggestion[]>([])
  const [searching, setSearching] = useState(false)
  const [problem, setProblem] = useState<string | null>(null)
  const session = useRef(newPlaceSessionToken())

  /**
   * Move the pin, then ask what is there.
   *
   * `known` carries the name and identifier through when the move came from a
   * chosen suggestion, which is both better than the reverse-geocoded label and
   * free - it has already been paid for.
   */
  const place = useCallback(
    async (
      latitude: number,
      longitude: number,
      known?: {
        label: string
        placeId: string
        area: string | null
        currency: string | null
        kind: string | null
        attributions: string[]
      },
    ) => {
      setProblem(null)
      // Report the coordinates immediately. The description arrives later and
      // must never gate the answer - a publisher who taps and submits at once
      // has still chosen a real place.
      onChangeRef.current({
        latitude,
        longitude,
        label: known?.label ?? null,
        placeId: known?.placeId ?? null,
        area: known?.area ?? null,
        currency: known?.currency ?? null,
        kind: known?.kind ?? null,
        attributions: known?.attributions ?? [],
      })
      // The map follows `value`, so reporting the coordinates has already moved
      // the pin - there is nothing imperative left to do here.

      // Nothing left to ask: a chosen suggestion arrives already described, and
      // resolving it again would spend a lookup to replace "Blue Bottle Coffee"
      // with the street it is on.
      if (known) return

      setLabelling(true)
      try {
        // `/places/resolve` rather than the publisher's own reverse geocode:
        // same question, and this one comes back with the administrative chain,
        // so the pin also tells us which city the post belongs to and what it
        // should be priced in.
        const context = await api.resolvePlace(latitude, longitude)
        const found = context.place
        onChangeRef.current({
          latitude,
          longitude,
          label: found?.label ?? null,
          // A tap is not a place, so there is no identifier to keep even though
          // the point resolved to somewhere with one.
          placeId: null,
          area: found?.area ?? null,
          currency: found?.currency ?? null,
          // A tap is a point on a building, whatever the geocoder called the
          // feature it landed in - so it is never treated as an area pick.
          kind: null,
          attributions: found?.attributions ?? [],
        })
      } catch {
        // A missing description is cosmetic; the pin is already placed, and the
        // server works the city out from the coordinates either way.
      } finally {
        setLabelling(false)
      }
    },
    [],
  )


  // Typing is not a search: each keystroke would be a request to somebody
  // else's service.
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(query.trim()), 350)
    return () => clearTimeout(timer)
  }, [query])

  useEffect(() => {
    if (debounced.length < 3) {
      setSuggestions([])
      return
    }
    // Biased towards where the map is looking rather than the city the post is
    // filed under: a publisher who has already panned to the right district
    // means the café on that street, not the one with the same name across town.
    let current = true
    api
      .autocompletePlaces(debounced, { lat: centre.latitude, lng: centre.longitude }, {
        sessionToken: session.current,
      })
      .then((found) => {
        if (current) setSuggestions(found)
      })
      .catch(() => {
        // Suggestions are an accelerator. Losing them leaves the map, which is
        // the way the pin is actually placed.
        if (current) setSuggestions([])
      })
    return () => {
      current = false
    }
  }, [debounced, centre.latitude, centre.longitude])

  function useMyLocation() {
    if (!('geolocation' in navigator)) {
      setProblem('This browser cannot share your location. Tap the map instead.')
      return
    }
    setLocating(true)
    setProblem(null)
    navigator.geolocation.getCurrentPosition(
      (position) => {
        setLocating(false)
        void place(position.coords.latitude, position.coords.longitude)
      },
      (error) => {
        setLocating(false)
        setProblem(
          error.code === error.PERMISSION_DENIED
            ? 'Location is blocked for this site. Tap the map to place the pin instead.'
            : 'Could not get your location. Tap the map to place the pin instead.',
        )
      },
      // A venue pin needs street-level accuracy, and it is worth a few seconds
      // to get it rather than dropping the pin on a cell tower. Same fresh
      // high-accuracy options as discover — the publisher still confirms the pin.
      DEVICE_LOCATION_OPTIONS,
    )
  }

  /**
   * Resolve a chosen suggestion and move the map to it.
   *
   * Moves the pin as well, but the publisher still sees and confirms it: search
   * is a way of getting the map to roughly the right place, not a way of
   * asserting an answer. The details call closes the session, so a fresh token
   * is minted afterwards whether or not it succeeded.
   */
  async function choose(suggestion: PlaceSuggestion) {
    setSearching(true)
    setProblem(null)
    try {
      const context = await api.placeDetails(suggestion.placeId, session.current)
      const found = context.place
      if (!found) {
        setProblem('Could not find that. Try a landmark, or just tap the map.')
        return
      }
      setQuery('')
      setSuggestions([])
      await place(found.latitude, found.longitude, {
        label: found.label,
        placeId: found.placeId ?? suggestion.placeId,
        area: found.area || null,
        currency: found.currency || null,
        kind: found.kind ?? null,
        attributions: found.attributions ?? [],
      })
    } catch {
      setProblem('Could not find that. Try a landmark, or just tap the map.')
    } finally {
      session.current = newPlaceSessionToken()
      setSearching(false)
    }
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-2">
        <Button
          type="button"
          variant="secondary"
          size="sm"
          onClick={useMyLocation}
          disabled={locating}
        >
          {locating ? (
            <LoaderCircle className="size-4 animate-spin" aria-hidden />
          ) : (
            <Crosshair className="size-4" aria-hidden />
          )}
          {locating ? 'Finding you…' : "I'm here now"}
        </Button>

        <div className="relative flex min-w-[12rem] flex-1 gap-2">
          <Input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => {
              // Enter takes the first suggestion. Submitting the raw text would
              // mean guessing at it, which is the interaction this replaced.
              if (event.key === 'Enter') {
                event.preventDefault()
                if (suggestions[0]) void choose(suggestions[0])
              }
              if (event.key === 'Escape') setSuggestions([])
            }}
            placeholder="Or search for a place"
            aria-label="Search for a place to move the map"
            autoComplete="off"
          />
          <span className="grid w-9 shrink-0 place-items-center text-sand-500">
            {searching ? (
              <LoaderCircle className="size-4 animate-spin" aria-hidden />
            ) : (
              <Search className="size-4" aria-hidden />
            )}
          </span>

          {suggestions.length > 0 && (
            <ul
              className="absolute top-full left-0 z-30 mt-1 w-full overflow-hidden rounded-xl border border-sand-200 bg-sand-100 shadow-lifted"
              aria-label="Places matching your search"
            >
              {suggestions.map((suggestion) => (
                <li key={suggestion.placeId}>
                  <button
                    type="button"
                    disabled={searching}
                    onClick={() => void choose(suggestion)}
                    className="block w-full px-3 py-2 text-left text-sm hover:bg-sand-200 disabled:opacity-60"
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
      </div>

      <PinMap
        centre={centre}
        value={value}
        // A tap or a drag is a deliberate placement with no place behind it, so
        // it goes through `place` with no `known` - which drops any identifier a
        // previous search had attached and asks what is at the new point.
        onPick={(latitude, longitude) => void place(latitude, longitude)}
        className={className}
      />

      <p className="text-sm text-sand-600">
        {value ? (
          <span className="inline-flex items-start gap-1.5">
            <MapPin className="mt-0.5 size-4 shrink-0 text-brand-600" aria-hidden />
            <span>
              <span className="text-sand-900">{value.label ?? 'Pin placed'}</span>
              {labelling && <span className="text-sand-500"> — checking…</span>}
              <span className="block text-xs text-sand-500">
                {value.latitude.toFixed(5)}, {value.longitude.toFixed(5)}
              </span>
              {/* Required by the provider wherever its description of a place is
                  shown. Almost always empty; dropping it when it is not would be
                  a licence breach rather than a missing detail. */}
              {value.attributions && value.attributions.length > 0 && (
                <span className="block text-xs text-sand-500">
                  {value.attributions.join(' · ')}
                </span>
              )}
            </span>
          </span>
        ) : (
          'Tap the map where the place is, or use the buttons above.'
        )}
      </p>

      {problem && (
        <p className="text-sm text-red-300" role="alert">
          {problem}
        </p>
      )}
    </div>
  )
}

export default LocationPicker
