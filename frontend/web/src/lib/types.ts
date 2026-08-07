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
  results: ConciergeResult[]
  suggestedActions: SuggestedAction[]
  clarification?: string | null
  model?: string | null
  latencyMs?: number | null
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
