/**
 * What a listing claims to be suitable for.
 *
 * The client half of `backend/app/domains/catalog/suitability.py`. Two copies of
 * one vocabulary is a real cost, and the alternative - fetching the list at
 * runtime - is worse: the composer needs these to render a form before it has
 * asked the server anything, and a network round-trip to discover the names of
 * checkboxes would make the whole section flash in late. A backend test asserts
 * the two lists match, so drift fails a build rather than shipping a slug the
 * server silently drops.
 *
 * **Presence is a claim; absence is not a denial.** A listing that does not say
 * `vegan` has not said it lacks vegan food - nobody has said anything. Anything
 * rendering these must keep that distinction: see `unverified` on a card, which
 * is the set the explorer asked about and nobody has answered.
 */

export const SUITABILITY_GROUPS = [
  {
    key: 'dietary',
    label: 'Food and drink',
    // Named for what a publisher is being asked, not for the data model. "What
    // can you serve?" gets an honest answer; "dietary attributes" gets guesses.
    hint: 'Only tick what the kitchen can genuinely do every time it is open.',
    slugs: [
      'vegan',
      'vegetarian',
      'halal',
      'kosher',
      'gluten_free',
      'nut_free',
      'dairy_free',
      'fasting_menu',
      'alcohol_free',
      'serves_late',
    ],
  },
  {
    key: 'family',
    label: 'Children',
    hint: 'What is actually there, rather than whether children are welcome.',
    slugs: [
      'childrens_play_area',
      'child_menu',
      'high_chairs',
      'baby_changing',
      'child_friendly',
      'pushchair_access',
    ],
  },
  {
    key: 'access',
    label: 'Access',
    hint: 'Somebody will plan their day around these, so leave them blank if unsure.',
    slugs: [
      'step_free_access',
      'accessible_toilet',
      'accessible_parking',
      'hearing_loop',
      'sign_language',
      'quiet_space',
    ],
  },
  {
    key: 'comfort',
    label: 'Weather and comfort',
    hint: 'Used to decide what to suggest when the forecast is against being outside.',
    slugs: [
      'indoor_seating',
      'outdoor_seating',
      'shaded_seating',
      'heated',
      'air_conditioned',
      'covered',
    ],
  },
  {
    key: 'practical',
    label: 'Practical',
    hint: null,
    slugs: ['parking', 'wifi', 'prayer_room', 'pet_friendly', 'card_accepted'],
  },
] as const

export const SUITABILITY_LABELS: Record<string, string> = {
  vegan: 'vegan options',
  vegetarian: 'vegetarian options',
  halal: 'halal',
  kosher: 'kosher',
  gluten_free: 'gluten-free options',
  nut_free: 'nut-free kitchen',
  dairy_free: 'dairy-free options',
  fasting_menu: 'fasting menu',
  alcohol_free: 'alcohol-free',
  serves_late: 'serves late',
  childrens_play_area: "children's play area",
  child_menu: "children's menu",
  high_chairs: 'high chairs',
  baby_changing: 'baby changing',
  child_friendly: 'good with children',
  pushchair_access: 'pushchair access',
  step_free_access: 'step-free access',
  accessible_toilet: 'accessible toilet',
  accessible_parking: 'accessible parking',
  hearing_loop: 'hearing loop',
  sign_language: 'sign language',
  quiet_space: 'quiet space',
  indoor_seating: 'indoor seating',
  outdoor_seating: 'outdoor seating',
  shaded_seating: 'shade',
  heated: 'heated',
  air_conditioned: 'air conditioning',
  covered: 'covered',
  parking: 'parking',
  wifi: 'wifi',
  prayer_room: 'prayer room',
  pet_friendly: 'pets welcome',
  card_accepted: 'cards accepted',
}

export type SuitabilitySlug = keyof typeof SUITABILITY_LABELS

export const ALL_SUITABILITY = Object.keys(SUITABILITY_LABELS) as SuitabilitySlug[]

/** How a claim reads in a sentence, falling back to a readable form of the slug. */
export function suitabilityLabel(slug: string): string {
  return SUITABILITY_LABELS[slug] ?? slug.replace(/_/g, ' ')
}

/** A comma-joined phrase naming what a listing claims. */
export function describeSuitability(slugs: readonly string[]): string {
  return slugs.map(suitabilityLabel).join(', ')
}
