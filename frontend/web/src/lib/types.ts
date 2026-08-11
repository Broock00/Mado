/**
 * API types mirroring the backend schemas.
 *
 * Hand-written for now. Spec 80.03 s28 calls for SDK generation from the OpenAPI
 * document, which is the right end state - the API already publishes one at
 * /openapi.json. Until that generator is wired into CI, these stay in sync by
 * review.
 */

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
  /** Why this was surfaced - spec PRODUCT-00 principle 5. */
  reason?: string | null
  distanceKm?: number | null
  isSaved: boolean
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
  city: string
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

/* ------------------------------------------------------- commerce */

/** One tier on one date: "General admission", "VIP" (spec COM-002). */
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
  citySlug: string
  type: 'place' | 'event' | 'activity'
  summary?: string | null
  categorySlug?: string | null
  venueId?: string | null
  tags?: string[]
  priceType?: 'free' | 'fixed' | 'range'
  priceAmount?: number | null
  durationMinutes?: number | null
  isIndoor?: boolean | null
}

export interface CreateVenueInput {
  name: string
  address: string
  citySlug: string
  latitude: number
  longitude: number
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
}

export interface Plan {
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
  stops: PlanStop[]
}

export interface PlanRequestInput {
  startsAt?: string | null
  endsAt?: string | null
  city: string
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
