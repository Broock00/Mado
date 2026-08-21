/**
 * Looking somewhere else.
 *
 * The default is not a place at all - it is wherever the explorer is, resolved
 * from their coordinates. This exists for the two things location cannot do:
 * looking at somewhere you are not yet, and being somewhere Mado has nothing.
 *
 * Anything typeable works, because nothing here comes from a list Mado keeps. A
 * country, a city, a borough, a street, a landmark, a venue: the text goes to
 * Google Places and comes back as a point with a sensible radius around it, so
 * "5th Avenue" searches a few streets and "Brooklyn" searches a borough. The
 * predecessor of this component offered a dropdown of cities the database
 * happened to contain, which was one.
 *
 * **Two calls, not one.** Typing asks for suggestions - names and identifiers,
 * no coordinates. Choosing one asks for its details. That is worth the extra
 * round trip on selection because the alternative resolves every row in the
 * dropdown, five or six of which nobody picks, and is billed for all of them. A
 * session token ties the keystrokes to the choice so the provider charges for
 * the search rather than for the typing; it is minted per search and never
 * reused, because a reused token quietly loses the grouping.
 *
 * The resolved point is stored, not the words. Sending the text on every
 * request would re-resolve it each time, and a search that quietly resolved
 * somewhere else on the second attempt is hard to notice and harder to report.
 */

import { useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Check, ChevronDown, Loader2, MapPin, Search, X } from 'lucide-react'

import { api, newPlaceSessionToken } from '@/lib/api'
import { useAppStore } from '@/app/store'
import { useLocationContext, useRequestLocation } from '@/app/hooks'
import { cn } from '@/lib/utils'

export function PlaceFilter() {
  const place = useAppStore((s) => s.place)
  const setPlace = useAppStore((s) => s.setPlace)
  const location = useAppStore((s) => s.location)
  const requestLocation = useRequestLocation()
  const { data: context, isLoading: resolving } = useLocationContext()

  const [open, setOpen] = useState(false)
  const [term, setTerm] = useState('')
  const [debounced, setDebounced] = useState('')
  // Which row is being resolved, so the dropdown can show it working rather
  // than sitting still for the length of a network call.
  const [choosing, setChoosing] = useState<string | null>(null)
  const [failed, setFailed] = useState(false)
  const container = useRef<HTMLDivElement>(null)
  const session = useRef(newPlaceSessionToken())

  // Typing is not a search. Each keystroke would be a request to somebody
  // else's service, which their terms of use would rightly object to - and
  // although the session token means the provider bills the search rather than
  // the keystrokes, it does not make the requests themselves free.
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(term.trim()), 350)
    return () => clearTimeout(timer)
  }, [term])

  // Opening the box starts a new search, and a search is what a session token
  // groups. Carrying the previous one over would bill two searches as one and
  // rank the second against the first one's context.
  useEffect(() => {
    if (!open) return
    session.current = newPlaceSessionToken()
    setFailed(false)
  }, [open])

  useEffect(() => {
    if (!open) return
    const onPointerDown = (event: PointerEvent) => {
      if (!container.current?.contains(event.target as Node)) setOpen(false)
    }
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false)
    }
    document.addEventListener('pointerdown', onPointerDown)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [open])

  const near =
    location.granted && location.latitude != null && location.longitude != null
      ? { lat: location.latitude, lng: location.longitude }
      : null

  const { data: results, isFetching } = useQuery({
    queryKey: ['place-autocomplete', debounced, near?.lat, near?.lng],
    queryFn: () =>
      api.autocompletePlaces(debounced, near, { sessionToken: session.current }),
    enabled: debounced.length >= 2,
    // Predictions belong to the session that asked for them, and the session
    // ends the moment something is chosen. Caching across sessions would show a
    // second search results grouped under the first one's token.
    staleTime: 0,
    gcTime: 0,
  })

  /**
   * Resolve a chosen suggestion and adopt it.
   *
   * The details call closes the session, so a fresh token is minted immediately
   * afterwards - including on failure, because a token that has been sent once
   * has already been spent whether or not an answer came back.
   */
  const choose = async (placeId: string) => {
    setChoosing(placeId)
    setFailed(false)
    try {
      const context = await api.placeDetails(placeId, session.current)
      const found = context.place
      if (!found) {
        setFailed(true)
        return
      }
      setPlace({
        label: found.label,
        latitude: found.latitude,
        longitude: found.longitude,
        radiusKm: found.suggestedRadiusKm,
        // A country is searched by its code and a wide place by its box. Only
        // something small enough to be a circle relies on the radius above.
        countryCode:
          (found.kind || '').toLowerCase() === 'country' ? found.countryCode : null,
      })
      setTerm('')
      setOpen(false)
    } catch {
      setFailed(true)
    } finally {
      session.current = newPlaceSessionToken()
      setChoosing(null)
    }
  }

  // What the button says. Never "near you" for somewhere the explorer is not.
  const label = place
    ? place.label
    : context?.resolved && context.place
      ? `Near you · ${context.place.area || context.place.label}`
      : location.granted
        ? 'Near you'
        : 'Anywhere'

  return (
    <div className="relative" ref={container}>
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        aria-haspopup="dialog"
        className="inline-flex max-w-[16rem] items-center gap-1.5 rounded-lg border border-sand-300 bg-sand-100 px-3 py-2 text-sm font-medium text-sand-800 hover:bg-sand-200"
      >
        {resolving ? (
          <Loader2 className="size-4 shrink-0 animate-spin text-sand-500" aria-hidden />
        ) : (
          <MapPin className="size-4 shrink-0 text-sand-500" aria-hidden />
        )}
        <span className="truncate">{label}</span>
        <ChevronDown className="size-4 shrink-0 text-sand-500" aria-hidden />
      </button>

      {open && (
        <div
          role="dialog"
          aria-label="Choose where to look"
          className="absolute right-0 z-40 mt-1 w-80 rounded-xl border border-sand-200 bg-sand-100 p-2 shadow-lg"
        >
          <div className="flex items-center gap-2 rounded-lg border border-sand-300 px-2">
            <Search className="size-4 shrink-0 text-sand-500" aria-hidden />
            <input
              autoFocus
              value={term}
              onChange={(event) => setTerm(event.target.value)}
              placeholder="A city, a neighbourhood, a street…"
              aria-label="Search for a place"
              className="h-10 w-full bg-transparent text-sm outline-none placeholder:text-sand-400"
            />
            {term && (
              <button
                type="button"
                aria-label="Clear"
                onClick={() => setTerm('')}
                className="text-sand-400 hover:text-sand-700"
              >
                <X className="size-4" aria-hidden />
              </button>
            )}
          </div>

          <div className="mt-1 max-h-72 overflow-y-auto">
            <button
              type="button"
              onClick={() => {
                setPlace(null)
                if (!location.granted) requestLocation()
                setOpen(false)
              }}
              className={cn(
                'flex w-full items-center justify-between gap-2 rounded-lg px-3 py-2 text-left text-sm hover:bg-sand-200',
                !place && 'font-medium text-brand-400',
              )}
            >
              <span>
                Near me
                {!location.granted && (
                  <span className="block text-xs font-normal text-sand-500">
                    Needs your location
                  </span>
                )}
              </span>
              {!place && <Check className="size-4 shrink-0" aria-hidden />}
            </button>

            {debounced.length >= 2 && (
              <>
                <div className="my-1 border-t border-sand-200" />
                {isFetching && (
                  <p className="px-3 py-2 text-sm text-sand-500">Looking…</p>
                )}
                {!isFetching && results && results.length === 0 && (
                  <p className="px-3 py-2 text-sm text-sand-600">
                    Nowhere by that name. Try a city or a street.
                  </p>
                )}
                {failed && (
                  <p className="px-3 py-2 text-sm text-sand-600">
                    That place would not resolve. Try another.
                  </p>
                )}
                {results?.map((found) => (
                  <button
                    key={found.placeId}
                    type="button"
                    disabled={choosing !== null}
                    onClick={() => void choose(found.placeId)}
                    className="flex w-full items-center gap-2 rounded-lg px-3 py-2 text-left text-sm hover:bg-sand-200 disabled:opacity-60"
                  >
                    <span className="min-w-0 flex-1">
                      {/* Two lines rather than one: eight rows that all begin
                          "Brooklyn, " are unreadable at a glance. */}
                      <span className="block truncate text-sand-900">
                        {found.primary || found.text}
                      </span>
                      {found.secondary && (
                        <span className="block truncate text-xs text-sand-500">
                          {found.secondary}
                        </span>
                      )}
                    </span>
                    {choosing === found.placeId && (
                      <Loader2 className="size-4 shrink-0 animate-spin text-sand-500" aria-hidden />
                    )}
                  </button>
                ))}
              </>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
