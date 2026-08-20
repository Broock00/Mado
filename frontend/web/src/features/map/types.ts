/**
 * What a map component is, independently of who draws it.
 *
 * Spec 82.01 s10 makes the map vendor infrastructure: it draws tiles, and Mado
 * supplies the pins and decides their order. That only stays true if no vendor
 * type reaches a caller, which is what this file is for - the props below are
 * the entire contract, and `ExperienceMap`, `RouteMap` and `PinMap` each pick an
 * implementation behind it.
 *
 * Coordinates are plain numbers in these types, never a vendor's LatLng: a page
 * that had to construct one to render a map would be a page that has to change
 * when the vendor does.
 */

import type { ExperienceSummary, PlanRoute } from '@/lib/types'
import type { LiveFix } from './useLiveLocation'

export interface ExperienceMapProps {
  experiences: ExperienceSummary[]
  /** The explorer's own position, drawn distinctly from the pins. */
  origin?: { latitude: number; longitude: number } | null
  className?: string
  /** Fixed zoom for a single-venue map, where fitting bounds makes no sense. */
  zoom?: number
}

export interface RouteMapProps {
  route: PlanRoute | null | undefined
  /** Stop titles in plan order. Positions come from `route.points`. */
  titles: string[]
  /** The live fix while navigating. Null when not. */
  fix?: LiveFix | null
  /** Metres travelled along the route, for shading the part already covered. */
  alongMetres?: number | null
  /** Keep the camera on the explorer instead of the whole route. */
  follow?: boolean
  className?: string
}

export interface PinMapProps {
  /** Where to open when there is no pin yet. */
  centre: { latitude: number; longitude: number }
  /**
   * Where the pin is. The map follows it, so moving the pin is done by changing
   * this rather than by reaching into the map - which is what keeps the two
   * implementations free of an imperative handle between them.
   */
  value: { latitude: number; longitude: number } | null
  /** A tap on the map, or the pin dragged and dropped. */
  onPick: (latitude: number, longitude: number) => void
  className?: string
}

/** Addis Ababa. Used only when there is nothing to fit the view to. */
export const FALLBACK_CENTRE = { latitude: 9.0192, longitude: 38.7525 }

/**
 * Kinds of place that are an area rather than somewhere you can stand outside.
 *
 * Two vocabularies in one set because the providers name things differently -
 * OpenStreetMap says "city" and "suburb", Google says "locality" and
 * "sublocality" - and no caller should have to know which one answered.
 */
const AREA_KINDS = new Set([
  'city',
  'town',
  'village',
  'municipality',
  'hamlet',
  'suburb',
  'quarter',
  'district',
  'county',
  'state',
  'country',
  'administrative',
  'locality',
  'postal_town',
  'sublocality',
  'sublocality_level_1',
  'neighborhood',
  'neighbourhood',
  'postal_code',
  'political',
  'administrative_area_level_1',
  'administrative_area_level_2',
  'administrative_area_level_3',
])

/**
 * Whether a pin of this kind stands for a whole area rather than one place.
 *
 * What it changes is whether the pin's own label can serve as a venue's name.
 * For a cafe it can, and asking somebody to retype the name they picked a
 * second ago is friction for nothing. For a city it cannot: a venue called
 * "Nairobi" tells an explorer standing in Nairobi nothing at all.
 *
 * Takes the kind rather than the pin so this module stays free of a dependency
 * on `LocationPicker`, which is lazily loaded - importing it here to read one
 * string would pull the whole map bundle into every page that asks.
 */
export function isAreaPick(kind: string | null | undefined): boolean {
  return Boolean(kind && AREA_KINDS.has(kind.toLowerCase()))
}

/** Close enough to read street names, which is what confirms a pin is right. */
export const PLACE_ZOOM = 17
export const CITY_ZOOM = 13
