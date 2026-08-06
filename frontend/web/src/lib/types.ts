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

export interface ConciergeResponse {
  conversationId: string
  message: string
  intent: string
  confidence: number
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
