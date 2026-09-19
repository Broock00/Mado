/**
 * API types mirroring the backend schemas.
 *
 * Hand-written for now. Spec 80.03 s28 calls for SDK generation from the OpenAPI
 * document, which is the right end state - the API already publishes one at
 * /openapi.json. Until that generator is wired into CI, these stay in sync by
 * review.
 */

import type { SuitabilitySlug } from './suitability'

export type { SuitabilitySlug }

export interface Meta {
  requestId?: string | null
  timestamp?: string | null
}

export interface Pagination {
  nextCursor?: string | null
  hasMore: boolean
  totalCount?: number | null
}

export interface Envelope<T> {
  data: T
  meta?: Meta
}

export interface CollectionEnvelope<T> {
  data: T[]
  pagination?: Pagination
  meta?: Meta
}

export interface ApiErrorBody {
  error: {
    code: string
    message: string
    details?: Record<string, unknown>
    requestId?: string | null
    timestamp?: string | null
  }
}

export interface City {
  id: string
  name: string
  slug: string
  country: string
  countryCode: string
  timezone: string
  currency: string
  languages: string[]
  latitude: number
  longitude: number
  isLive: boolean
}

export interface Neighborhood {
  id: string
  name: string
  slug: string
  description?: string | null
  latitude: number
  longitude: number
}

export interface Category {
  id: string
  name: string
  slug: string
  icon?: string | null
}

export interface Tag {
  id: string
  name: string
  slug: string
}

export interface Media {
  id: string
  type: string
  url: string
  altText?: string | null
  sortOrder: number
}

export interface PublisherSummary {
  id: string
  name: string
  slug: string
  logoUrl?: string | null
  verificationStatus: string
  trustLevel: number
  /**
   * `organization` means a business posted this; `individual` means a person
   * did. On a card that is the difference between "the cafe says so" and
   * "somebody who went there says so", which are different claims.
   */
  type: 'individual' | 'organization'
  businessType?: string | null
  businessTypeLabel?: string | null
}

export interface VenueSummary {
  id: string
  name: string
  slug: string
  address: string
  latitude: number
  longitude: number
  neighborhood?: Neighborhood | null
  accessibility: Record<string, unknown>
  /**
   * The building's own claims. Sent apart from the experience's union so an
   * editor can tell which record owns a claim and not offer to untick one it
   * does not own.
   */
  facilities: SuitabilitySlug[]
  openingHours: Record<string, string>
}

export interface EventInstance {
  id: string
  startTime: string
  endTime?: string | null
  status: string
  capacity?: number | null
  remaining?: number | null
  cancellationReason?: string | null
}

export interface Price {
  type: 'free' | 'fixed' | 'range'
  amount?: number | null
  maxAmount?: number | null
  currency: string
}

export interface ExperienceSummary {
  id: string
  title: string
  slug: string
  summary?: string | null
  type: 'place' | 'event' | 'activity'
  category?: Category | null
  tags: Tag[]
  venue?: VenueSummary | null
  citySlug?: string | null
  price: Price
  media: Media[]
  publisher?: PublisherSummary | null
  ratingAverage?: number | null
  ratingCount: number
  nextEvent?: EventInstance | null
  durationMinutes?: number | null
  isIndoor?: boolean | null
  /**
   * What this listing claims to be suitable for, from the vocabulary in
   * `suitability.ts` - the union of the experience's own claims and its venue's.
   */
  suitability: SuitabilitySlug[]
  /**
   * Of what the explorer asked for, what this listing has *not* claimed.
   *
   * Unknown, never denied. Rendering this as "does not have" would invent a
   * refusal nobody made, which for an access or allergy need is the harmful
   * direction to be wrong in.
   */
  unverified: SuitabilitySlug[]
  /** Why this was surfaced - spec PRODUCT-00 principle 5. */
  reason?: string | null
  distanceKm?: number | null
  isSaved: boolean
  /** The count comes off the listing itself, so a card costs no extra query. */
  repostCount: number
  isReposted: boolean
  /**
   * A business paid for this position.
   *
   * Never inferable from anything else on the card, and never true for a
   * listing that merely ranked well — sponsorship is a separate labelled slot,
   * not a weight on the ranking. The card must show the label whenever this is
   * set, including in the compact layout: it is the condition paid placement is
   * sold under, not a decoration.
   */
  sponsored?: boolean
  /** Set alongside `sponsored`, so a click can be attributed to the campaign. */
  promotionId?: string | null
}

/** The state of the repost toggle after it was pressed. */
export interface Reaction {
  active: boolean
  count: number
}


export interface ExperienceDetail extends ExperienceSummary {
  description: string
  accessibility: Record<string, unknown>
  attributes: Record<string, unknown>
  upcomingEvents: EventInstance[]
  publishedAt?: string | null
  updatedAt?: string | null
}

export interface FeedModule {
  key: string
  title: string
  subtitle?: string | null
  layout: 'carousel' | 'grid' | 'list' | 'map'
  items: ExperienceSummary[]
}

export interface DiscoveryCanvas {
  /** A city row, when one happens to match. Not the scoping key any more. */
  city: string | null
  /** What to call where this is showing, when a place was searched by name. */
  areaLabel?: string | null
  /** `chosen` | `location` | `unknown` - why here, so the page can say. */
  resolvedBy: string
  modules: FeedModule[]
}

export interface SearchResponse {
  results: ExperienceSummary[]
  meta: {
    query: string
    total: number
    /** True when the search index was unreachable and results came from Postgres. */
    degraded: boolean
    /**
     * True when vector retrieval contributed, so the interface can distinguish
     * "matched your words" from "understood what you meant" - and so a silent
     * fall back to keyword-only is visible.
     */
    semantic: boolean
  }
}

export interface ConciergeResult {
  id: string
  title: string
  summary?: string | null
  type?: string | null
  category?: string | null
  venueName?: string | null
  neighborhood?: string | null
  when?: string | null
  price?: string | null
  reason?: string | null
  distanceKm?: number | null
  rating?: number | null
  /** A result in the chat is a post like any other, so it can be reposted
   *  from there. Zero for plan stops, which are built by hand. */
  repostCount: number
  isReposted: boolean
  publisherName?: string | null
  publisherSlug?: string | null
  publisherType?: 'individual' | 'organization' | null
}

export interface SuggestedAction {
  label: string
  message: string
}

/** An itinerary the concierge worked out, offered for the explorer to keep. */
export interface OfferedPlan {
  stops: PlanStop[]
  totalCost: number
  currency: string
  totalTravelMinutes: number
  rationale: string
  unmet: string[]
}

export interface ConciergeResponse {
  conversationId: string
  message: string
  intent: string
  confidence: number
  /**
   * Present when the turn produced an itinerary. Nothing is stored until the
   * explorer accepts it - most plans are looked at once and a different one
   * asked for.
   */
  plan?: OfferedPlan | null
  /** One sentence naming what a refinement changed. Absent on a first plan.
   *  Derived by comparing the two plans, not from the model's own account. */
  planChange?: string | null
  results: ConciergeResult[]
  suggestedActions: SuggestedAction[]
  clarification?: string | null
  model?: string | null
  latencyMs?: number | null
}

/** One of the languages Mado is available in, named in itself. */
export interface LanguageOption {
  code: string
  name: string
  /** Whether the request that returned this was answered in this language. */
  current: boolean
}

export interface UserProfile {
  userId: string
  displayName: string
  email?: string | null
  avatarUrl?: string | null
  bio?: string | null
  language: string
  timezone: string
  homeCitySlug?: string | null
  preferences: Record<string, unknown>
  privacy: Record<string, unknown>
}

export interface Me {
  id: string
  status: string
  isVerified: boolean
  createdAt: string
  profile: UserProfile
  /**
   * Display hint for whether to offer the moderation console. Never a
   * permission check - the API re-verifies on every moderation call.
   */
  isModerator?: boolean
}

export interface TokenPair {
  accessToken: string
  refreshToken: string
  tokenType: string
  expiresIn: number
}

export interface AuthResponse {
  user: Me
  tokens: TokenPair
}

/** One signed-in device, as the account settings list them. */
export interface AuthSession {
  id: string
  createdAt: string
  expiresAt: string
  userAgent?: string | null
  platform?: string | null
  /** Marks the row the reader is most likely looking at it from. A hint for a
   *  human, never an authorization decision - the server treats every row the
   *  same. */
  isCurrent: boolean
}

/** Suggestions for a draft listing. Every field is optional: anything that
 *  failed the server's grounding check is simply absent. */
export interface AssistSuggestions {
  summary?: string | null
  description?: string | null
  categorySlug?: string | null
  tags: string[]
  /** Questions a reader would still have. Nothing to apply - the platform does
   *  not know the answers and must not appear to. */
  missing: string[]
  /** False when no model is configured; the control hides itself. */
  available: boolean
}

/* --------------------------------------------------- flags and the record */

export interface FeatureFlag {
  key: string
  description: string
  enabled: boolean
  rolloutPercentage: number
  updatedAt?: string | null
}

export interface AuditEntry {
  id: string
  actorLabel: string
  action: string
  subjectType: string
  subjectId?: string | null
  subjectLabel?: string | null
  reason?: string | null
  context: Record<string, unknown>
  occurredAt: string
}

/**
 * One thing the concierge offers before being asked (spec AI-005).
 *
 * Null is a normal answer, not an error: no stated city, no affinity yet, or
 * nothing on that clears the quality floor.
 */
export interface ProactiveSuggestion {
  experienceId: string
  title: string
  summary?: string | null
  category?: string | null
  venueName?: string | null
  when?: string | null
  reason: string
}

/* ------------------------------------------------------ publisher standing */

export interface ReputationSignal {
  key: string
  label: string
  /** -1 to 1. Negative pulled the score down. */
  direction: number
  detail: string
}

/**
 * A publisher's own standing. There is no type for anybody else's, because
 * there is no endpoint that returns one.
 */
export interface PublisherReputation {
  score: number
  /** excellent | good | mixed | poor | provisional */
  band: string
  isProvisional: boolean
  completedDates: number
  cancelledDates: number
  ratings: number
  reports: number
  withheldListings: number
  signals: ReputationSignal[]
  computedAt?: string | null
}

/* ---------------------------------------------- searching by photograph */

/**
 * What the model made of a photograph (spec SRCH-005).
 *
 * It says what *kind* of thing the picture shows and never names a venue, so
 * this is grounds for a search rather than an identification.
 */
export interface VisualLook {
  description: string
  terms: string[]
  confidence: number
  /** The subject could not be made out. `results` will be empty. */
  unclear: boolean
}

export interface VisualSearchResult {
  look: VisualLook
  results: ExperienceSummary[]
  meta: SearchResponse['meta']
}

/* -------------------------------------------------- the developer platform */

export interface DeveloperScope {
  key: string
  description: string
}

/* --------------------------------------------------------- places */

/**
 * Somewhere, as an external geocoder describes it.
 *
 * Everything but the coordinates is optional: the administrative chain differs
 * by country, and a London borough is not a state. Render what is there.
 */
export interface Place {
  latitude: number
  longitude: number
  /** The shortest phrase a person would use. */
  label: string
  /** The wider area, for "near you in Brooklyn". */
  area: string
  displayName?: string
  country?: string | null
  countryCode?: string | null
  region?: string | null
  county?: string | null
  locality?: string | null
  district?: string | null
  neighbourhood?: string | null
  road?: string | null
  postcode?: string | null
  kind?: string | null
  /** How far around this place to look first - a road is not a borough. */
  suggestedRadiusKm: number
  /**
   * south, west, north, east when the place has real extent. Sent back on
   * search so a borough is a box rather than a circle around its centre.
   */
  boundingBox?: number[] | null
  provider: string
  /** The provider's identifier, kept when saving a venue against this place. */
  placeId?: string | null
  /**
   * The currency in official use in this country, from CLDR. Not a property of
   * the place - a lookup on `countryCode` done server-side so the browser does
   * not need its own copy of the country-to-currency table.
   */
  currency?: string
  /**
   * Credits the provider requires be shown wherever this place is. Usually
   * empty; when it is not, rendering the place without them breaches the
   * licence it came under.
   */
  attributions?: string[]
}

/**
 * One row of the location box, while somebody is still typing.
 *
 * Deliberately has no coordinates. Resolving every row would cost a lookup for
 * the several nobody picks; `api.placeDetails` resolves the one that is chosen.
 */
export interface PlaceSuggestion {
  placeId: string
  text: string
  /** "Brooklyn" - what the row leads with. */
  primary: string
  /** "NY, USA" - what tells two identically-named streets apart. */
  secondary: string
  kinds: string[]
  distanceMetres?: number | null
  provider: string
}

export interface LocationContext {
  resolved: boolean
  place?: Place | null
  reason?: string | null
}

/**
 * A place the explorer picked, kept so the interface can name it and the server
 * can search it.
 *
 * Three ways to describe an area, because one does not fit them all. A street or
 * a neighbourhood is a point and a radius. A borough or a city is a bounding
 * box. A country is a country code - exact, where a box around Kenya also
 * covers four neighbours and the one around the United States spans the globe.
 */
export interface ChosenPlace {
  label: string
  latitude: number
  longitude: number
  radiusKm: number
  /** south,west,north,east - present when the place has real extent. */
  bbox?: string | null
  /** Set only when the explorer picked a whole country. */
  countryCode?: string | null
}

/* ------------------------------------------------------- commerce */

/** One tier on one date: "General admission", "VIP" (spec COM-002). */
/** What the door was told about one scan. Codes, not sentences. */
export interface Scan {
  /** `admitted` | `already_admitted` | `wrong_event` | `void` | `unknown` */
  verdict: string
  /** Absent unless the ticket is for this door. */
  name?: string | null
  ticketTypeName?: string | null
  reference?: string | null
  checkedInAt?: string | null
  admittedCount: number
  issuedCount: number
}

/**
 * One ticket as the publisher thinks of it: across every date, not on one.
 *
 * Tiers are stored per date because inventory is, but nobody authors them that
 * way - a six-night run sells the same VIP ticket on all six.
 */
export interface PlanEntry {
  name: string
  description?: string | null
  priceMinor: number
  currency: string
  quantity?: number | null
  /** How many dates carry it. */
  dates: number
  sold: number
  remaining?: number | null
  isActive: boolean
  /** True when the dates disagree - somebody edited one by hand. */
  varies: boolean
}

/** One tier's sales, as the publisher sees them. */
export interface TierSales {
  ticketTypeId: string
  name: string
  priceMinor: number
  quantity?: number | null
  sold: number
  remaining?: number | null
  revenueMinor: number
}

/** One booking on the door list. Named, and nothing else about the person. */
export interface Buyer {
  orderId: string
  reference: string
  name: string
  quantity: number
  amountMinor: number
  /** `paid` | `pending` */
  status: string
  /** Rendered lines, e.g. "2 x VIP". */
  tiers: string[]
  orderedAt: string
  paidAt?: string | null
}

export interface Bookings {
  currency: string
  capacity?: number | null
  /** Paid and pending together - what decides whether the room is full. */
  seatsTaken: number
  seatsRemaining?: number | null
  ticketsPaid: number
  ticketsPending: number
  revenueMinor: number
  /** Held, not taken. Never added to revenue. */
  pendingMinor: number
  ordersPaid: number
  ordersPending: number
  ordersFailed: number
  tiers: TierSales[]
  buyers: Buyer[]
}

export interface TicketTypeSummary {
  id: string
  name: string
  description?: string | null
  /** Santim, not birr. Formatted at the edge, never stored as a float. */
  priceMinor: number
  currency: string
  quantity?: number | null
  remaining?: number | null
  onSale: boolean
  /** `withdrawn` | `not_open_yet` | `closed` | `sold_out` */
  unavailableReason?: string | null
  position: number
}

/**
 * What the details page should say and offer.
 *
 * Codes, not sentences: the server decides the *state* and this client decides
 * the words, so there is one place Amharic lives rather than two that drift.
 */
export interface Ticketing {
  availability: string
  cta: string
  priceType: string
  currency: string
  minPriceMinor?: number | null
  maxPriceMinor?: number | null
  externalUrl?: string | null
  seatsRemaining?: number | null
  ticketTypes: TicketTypeSummary[]
}

export interface OrderLine {
  ticketTypeId: string
  ticketTypeName: string
  quantity: number
  unitPriceMinor: number
  totalMinor: number
}

export interface IssuedTicket {
  id: string
  code: string
  ticketTypeName: string
  status: string
  checkedInAt?: string | null
}

export interface Order {
  id: string
  reference: string
  /** pending | paid | failed | expired | cancelled */
  status: string
  experienceId: string
  experienceTitle: string
  eventInstanceId: string
  startsAt: string
  amountMinor: number
  currency: string
  quantity: number
  /** Absent for a free order, which has nothing to pay. */
  checkoutUrl?: string | null
  expiresAt?: string | null
  paidAt?: string | null
  outcomeReason?: string | null
  lines: OrderLine[]
  /** Only ever populated once the money arrived. */
  tickets: IssuedTicket[]
}

/** A client library the developer page offers for download. */
export interface Sdk {
  language: string
  label: string
  /** API version plus a digest of the endpoint surface. */
  version: string
  filename: string
  files: string[]
}

export interface WebhookEventType {
  type: string
  description: string
}

export interface ApiKey {
  id: string
  name: string
  /** Prefix and last four. Never enough to use. */
  preview: string
  scopes: string[]
  /** active | expired | revoked */
  state: string
  createdAt: string
  lastUsedAt?: string | null
  expiresAt?: string | null
  revokedAt?: string | null
}

/** The one response that carries the key itself. */
export interface NewApiKey {
  key: ApiKey
  secret: string
}

export interface WebhookEndpoint {
  id: string
  url: string
  events: string[]
  /** active | paused | suspended */
  status: string
  secretPreview: string
  consecutiveFailures: number
  createdAt: string
  lastSuccessAt?: string | null
  lastFailureAt?: string | null
  lastError?: string | null
}

export interface NewWebhookEndpoint {
  endpoint: WebhookEndpoint
  secret: string
}

export interface WebhookDelivery {
  id: string
  eventId: string
  eventType: string
  /** pending | retrying | delivered | failed */
  status: string
  attempts: number
  isTest: boolean
  createdAt: string
  nextAttemptAt: string
  deliveredAt?: string | null
  responseStatus?: number | null
  error?: string | null
  durationMs?: number | null
}

/* ------------------------------------------------------------- reservations */

export interface Availability {
  /** available | limited | full | cancelled */
  status: string
  capacity?: number | null
  /** Null when the publisher set no capacity - unlimited, not zero. */
  remaining?: number | null
  isUnlimited: boolean
  canReserve: boolean
}

export interface Reservation {
  id: string
  eventInstanceId: string
  experienceId: string
  experienceTitle: string
  startsAt: string
  partySize: number
  status: string
  note?: string | null
}

export interface Attendee {
  reservationId: string
  name: string
  partySize: number
  note?: string | null
  reservedAt: string
}

/* ------------------------------------------------------------ route guidance */

export interface RouteLeg {
  fromIndex: number
  toIndex: number
  mode: 'walk' | 'drive' | string
  durationMinutes: number
  distanceKm: number
  /** GeoJSON order: [[lon, lat], ...]. */
  geometry: number[][]
  /** True when the router could not serve this hop and it is a straight line.
   *  Drawn dashed - a solid line claims a road that nobody verified. */
  isEstimated: boolean
}

export interface RoutePoint {
  index: number
  latitude: number
  longitude: number
}

export interface PlanRoute {
  legs: RouteLeg[]
  /** Where each located stop is, for the numbered markers. Stops with no
   *  located venue are absent rather than placed at a guess. */
  points: RoutePoint[]
  totalDurationMinutes: number
  totalDistanceKm: number
  provider: string
  /** True only when nothing at all was routed for real. */
  isEstimated: boolean
  estimatedLegs: number
  /** Minutes the real route adds over what the plan assumed. Positive is worse. */
  driftMinutes: number
  warning?: string | null
}

/* ---------------------------------------------------------------- analytics */

export interface DayPoint {
  day: string
  views: number
  saves: number
}

export interface ExperienceMetrics {
  experienceId: string
  title: string
  status: string
  moderationStatus: string
  views: number
  uniqueViewers: number
  saves: number
  /** Net of unsaves. Saved forty times and unsaved thirty-nine is not popular. */
  netSaves: number
  /** Null when there were too few views for a ratio to mean anything - not 0,
   *  which would read as "nobody saves this". */
  saveRate?: number | null
  ratingAverage?: number | null
  ratingCount: number
  reportCount: number
}

export interface PublisherAnalytics {
  windowDays: number
  totalViews: number
  uniqueViewers: number
  totalSaves: number
  netSaves: number
  saveRate?: number | null
  publishedCount: number
  draftCount: number
  withheldCount: number
  reviewCount: number
  ratingAverage?: number | null
  reportCount: number
  series: DayPoint[]
  experiences: ExperienceMetrics[]
  /** The threshold the server applied, so the explanation always matches it. */
  minRateSample: number
}

export interface CategoryWeight {
  slug: string
  name: string
  /** A weighted score, not a number of events. Never render it as "9 times". */
  weight: number
}

export interface ExplorerSummary {
  savedCount: number
  collectionCount: number
  planCount: number
  reviewCount: number
  exploredCount: number
  recentDays: number
  topCategories: CategoryWeight[]
  memberSince?: string | null
  isEmpty: boolean
}

/* -------------------------------------------------------------- collections */

export type CollectionVisibility = 'private' | 'unlisted' | 'public'

export interface CollectionCard {
  id: string
  slug: string
  title: string
  description?: string | null
  citySlug?: string | null
  visibility: CollectionVisibility
  source: string
  itemCount: number
  previewImageUrls: string[]
  isMine: boolean
  /** Only ever populated for the owner - a reader does not need to know a page
   *  is awaiting review, they need it withheld, which the listing query does. */
  moderationStatus?: string | null
}

export interface CollectionDetail extends CollectionCard {
  experiences: ExperienceSummary[]
  /** Keyed by experience id: why the curator put each one in. */
  notes: Record<string, string>
}

export interface SavedItem {
  id: string
  entityType: string
  entityId: string
  note?: string | null
  createdAt: string
}

/* ------------------------------------------------------------------ publishing */

export interface Publisher {
  id: string
  name: string
  slug: string
  type: 'individual' | 'organization'
  verificationStatus: string
  trustLevel: number
  logoUrl?: string | null
}

export type PostStatus = 'draft' | 'review' | 'published' | 'archived'
export type ModerationStatus = 'approved' | 'pending' | 'flagged' | 'rejected'

/** An author's view of their own post, including editorial state. */
export interface OwnPost extends ExperienceDetail {
  status: PostStatus
  moderationStatus: ModerationStatus
  moderationNotes?: string | null
  reportCount: number
  /** What still stands between this draft and going live. */
  readinessProblems: string[]
}

export interface CreatePostInput {
  title: string
  description: string
  type: 'place' | 'event' | 'activity'
  /**
   * Normally omitted. A post's city follows its venue, which was itself filed
   * under the city its coordinates turned out to be in. Send one only for a post
   * with no location at all.
   */
  citySlug?: string | null
  summary?: string | null
  categorySlug?: string | null
  venueId?: string | null
  tags?: string[]
  priceType?: 'free' | 'fixed' | 'range'
  priceAmount?: number | null
  /** Omitted follows the city's own currency, which is right almost always. */
  currency?: string | null
  durationMinutes?: number | null
  isIndoor?: boolean | null
  /**
   * What this listing claims to be suitable for. Anything outside the vocabulary
   * is dropped by the server rather than rejecting the save, so a stale client
   * loses a claim instead of losing the post somebody just wrote.
   */
  suitability?: SuitabilitySlug[] | null
}

/**
 * A business — what a business *account* is.
 *
 * An account is a person or a business, never both. There is no business login:
 * the same credentials sign in to the same account, and after converting, that
 * account has no personal profile alongside the business.
 */
export interface Business {
  id: string
  name: string
  slug: string
  type: 'individual' | 'organization'
  businessType?: string | null
  businessTypeLabel?: string | null
  description?: string | null
  industry?: string | null
  website?: string | null
  contact: Record<string, string>
  social: Record<string, string>
  logoUrl?: string | null
  coverUrl?: string | null
  verificationStatus: string
  trustLevel: number
  createdAt?: string | null
}

/**
 * What a business has taken, in one currency.
 *
 * Four figures rather than one, because they answer different questions and
 * deriving any of them here would be a second opinion about money: gross is
 * what explorers paid, fee is Mado's commission, net is what the business is
 * owed, and owing is the part of that net not yet paid out.
 *
 * One row per currency. A business selling in Addis and in Nairobi earns in
 * two, and a single figure adding birr to shillings would mean nothing.
 */
export interface EarningsLine {
  currency: string
  grossMinor: number
  feeMinor: number
  netMinor: number
  owingMinor: number
  sales: number
}

export interface BusinessPayout {
  id: string
  currency: string
  totalMinor: number
  entryCount: number
  periodStart: string
  periodEnd: string
  status: 'owing' | 'paid'
  paidAt?: string | null
  reference?: string | null
}

/**
 * How much of the API allowance an account has used.
 *
 * `monthlyAllowance` is null for an unmetered agreement — never zero, which
 * would read as "no calls allowed", the opposite of what it means.
 */
export interface ApiUsageSummary {
  plan: string
  planName: string
  callsThisMonth: number
  monthlyAllowance: number | null
  /** The day the allowance resets, so somebody who has run out knows whether to
   * wait or to ask for more. */
  resetsOn: string
  daily: { day: string; calls: number }[]
}

export interface PlanEntitlementsOut {
  maxLiveListings: number | null
  maxTeamMembers: number | null
  analyticsWindowDays: number
  aiAssistant: boolean
  apiAccess: boolean
}

export interface BusinessPlan {
  key: string
  name: string
  tagline: string
  entitlements: PlanEntitlementsOut
  /** Null where the plan is not sold in the currency asked for — never a
   * converted figure, which would be a price nobody decided to charge. */
  priceMinor?: number | null
  currency?: string | null
  purchasable: boolean
}

export interface BusinessSubscription {
  /** What is in force now, which is not always what was bought: a period that
   * has run out reads as free without anything having to expire it, and an
   * account part-way through buying an upgrade still holds what it paid for. */
  plan: string
  planName: string
  status: string
  currentPeriodEnd?: string | null
  /** What is being bought, while a purchase waits on the provider. */
  pendingPlan?: string | null
  /** Cleared the moment the purchase settles, so a stale link cannot become a
   * second charge. */
  checkoutUrl?: string | null
}

export interface BusinessPlans {
  current: BusinessSubscription
  plans: BusinessPlan[]
  /** What these prices are quoted in — the business's own city, unless asked. */
  currency: string
  /** Every currency a plan is sold in, so the switcher is never a stale list. */
  soldIn: string[]
  /** Who takes the money in that currency. See `PromotionPricing.provider`. */
  provider: string
}

/**
 * A paid slot a business bought, and what it did.
 *
 * `impressions` and `clicks` are reporting only. Nothing about what is shown
 * depends on them — a counter that fed back into placement would become a
 * reason to show a listing to somebody it does not suit.
 */
export interface BusinessPromotion {
  id: string
  experienceId: string
  experienceTitle?: string | null
  citySlug?: string | null
  startsAt: string
  endsAt: string
  status: 'pending_payment' | 'active' | 'ended' | 'refused'
  amountMinor: number
  currency: string
  checkoutUrl?: string | null
  impressions: number
  clicks: number
}

export interface PromotionPricing {
  currency: string
  /** Null where promotions are not sold in this currency — never converted. */
  dailyMinor: number | null
  minDays: number
  maxDays: number
  /** Every currency a promotion is sold in, so a switcher is never stale. */
  soldIn: string[]
  /**
   * Who will take the money, decided by the currency. Named by the server
   * because `provider_for` is the one thing that knows — a client
   * reimplementing that rule would eventually tell somebody they are paying by
   * card and then send them to Chapa.
   */
  provider: string
}

export interface StartPromotionInput {
  experienceId: string
  days: number
  currency?: string
  citySlug?: string
  latitude?: number
  longitude?: number
  radiusKm?: number
}

export interface BusinessEarnings {
  /**
   * Stated outright, so the gap between gross and net is never left to be
   * inferred from arithmetic — a deduction nobody can name reads as a mistake.
   */
  feeRateBps: number
  totals: EarningsLine[]
  payouts: BusinessPayout[]
}

/**
 * A photograph or video a business put on its own profile.
 *
 * Distinct from `Media`, which belongs to a listing and disappears with it.
 * This is the building, the rooms, the view — what somebody deciding whether to
 * go wants to see, and what a history of posts cannot show them.
 */
export interface BusinessMedia {
  id: string
  kind: 'image' | 'video'
  url: string
  caption?: string | null
  sortOrder: number
  /**
   * Null for a video, always. Nothing decodes video server-side, and the layout
   * reserves space from these — treating null as 16:9 would make every vertical
   * phone video jump when it loads.
   */
  width?: number | null
  height?: number | null
  contentType?: string | null
  createdAt?: string | null
}

/** A business as an explorer sees it, with what it currently has published. */
export interface PublicBusiness extends Business {
  listings: ExperienceSummary[]
  gallery: BusinessMedia[]
}

export interface BusinessMember {
  id: string
  role: string
  roleLabel: string
  status: 'invited' | 'active' | 'declined' | 'removed'
  /** Null while an invitation is unanswered — no account is attached yet. */
  userId?: string | null
  displayName?: string | null
  invitedEmail?: string | null
  invitedAt?: string | null
  expiresAt?: string | null
  /** What the role permits, from the same source that enforces it. */
  permissions: string[]
}

export interface BusinessInvitation {
  id: string
  role: string
  roleLabel: string
  businessId: string
  businessName: string
  businessSlug: string
  invitedAt?: string | null
  expiresAt?: string | null
}

export interface BusinessRole {
  value: string
  label: string
  permissions: string[]
}

export type AccountType = 'individual' | 'business'

export interface AccountTypeState {
  accountType: AccountType
  /**
   * False when the account has never answered — it predates the question, or
   * has only just registered. Distinct from the type itself, which defaults to
   * individual, so the interface asks once rather than reading a default as a
   * decision.
   */
  chosen: boolean
  business?: Business | null
}

/** Someone a post can be published under. */
export interface PublishingIdentity {
  id: string
  name: string
  type: 'individual' | 'organization'
  logoUrl?: string | null
  /** The account's own identity. Exactly one entry carries this. */
  isDefault: boolean
}

export interface CreateBusinessInput {
  name: string
  businessType?: string | null
  description?: string | null
  website?: string | null
  contact?: Record<string, string> | null
  social?: Record<string, string> | null
  logoUrl?: string | null
  coverUrl?: string | null
}

export interface CreateVenueInput {
  name: string
  address: string
  latitude: number
  longitude: number
  /**
   * Normally omitted: the server works the city out from the coordinates, which
   * is what lets a venue be added anywhere rather than only in a city somebody
   * had already typed into a table.
   */
  citySlug?: string | null
  /**
   * The building's half of the suitability vocabulary - step-free access, a car
   * park, a play area. These stay true whoever is performing tonight, which is
   * why they belong to the venue rather than to a listing.
   */
  facilities?: SuitabilitySlug[] | null
  /**
   * The place provider's identifier, when the coordinates came from a search
   * rather than a pin dropped on a map. Optional: a venue is located by its
   * coordinates, and most are placed by hand.
   */
  placeId?: string | null
}

export interface VenueCreated {
  id: string
  name: string
  slug: string
  address: string
  latitude: number
  longitude: number
}

export type ReportReason =
  | 'spam'
  | 'inaccurate'
  | 'inappropriate'
  | 'duplicate'
  | 'scam'
  | 'other'

// --- Planning (spec 10.01.04, Journey Planner) -------------------------------

export interface PlanStop {
  /** Persisted stop row id — present on itinerary stops, absent on ephemeral plans. */
  id?: string | null
  experienceId: string
  eventInstanceId?: string | null
  title: string
  arriveAt: string
  departAt: string
  dwellMinutes: number
  travelMinutes: number
  travelKm?: number | null
  estimatedCost: number
  /** Dictated by a scheduled event rather than chosen, so it cannot be moved. */
  isFixedTime: boolean
  note?: string | null
  /** 0-based day within a multi-day trip; outings are always 0. */
  dayIndex?: number
}

export interface Plan {
  /** Where the plan ended up - the server may have resolved it from location. */
  citySlug?: string | null
  stops: PlanStop[]
  totalCost: number
  currency: string
  totalTravelMinutes: number
  rationale: string
  /** Constraints the planner could not meet, stated rather than hidden. */
  unmet: string[]
}

export interface Itinerary {
  id: string
  title: string
  citySlug?: string | null
  startsAt: string
  endsAt: string
  estimatedCost?: number | null
  currency: string
  totalTravelMinutes: number
  rationale?: string | null
  /** "draft" while building, "kept" after explicit save. */
  status: 'draft' | 'kept'
  /** outing = one day; trip = multi-day builder. */
  kind?: 'outing' | 'trip'
  timezone?: string | null
  /** Who can open the link — same meaning as collections. */
  visibility?: PlanVisibility
  /** Whether the current caller owns this plan. */
  isMine?: boolean
  stops: PlanStop[]
}

export type PlanVisibility = 'private' | 'unlisted' | 'public'

// ---- Stop spec for full replacement
export interface StopSpec {
  experienceId: string
  eventInstanceId?: string | null
  isFixedTime?: boolean
  arriveAt?: string | null
  departAt?: string | null
  note?: string | null
  dayIndex?: number
}

// ---- Analysis (Check My Plan result)
export interface PlanConflict {
  kind: 'overlap' | 'travel_gap' | 'fixed_time_miss' | 'window_overrun' | 'budget_overrun'
  stopIndices: number[]
  message: string
  resolutions: Array<'move_stop' | 'change_duration' | 'remove_stop' | 'keep_as_is'>
}

export interface PlanGap {
  afterIndex: number
  startsAt: string
  endsAt: string
  freeMinutes: number
}

export interface PlanAnalysis {
  conflicts: PlanConflict[]
  gaps: PlanGap[]
  totalCost: number
  totalTravelMinutes: number
  budgetOverrun: number
}

// ---- Draft create / patch
export interface CreateDraftInput {
  title?: string
  city?: string
  latitude?: number | null
  longitude?: number | null
  startsAt?: string | null
  endsAt?: string | null
  budget?: number | null
  freeOnly?: boolean
  timezone?: string | null
  kind?: 'outing' | 'trip' | null
}

export interface PatchDraftInput {
  title?: string | null
  city?: string | null
  startsAt?: string | null
  endsAt?: string | null
  budget?: number | null
  freeOnly?: boolean | null
}

export interface PlanRequestInput {
  startsAt?: string | null
  endsAt?: string | null
  /**
   * Omitted unless the explorer chose one. Unlike a feed, a plan cannot
   * degrade to nothing - the server refuses with CITY_REQUIRED when it can
   * resolve neither a choice nor a location.
   */
  city?: string
  latitude?: number | null
  longitude?: number | null
  budget?: number | null
  maxStops?: number
  categories?: string[]
  freeOnly?: boolean
}

// --- Memory and privacy ------------------------------------------------------

export interface MemoryEntry {
  id: string
  type: string
  category?: string | null
  attribute?: string | null
  value: string
  /** Already discounted for age - what this memory is worth today. */
  confidence: number
  source: string
  /** True when the explorer said it, false when the platform inferred it. */
  isExplicit: boolean
  lastReinforcedAt?: string | null
}

export interface PrivacySettings {
  personalizationEnabled: boolean
  locationEnabled: boolean
  aiMemoryEnabled: boolean
  analyticsEnabled: boolean
}

// --- Moderation --------------------------------------------------------------

export interface ModerationItem {
  id: string
  title: string
  summary?: string | null
  publisherName?: string | null
  moderationStatus: ModerationStatus
  /** The screening signals, in words - a moderator sees why, not just a number. */
  moderationNotes?: string | null
  reportCount: number
  riskScore: number
  citySlug?: string | null
  createdAt: string
}

/**
 * An account, as an administrator sees it.
 *
 * Deliberately thin. The email is here because identifying the right account is
 * the whole job, but nothing about what the person searched for, planned or was
 * recommended appears - an admin console is not a surveillance surface.
 */
export interface AdminAccount {
  id: string
  displayName: string
  email?: string | null
  status: 'active' | 'suspended' | string
  isModerator: boolean
  isVerified: boolean
  createdAt: string
  /** The pair a decision actually turns on: how much they published, and how
   *  much of it drew reports. Neither number alone says which they are. */
  publishedCount: number
  reportedCount: number
}

export type VerificationStatus = 'unverified' | 'requested' | 'verified' | string

export interface PublisherVerification {
  id: string
  name: string
  slug: string
  verificationStatus: VerificationStatus
  /** Whatever the publisher offered as evidence. */
  verificationNote?: string | null
  verificationRequestedAt?: string | null
  trustLevel: number
}


export interface GeocodeResult {
  latitude: number
  longitude: number
  /** The provider's tidied version of the address, shown back for confirmation. */
  formattedAddress: string
  confidence: number
  provider: string
}


// --- Reviews (spec BUSINESS-03, the third content inflow) ---------------------

export interface ReviewEntry {
  id: string
  rating: number
  comment?: string | null
  authorName: string
  verifiedAttendance: boolean
  createdAt: string
  /** Only ever set on your own review, so a withheld one can be explained. */
  status?: string | null
  moderationNotes?: string | null
}

export interface RatingSummary {
  average?: number | null
  count: number
  /** The spread behind the average - what an average on its own conceals. */
  distribution: Record<number, number>
}

export interface ReviewsResponse {
  summary: RatingSummary
  reviews: ReviewEntry[]
  mine?: ReviewEntry | null
}


// --- Notifications (spec NOT-001) --------------------------------------------

export interface NotificationEntry {
  id: string
  kind: string
  title: string
  body?: string | null
  link?: string | null
  isUnread: boolean
  deliveredAt?: string | null
}

export interface NotificationInbox {
  notifications: NotificationEntry[]
  unread: number
}

export interface NotificationPreferences {
  /** Every kind with its resolved state, so a client never duplicates defaults. */
  kinds: Record<string, boolean>
}
