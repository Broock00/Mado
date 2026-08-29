/**
 * The plan catalogue, mirroring `app/domains/publisher/plans.py`.
 *
 * Duplicated for the same reason `suitability.ts` is: the upgrade page has to
 * render the tiers and what each one includes before it has asked the server
 * anything, and a screen that shows nothing until a request comes back is a
 * screen people leave. `plans.test.ts` asserts the two lists agree, so drift
 * fails a build rather than shipping a price list that disagrees with what is
 * charged.
 *
 * What is deliberately **not** here: the prices. Those come from the server,
 * per currency, because a price list is a decision that changes without a
 * deploy and a stale number in the bundle is the one thing worse than a
 * spinner.
 */

export const FREE = 'free'
export const PROFESSIONAL = 'professional'
export const BUSINESS = 'business'
export const ENTERPRISE = 'enterprise'

export interface PlanEntitlements {
  /** Null is "no limit", never zero — a missing number read as none would lock
   * an enterprise account out of what it paid most for. */
  maxLiveListings: number | null
  maxTeamMembers: number | null
  analyticsWindowDays: number
  aiAssistant: boolean
  apiAccess: boolean
}

export interface PlanDefinition {
  key: string
  name: string
  tagline: string
  entitlements: PlanEntitlements
}

/** Cheapest first, because that is how a price list is read. */
export const PLAN_ORDER = [FREE, PROFESSIONAL, BUSINESS, ENTERPRISE] as const

export const PLANS: Record<string, PlanDefinition> = {
  [FREE]: {
    key: FREE,
    name: 'Free',
    tagline: 'Put the business on Mado and start posting.',
    entitlements: {
      maxLiveListings: 5,
      maxTeamMembers: 1,
      analyticsWindowDays: 30,
      aiAssistant: false,
      apiAccess: false,
    },
  },
  [PROFESSIONAL]: {
    key: PROFESSIONAL,
    name: 'Professional',
    tagline: 'Publish without counting, with a team and the writing assistant.',
    entitlements: {
      maxLiveListings: null,
      maxTeamMembers: 5,
      analyticsWindowDays: 90,
      aiAssistant: true,
      apiAccess: false,
    },
  },
  [BUSINESS]: {
    key: BUSINESS,
    name: 'Business',
    tagline: 'For somewhere with several venues and a programme to run.',
    entitlements: {
      maxLiveListings: null,
      maxTeamMembers: 25,
      analyticsWindowDays: 90,
      aiAssistant: true,
      apiAccess: true,
    },
  },
  [ENTERPRISE]: {
    key: ENTERPRISE,
    name: 'Enterprise',
    tagline: 'Tourism boards, hotel groups and universities.',
    entitlements: {
      maxLiveListings: null,
      maxTeamMembers: null,
      analyticsWindowDays: 90,
      aiAssistant: true,
      apiAccess: true,
    },
  },
}

/**
 * What a plan includes, as sentences somebody can compare.
 *
 * Built from the entitlements rather than written out beside them, so a limit
 * that changes cannot leave a caption behind describing the old one.
 */
export function planIncludes(plan: PlanDefinition): string[] {
  const e = plan.entitlements
  return [
    e.maxLiveListings === null
      ? 'Unlimited posts live at once'
      : `${e.maxLiveListings} posts live at once`,
    e.maxTeamMembers === null
      ? 'Unlimited team members'
      : `${e.maxTeamMembers} team ${e.maxTeamMembers === 1 ? 'member' : 'members'}`,
    `${e.analyticsWindowDays} days of analytics`,
    e.aiAssistant ? 'Writing assistant' : null,
    e.apiAccess ? 'API access' : null,
  ].filter((line): line is string => line !== null)
}
