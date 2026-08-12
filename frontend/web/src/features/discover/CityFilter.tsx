/**
 * Choosing which city to look at, or letting location decide.
 *
 * The default is **not** a city. It is "wherever I am": the client sends
 * coordinates and the server resolves the nearest covered city, so somebody
 * opening Mado in a city it covers gets that city without touching anything.
 * This control exists for the two cases location cannot serve - planning a trip
 * to somewhere you are not yet, and being somewhere Mado does not cover.
 *
 * Before this, `citySlug` started at a hardcoded `'addis-ababa'`, was persisted
 * to localStorage, and nothing in the interface ever called `setCity`. Every
 * explorer on earth was shown one Ethiopian city with no way to change it.
 */

import { useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Check, ChevronDown, MapPin } from 'lucide-react'

import { api } from '@/lib/api'
import { useAppStore } from '@/app/store'
import { cn } from '@/lib/utils'

export function CityFilter({ resolvedCity }: { resolvedCity?: string | null }) {
  const citySlug = useAppStore((s) => s.citySlug)
  const setCity = useAppStore((s) => s.setCity)
  const location = useAppStore((s) => s.location)
  const [open, setOpen] = useState(false)
  const container = useRef<HTMLDivElement>(null)

  const { data: cities } = useQuery({
    queryKey: ['cities', 'live'],
    queryFn: () => api.cities(true),
    // The set of covered cities changes when a city is launched, not while
    // somebody is browsing.
    staleTime: 30 * 60_000,
  })

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

  // What the button says. A chosen city is named; otherwise the label follows
  // what the server actually resolved, so "Near you" is never claimed for
  // somewhere the explorer is not.
  const chosen = citySlug ? cities?.find((c) => c.slug === citySlug) : null
  const nearby = !citySlug && resolvedCity ? cities?.find((c) => c.slug === resolvedCity) : null
  const label = chosen?.name ?? (nearby ? `Near you · ${nearby.name}` : 'Choose a city')

  return (
    <div className="relative" ref={container}>
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        aria-haspopup="listbox"
        className="inline-flex items-center gap-1.5 rounded-lg border border-sand-300 bg-white px-3 py-2 text-sm font-medium text-sand-800 hover:bg-sand-100"
      >
        <MapPin className="size-4 text-sand-500" aria-hidden />
        {label}
        <ChevronDown className="size-4 text-sand-500" aria-hidden />
      </button>

      {open && (
        <div
          role="listbox"
          className="absolute right-0 z-40 mt-1 max-h-80 w-64 overflow-y-auto rounded-xl border border-sand-200 bg-white p-1 shadow-lg"
        >
          <button
            type="button"
            role="option"
            aria-selected={citySlug === null}
            onClick={() => {
              setCity(null)
              setOpen(false)
            }}
            className={cn(
              'flex w-full items-center justify-between gap-2 rounded-lg px-3 py-2 text-left text-sm hover:bg-sand-100',
              citySlug === null && 'font-medium text-brand-800',
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
            {citySlug === null && <Check className="size-4 shrink-0" aria-hidden />}
          </button>

          <div className="my-1 border-t border-sand-200" />

          {cities?.map((city) => (
            <button
              key={city.id}
              type="button"
              role="option"
              aria-selected={citySlug === city.slug}
              onClick={() => {
                setCity(city.slug)
                setOpen(false)
              }}
              className={cn(
                'flex w-full items-center justify-between gap-2 rounded-lg px-3 py-2 text-left text-sm hover:bg-sand-100',
                citySlug === city.slug && 'font-medium text-brand-800',
              )}
            >
              <span>
                {city.name}
                <span className="block text-xs font-normal text-sand-500">{city.country}</span>
              </span>
              {citySlug === city.slug && <Check className="size-4 shrink-0" aria-hidden />}
            </button>
          ))}

          {cities && cities.length === 0 && (
            <p className="px-3 py-2 text-sm text-sand-600">No cities are live yet.</p>
          )}
        </div>
      )}
    </div>
  )
}
