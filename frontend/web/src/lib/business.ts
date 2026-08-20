/**
 * Business types, mirroring `backend/app/domains/publisher/business.py`.
 *
 * Duplicated for the same reason `suitability.ts` is: the form has to render a
 * dropdown before it has asked the server anything, and a round-trip to
 * discover the options makes the field arrive late. A backend test asserts the
 * two lists match, so drift fails a build rather than shipping a value the
 * server silently drops.
 *
 * An unrecognised type is dropped server-side rather than rejecting the whole
 * submission — it is a label, and losing somebody's description over it would
 * be a poor trade. So a stale client loses the field, never the profile.
 */

export const BUSINESS_TYPES = [
  { value: 'hotel', label: 'Hotel' },
  { value: 'resort', label: 'Resort' },
  { value: 'guesthouse', label: 'Guesthouse' },
  { value: 'restaurant', label: 'Restaurant' },
  { value: 'cafe', label: 'Cafe' },
  { value: 'bar', label: 'Bar' },
  { value: 'museum', label: 'Museum' },
  { value: 'gallery', label: 'Gallery' },
  { value: 'venue', label: 'Venue' },
  { value: 'attraction', label: 'Attraction' },
  { value: 'gym', label: 'Gym' },
  { value: 'spa', label: 'Spa' },
  { value: 'shopping', label: 'Shopping centre' },
  { value: 'tour_operator', label: 'Tour operator' },
  { value: 'organization', label: 'Organization' },
  { value: 'other', label: 'Other' },
] as const

export type BusinessTypeValue = (typeof BUSINESS_TYPES)[number]['value']

export function businessTypeLabel(value: string | null | undefined): string {
  if (!value) return 'Business'
  return BUSINESS_TYPES.find((type) => type.value === value)?.label ?? 'Business'
}

/** Social platforms a business may list, in the order they are shown. */
export const SOCIAL_PLATFORMS = [
  'instagram',
  'facebook',
  'x',
  'tiktok',
  'youtube',
  'linkedin',
  'telegram',
] as const
