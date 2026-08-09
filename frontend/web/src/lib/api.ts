/**
 * API client.
 *
 * One place that knows about transport concerns: auth headers, the platform's
 * error envelope, the anonymous-id header, and unwrapping `{ data }`. Feature
 * code deals in domain types and never sees a Response object.
 */

import type {
  ApiErrorBody,
  CreatePostInput,
  CreateVenueInput,
  OwnPost,
  Publisher,
  ReportReason,
  VenueCreated,
  AuthResponse,
  Category,
  City,
  CollectionEnvelope,
  ConciergeResponse,
  DiscoveryCanvas,
  Envelope,
  ExperienceDetail,
  EventInstance,
  ExperienceSummary,
  GeocodeResult,
  Me,
  UserProfile,
  LanguageOption,
  NotificationInbox,
  NotificationPreferences,
  ReviewEntry,
  ReviewsResponse,
  SavedItem,
  SearchResponse,
  Itinerary,
  MemoryEntry,
  ModerationItem,
  FeatureFlag,
  AuditEntry,
  ApiKey,
  NewApiKey,
  DeveloperScope,
  WebhookEventType,
  WebhookEndpoint,
  NewWebhookEndpoint,
  WebhookDelivery,
  Availability,
  Reservation,
  Attendee,
  TokenPair,
  AuthSession,
  PlanRoute,
  AssistSuggestions,
  PublisherAnalytics,
  PublisherReputation,
  ExplorerSummary,
  CollectionCard,
  CollectionDetail,
  CollectionVisibility,
  AdminAccount,
  PublisherVerification,
  Plan,
  PlanRequestInput,
  PrivacySettings,
} from './types'

const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? ''
const ACCESS_TOKEN_KEY = 'mado.accessToken'
const REFRESH_TOKEN_KEY = 'mado.refreshToken'
const ANONYMOUS_ID_KEY = 'mado.anonymousId'

export class ApiError extends Error {
  // Declared as fields rather than parameter properties: the project builds with
  // `erasableSyntaxOnly`, which forbids syntax that emits runtime code from a
  // type position.
  readonly code: string
  readonly status: number
  readonly details?: Record<string, unknown>
  readonly requestId?: string | null

  constructor(
    code: string,
    message: string,
    status: number,
    details?: Record<string, unknown>,
    requestId?: string | null,
  ) {
    super(message)
    this.name = 'ApiError'
    this.code = code
    this.status = status
    this.details = details
    this.requestId = requestId
  }
}

export const tokenStore = {
  get access() {
    return localStorage.getItem(ACCESS_TOKEN_KEY)
  },
  get refresh() {
    return localStorage.getItem(REFRESH_TOKEN_KEY)
  },
  set(access: string, refresh: string) {
    localStorage.setItem(ACCESS_TOKEN_KEY, access)
    localStorage.setItem(REFRESH_TOKEN_KEY, refresh)
  },
  clear() {
    localStorage.removeItem(ACCESS_TOKEN_KEY)
    localStorage.removeItem(REFRESH_TOKEN_KEY)
  },
}

/**
 * Stable id for a signed-out explorer.
 *
 * Lets guest-mode concierge threads and interaction signals accumulate before
 * registration, and migrate to the account afterwards (spec 10.01.01).
 */
export function anonymousId(): string {
  let id = localStorage.getItem(ANONYMOUS_ID_KEY)
  if (!id) {
    id = `anon_${crypto.randomUUID().replace(/-/g, '').slice(0, 24)}`
    localStorage.setItem(ANONYMOUS_ID_KEY, id)
  }
  return id
}

/**
 * The language this device is reading in, read straight from storage.
 *
 * Deliberately not imported from the React context: this module is called from
 * outside React (token refresh, retries) and reaching into a hook from here
 * would be a circular dependency between transport and rendering.
 */
function currentLanguage(): string | null {
  try {
    const value = localStorage.getItem('mado.language')
    return value === 'en' || value === 'am' ? value : null
  } catch {
    return null
  }
}

function buildHeaders(extra?: HeadersInit): Headers {
  const headers = new Headers(extra)
  headers.set('Accept', 'application/json')
  headers.set('X-Mado-Anonymous-Id', anonymousId())
  headers.set('X-Mado-Platform', 'web')
  // What this device is currently reading in. The server prefers a stated
  // profile preference over this, so it only decides anything for a signed-out
  // explorer - which is exactly the case the browser header exists for.
  const language = currentLanguage()
  if (language) headers.set('Accept-Language', language)
  const token = tokenStore.access
  if (token) headers.set('Authorization', `Bearer ${token}`)
  return headers
}

async function toApiError(response: Response): Promise<ApiError> {
  let code = 'REQUEST_FAILED'
  let message = response.statusText || 'The request failed.'
  let details: Record<string, unknown> | undefined
  let requestId: string | null | undefined

  try {
    const body = (await response.json()) as ApiErrorBody
    if (body?.error) {
      code = body.error.code ?? code
      message = body.error.message ?? message
      details = body.error.details
      requestId = body.error.requestId
    }
  } catch {
    // A non-JSON error body (a proxy 502, say) still needs to surface sensibly.
  }
  return new ApiError(code, message, response.status, details, requestId)
}

let refreshInFlight: Promise<boolean> | null = null

/**
 * Exchange the refresh token, de-duplicating concurrent attempts.
 *
 * Without the shared promise, a page issuing several requests at once would fire
 * several refreshes; since the backend rotates and revokes on use, all but the
 * first would fail and sign the explorer out.
 */
async function refreshTokens(): Promise<boolean> {
  if (refreshInFlight) return refreshInFlight

  refreshInFlight = (async () => {
    const refreshToken = tokenStore.refresh
    if (!refreshToken) return false
    try {
      const response = await fetch(`${BASE_URL}/api/v1/auth/refresh`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refreshToken }),
      })
      if (!response.ok) {
        tokenStore.clear()
        return false
      }
      const body = (await response.json()) as Envelope<{
        accessToken: string
        refreshToken: string
      }>
      tokenStore.set(body.data.accessToken, body.data.refreshToken)
      return true
    } catch {
      return false
    } finally {
      refreshInFlight = null
    }
  })()

  return refreshInFlight
}

async function request<T>(path: string, init: RequestInit = {}, retry = true): Promise<T> {
  const response = await fetch(`${BASE_URL}${path}`, {
    ...init,
    headers: buildHeaders(init.headers),
  })

  if (response.status === 401 && retry && tokenStore.refresh) {
    if (await refreshTokens()) return request<T>(path, init, false)
  }

  if (!response.ok) throw await toApiError(response)
  if (response.status === 204) return undefined as T

  return (await response.json()) as T
}

function query(params: Record<string, string | number | boolean | undefined | null>): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== '') search.set(key, String(value))
  }
  const qs = search.toString()
  return qs ? `?${qs}` : ''
}

export interface DiscoveryParams {
  city: string
  lat?: number | null
  lng?: number | null
  raining?: boolean
  limit?: number
}

export const api = {
  // ------------------------------------------------------------- discovery
  canvas: (params: DiscoveryParams) =>
    request<Envelope<DiscoveryCanvas>>(`/api/v1/discover${query({ ...params })}`).then(
      (r) => r.data,
    ),

  rail: (key: 'now' | 'tonight' | 'weekend' | 'trending' | 'nearby', params: DiscoveryParams) =>
    request<CollectionEnvelope<ExperienceSummary>>(
      `/api/v1/discover/${key}${query({ ...params })}`,
    ).then((r) => r.data),

  forYou: (params: DiscoveryParams) =>
    request<CollectionEnvelope<ExperienceSummary>>(
      `/api/v1/recommendations/for-you${query({ ...params })}`,
    ).then((r) => r.data),

  search: (
    q: string,
    params: DiscoveryParams & { category?: string[]; free?: boolean; type?: string },
  ) => {
    const { category, ...rest } = params
    const categories = (category ?? []).map((c) => `&category=${encodeURIComponent(c)}`).join('')
    return request<Envelope<SearchResponse>>(
      `/api/v1/search${query({ q, ...rest })}${categories}`,
    ).then((r) => r.data)
  },

  suggestions: (q: string) =>
    request<CollectionEnvelope<{ id: string; title: string; type: string; category: string }>>(
      `/api/v1/search/suggestions${query({ q, limit: 8 })}`,
    ).then((r) => r.data),

  // --------------------------------------------------------------- catalog
  cities: () => request<CollectionEnvelope<City>>('/api/v1/cities').then((r) => r.data),

  categories: () =>
    request<CollectionEnvelope<Category>>('/api/v1/categories').then((r) => r.data),

  experience: (id: string, params?: { lat?: number | null; lng?: number | null }) =>
    request<Envelope<ExperienceDetail>>(`/api/v1/experiences/${id}${query({ ...params })}`).then(
      (r) => r.data,
    ),

  similar: (id: string) =>
    request<CollectionEnvelope<ExperienceSummary>>(
      `/api/v1/experiences/${id}/similar${query({ limit: 8 })}`,
    ).then((r) => r.data),

  experiences: (params: DiscoveryParams & { category?: string[]; free?: boolean }) => {
    const { category, ...rest } = params
    const categories = (category ?? []).map((c) => `&category=${encodeURIComponent(c)}`).join('')
    return request<CollectionEnvelope<ExperienceSummary>>(
      `/api/v1/experiences${query({ ...rest })}${categories}`,
    ).then((r) => r.data)
  },

  // ------------------------------------------------------------ explorer
  me: () => request<Envelope<Me>>('/api/v1/me').then((r) => r.data),

  updateProfile: (patch: {
    displayName?: string
    bio?: string
    language?: string
    timezone?: string
    homeCitySlug?: string
  }) =>
    request<Envelope<UserProfile>>('/api/v1/me', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    }).then((r) => r.data),

  /** Public. Each name is in its own language. */
  languages: () =>
    request<CollectionEnvelope<LanguageOption>>('/api/v1/me/languages').then((r) => r.data),

  savedItems: () =>
    request<CollectionEnvelope<SavedItem>>('/api/v1/me/saved').then((r) => r.data),

  save: (entityType: string, entityId: string) =>
    request<Envelope<SavedItem>>(`/api/v1/me/saved/${entityType}/${entityId}`, {
      method: 'POST',
    }).then((r) => r.data),

  unsave: (entityType: string, entityId: string) =>
    request<void>(`/api/v1/me/saved/${entityType}/${entityId}`, { method: 'DELETE' }),

  updatePreferences: (preferences: Record<string, unknown>) =>
    request<Envelope<Record<string, unknown>>>('/api/v1/me/preferences', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(preferences),
    }).then((r) => r.data),

  feedback: (experienceId: string, action: string) =>
    request<Envelope<{ recorded: boolean }>>(
      `/api/v1/recommendations/${experienceId}/feedback${query({ action })}`,
      { method: 'POST' },
    ).then((r) => r.data),

  // ---------------------------------------------------------------- auth
  register: (payload: {
    email: string
    password: string
    displayName: string
  }) =>
    request<Envelope<AuthResponse>>('/api/v1/auth/register', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...payload, anonymousId: anonymousId() }),
    }).then((r) => r.data),

  login: (payload: { email: string; password: string }) =>
    request<Envelope<AuthResponse>>('/api/v1/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }).then((r) => r.data),

  logout: async () => {
    const refreshToken = tokenStore.refresh
    if (refreshToken) {
      await request<void>('/api/v1/auth/logout', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refreshToken }),
      }).catch(() => undefined)
    }
    tokenStore.clear()
  },

  // --------------------------------------------- email and password flows
  sendVerificationEmail: () =>
    request<Envelope<{ sent: boolean }>>('/api/v1/auth/verify-email/send', {
      method: 'POST',
    }).then((r) => r.data),

  confirmEmail: (token: string) =>
    request<Envelope<Me>>('/api/v1/auth/verify-email/confirm', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token }),
    }).then((r) => r.data),

  forgotPassword: (email: string) =>
    request<Envelope<{ sent: boolean }>>('/api/v1/auth/password/forgot', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email }),
    }).then((r) => r.data),

  resetPassword: (token: string, password: string) =>
    request<Envelope<AuthResponse>>('/api/v1/auth/password/reset', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token, password }),
    }).then((r) => r.data),

  changePassword: (currentPassword: string, password: string) =>
    request<Envelope<TokenPair>>('/api/v1/auth/password/change', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ currentPassword, password }),
    }).then((r) => r.data),

  // ----------------------------------------------------------- sessions
  sessions: () =>
    request<CollectionEnvelope<AuthSession>>('/api/v1/auth/sessions').then((r) => r.data),

  revokeSession: (sessionId: string) =>
    request<void>(`/api/v1/auth/sessions/${sessionId}`, { method: 'DELETE' }),

  // --------------------------------------------------------- publishing
  myPublisher: () =>
    request<Envelope<Publisher>>('/api/v1/posts/me').then((r) => r.data),

  myPosts: (status?: string) =>
    request<CollectionEnvelope<OwnPost>>(`/api/v1/posts${query({ status })}`).then((r) => r.data),

  myPost: (id: string) =>
    request<Envelope<OwnPost>>(`/api/v1/posts/${id}`).then((r) => r.data),

  createPost: (input: CreatePostInput) =>
    request<Envelope<OwnPost>>('/api/v1/posts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(input),
    }).then((r) => r.data),

  updatePost: (id: string, input: Partial<CreatePostInput>) =>
    request<Envelope<OwnPost>>(`/api/v1/posts/${id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(input),
    }).then((r) => r.data),

  postAction: (id: string, action: 'publish' | 'unpublish' | 'archive' | 'restore') =>
    request<Envelope<OwnPost>>(`/api/v1/posts/${id}/${action}`, { method: 'POST' }).then(
      (r) => r.data,
    ),

  addPostDate: (id: string, startTime: string, capacity?: number | null) =>
    request<Envelope<EventInstance>>(`/api/v1/posts/${id}/events`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ startTime, capacity }),
    }).then((r) => r.data),

  /**
   * Upload an image file.
   *
   * No Content-Type header is set: the browser has to generate the multipart
   * boundary itself, and supplying the header without it produces a request the
   * server cannot parse.
   */
  uploadPostImage: (id: string, file: File, altText?: string) => {
    const body = new FormData()
    body.append('file', file)
    if (altText) body.append('alt_text', altText)
    return request<Envelope<OwnPost>>(`/api/v1/posts/${id}/media/upload`, {
      method: 'POST',
      body,
    }).then((r) => r.data)
  },

  addPostImage: (id: string, url: string, altText?: string) =>
    request<Envelope<OwnPost>>(`/api/v1/posts/${id}/media`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url, altText }),
    }).then((r) => r.data),

  /**
   * Describe a dropped pin. Never throws for a missing label - the coordinates
   * are already chosen and a name for them is confirmation, not the answer.
   */
  locate: (latitude: number, longitude: number) =>
    request<Envelope<{ latitude: number; longitude: number; label: string | null }>>(
      '/api/v1/posts/venues/locate',
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ latitude, longitude }),
      },
    ).then((r) => r.data),

  /** Move the map to a searched place. The publisher still confirms the pin. */
  geocode: (address: string, citySlug: string) =>
    request<Envelope<GeocodeResult>>('/api/v1/posts/venues/geocode', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ address, citySlug }),
    }).then((r) => r.data),

  createVenue: (input: CreateVenueInput) =>
    request<Envelope<VenueCreated>>('/api/v1/posts/venues', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(input),
    }).then((r) => r.data),

  reportExperience: (experienceId: string, reason: ReportReason, detail?: string) =>
    request<Envelope<{ id: string }>>(`/api/v1/experiences/${experienceId}/report`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ reason, detail }),
    }).then((r) => r.data),

  // ----------------------------------------------------------- concierge
  concierge: (payload: {
    message: string
    conversationId?: string | null
    city: string
    latitude?: number | null
    longitude?: number | null
  }) =>
    request<Envelope<ConciergeResponse>>('/api/v1/assistant/messages', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }).then((r) => r.data),

  /**
   * Keep the plan the concierge offered, exactly as it was shown.
   *
   * Takes no plan body: the server saves what it stored against the
   * conversation, so an explorer saying "yes, that one" gets the evening they
   * were shown rather than something recomputed since.
   */
  acceptPlan: (conversationId: string, title?: string) =>
    request<Envelope<{ id: string; title: string }>>(
      `/api/v1/assistant/conversations/${conversationId}/plan`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title }),
      },
    ).then((r) => r.data),

  // --------------------------------------------------------------- reviews
  reviews: (experienceId: string) =>
    request<Envelope<ReviewsResponse>>(`/api/v1/experiences/${experienceId}/reviews`).then(
      (r) => r.data,
    ),

  /** PUT, not POST: one review per person, so sending a second replaces it. */
  leaveReview: (experienceId: string, rating: number, comment?: string) =>
    request<Envelope<ReviewEntry>>(`/api/v1/experiences/${experienceId}/reviews`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ rating, comment }),
    }).then((r) => r.data),

  withdrawReview: (experienceId: string) =>
    request<void>(`/api/v1/experiences/${experienceId}/reviews`, { method: 'DELETE' }),

  // ------------------------------------------------------------- planning
  // Planning and saving are separate calls because most plans are looked at
  // once and discarded - an explorer asks for an evening, dislikes it, asks
  // again. Only the ones worth keeping become itineraries.
  plan: (input: PlanRequestInput) =>
    request<Envelope<Plan>>('/api/v1/plans', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(input),
    }).then((r) => r.data),

  saveItinerary: (input: PlanRequestInput & { title: string }) =>
    request<Envelope<Itinerary>>('/api/v1/itineraries', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(input),
    }).then((r) => r.data),

  itineraries: () =>
    request<CollectionEnvelope<Itinerary>>('/api/v1/itineraries').then((r) => r.data),

  itinerary: (id: string) =>
    request<Envelope<Itinerary>>(`/api/v1/itineraries/${id}`).then((r) => r.data),

  deleteItinerary: (id: string) =>
    request<void>(`/api/v1/itineraries/${id}`, { method: 'DELETE' }),

  // -------------------------------------------------------- notifications
  notifications: () =>
    request<Envelope<NotificationInbox>>('/api/v1/notifications').then((r) => r.data),

  markNotificationRead: (id: string) =>
    request<void>(`/api/v1/notifications/${id}/read`, { method: 'POST' }),

  markAllNotificationsRead: () =>
    request<void>('/api/v1/notifications/read-all', { method: 'POST' }),

  notificationPreferences: () =>
    request<Envelope<NotificationPreferences>>('/api/v1/notifications/preferences').then(
      (r) => r.data,
    ),

  updateNotificationPreferences: (kinds: Record<string, boolean>) =>
    request<Envelope<NotificationPreferences>>('/api/v1/notifications/preferences', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ kinds }),
    }).then((r) => r.data),

  // --------------------------------------------------------------- memory
  memories: () =>
    request<CollectionEnvelope<MemoryEntry>>('/api/v1/assistant/memory').then((r) => r.data),

  forgetMemory: (id: string) =>
    request<void>(`/api/v1/assistant/memory/${id}`, { method: 'DELETE' }),

  forgetAllMemories: () => request<void>('/api/v1/assistant/memory', { method: 'DELETE' }),

  // -------------------------------------------------------------- privacy
  updatePrivacy: (settings: Partial<PrivacySettings>) =>
    request<Envelope<Me>>('/api/v1/me/privacy', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(settings),
    }).then((r) => r.data),

  exportMyData: () => request<Envelope<Record<string, unknown>>>('/api/v1/me/export').then((r) => r.data),

  // ----------------------------------------------------------- moderation
  moderationQueue: () =>
    request<CollectionEnvelope<ModerationItem>>('/api/v1/moderation/queue').then((r) => r.data),

  decideModeration: (experienceId: string, approve: boolean, note?: string) =>
    request<Envelope<ModerationItem>>(`/api/v1/moderation/${experienceId}/decide`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ approve, note }),
    }).then((r) => r.data),

  assistDraft: (payload: { title: string; description: string; summary?: string }) =>
    request<Envelope<AssistSuggestions>>('/api/v1/posts/assist', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }).then((r) => r.data),

  itineraryRoute: (itineraryId: string, mode?: 'walk' | 'drive') =>
    request<Envelope<PlanRoute>>(
      `/api/v1/itineraries/${itineraryId}/route${query({ mode })}`,
    ).then((r) => r.data),

  // ------------------------------------------------------- reservations
  availability: (occurrenceId: string) =>
    request<Envelope<Availability>>(`/api/v1/events/${occurrenceId}/availability`).then(
      (r) => r.data,
    ),

  reserve: (occurrenceId: string, partySize: number, note?: string) =>
    request<Envelope<Reservation>>(`/api/v1/events/${occurrenceId}/reserve`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ partySize, note }),
    }).then((r) => r.data),

  myReservations: () =>
    request<CollectionEnvelope<Reservation>>('/api/v1/reservations').then((r) => r.data),

  changePartySize: (reservationId: string, partySize: number) =>
    request<Envelope<Reservation>>(`/api/v1/reservations/${reservationId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ partySize }),
    }).then((r) => r.data),

  cancelReservation: (reservationId: string) =>
    request<Envelope<Reservation>>(`/api/v1/reservations/${reservationId}`, {
      method: 'DELETE',
    }).then((r) => r.data),

  attendees: (occurrenceId: string) =>
    request<CollectionEnvelope<Attendee>>(
      `/api/v1/posts/events/${occurrenceId}/attendees`,
    ).then((r) => r.data),

  // ---------------------------------------------------------- analytics
  /** Yours only. There is no endpoint for another publisher's standing. */
  myReputation: () =>
    request<Envelope<PublisherReputation>>('/api/v1/analytics/reputation').then((r) => r.data),

  publisherAnalytics: (windowDays: number) =>
    request<Envelope<PublisherAnalytics>>(
      `/api/v1/analytics/publisher${query({ window: windowDays })}`,
    ).then((r) => r.data),

  myActivity: () =>
    request<Envelope<ExplorerSummary>>('/api/v1/analytics/me').then((r) => r.data),

  // -------------------------------------------------------- collections
  publicCollections: (city?: string) =>
    request<CollectionEnvelope<CollectionCard>>(
      `/api/v1/collections${query({ city })}`,
    ).then((r) => r.data),

  myCollections: () =>
    request<CollectionEnvelope<CollectionCard>>('/api/v1/collections/mine').then((r) => r.data),

  collection: (collectionId: string) =>
    request<Envelope<CollectionDetail>>(`/api/v1/collections/${collectionId}`).then((r) => r.data),

  collectionBySlug: (slug: string) =>
    request<Envelope<CollectionDetail>>(`/api/v1/collections/by-slug/${slug}`).then(
      (r) => r.data,
    ),

  createCollection: (payload: {
    title: string
    description?: string
    citySlug?: string
    visibility?: CollectionVisibility
  }) =>
    request<Envelope<CollectionDetail>>('/api/v1/collections', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }).then((r) => r.data),

  updateCollection: (
    collectionId: string,
    payload: { title?: string; description?: string; visibility?: CollectionVisibility },
  ) =>
    request<Envelope<CollectionDetail>>(`/api/v1/collections/${collectionId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }).then((r) => r.data),

  deleteCollection: (collectionId: string) =>
    request<void>(`/api/v1/collections/${collectionId}`, { method: 'DELETE' }),

  addToCollection: (collectionId: string, experienceId: string, note?: string) =>
    request<Envelope<CollectionDetail>>(`/api/v1/collections/${collectionId}/items`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ experienceId, note }),
    }).then((r) => r.data),

  removeFromCollection: (collectionId: string, experienceId: string) =>
    request<void>(`/api/v1/collections/${collectionId}/items/${experienceId}`, {
      method: 'DELETE',
    }),

  annotateCollectionItem: (collectionId: string, experienceId: string, note: string | null) =>
    request<Envelope<CollectionDetail>>(
      `/api/v1/collections/${collectionId}/items/${experienceId}/note`,
      {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ note }),
      },
    ).then((r) => r.data),

  reorderCollection: (collectionId: string, experienceIds: string[]) =>
    request<Envelope<CollectionDetail>>(`/api/v1/collections/${collectionId}/order`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ experienceIds }),
    }).then((r) => r.data),

  // ------------------------------------------------- flags and the record
  /** Resolved for the current explorer - never the definitions. */
  myFlags: () => request<Envelope<Record<string, boolean>>>('/api/v1/me/flags').then((r) => r.data),

  flags: () => request<CollectionEnvelope<FeatureFlag>>('/api/v1/flags').then((r) => r.data),

  createFlag: (key: string, description: string) =>
    request<Envelope<FeatureFlag>>('/api/v1/flags', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key, description }),
    }).then((r) => r.data),

  updateFlag: (key: string, patch: { enabled?: boolean; rolloutPercentage?: number }) =>
    request<Envelope<FeatureFlag>>(`/api/v1/flags/${key}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    }).then((r) => r.data),

  auditTrail: (days = 30) =>
    request<CollectionEnvelope<AuditEntry>>(`/api/v1/admin/audit${query({ days })}`).then(
      (r) => r.data,
    ),

  // ---------------------------------------------------- developer platform
  developerScopes: () =>
    request<CollectionEnvelope<DeveloperScope>>('/api/v1/developer/scopes').then((r) => r.data),

  webhookEventTypes: () =>
    request<CollectionEnvelope<WebhookEventType>>('/api/v1/developer/event-types').then(
      (r) => r.data,
    ),

  apiKeys: () =>
    request<CollectionEnvelope<ApiKey>>('/api/v1/developer/keys').then((r) => r.data),

  /** The only call that ever returns the key itself. Show it, then lose it. */
  createApiKey: (body: { name: string; scopes: string[]; expiresInDays?: number }) =>
    request<Envelope<NewApiKey>>('/api/v1/developer/keys', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }).then((r) => r.data),

  revokeApiKey: (keyId: string) =>
    request<Envelope<ApiKey>>(`/api/v1/developer/keys/${keyId}`, { method: 'DELETE' }).then(
      (r) => r.data,
    ),

  webhooks: () =>
    request<CollectionEnvelope<WebhookEndpoint>>('/api/v1/webhooks').then((r) => r.data),

  createWebhook: (url: string, events: string[]) =>
    request<Envelope<NewWebhookEndpoint>>('/api/v1/webhooks', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url, events }),
    }).then((r) => r.data),

  updateWebhook: (
    endpointId: string,
    patch: { url?: string; events?: string[]; status?: string },
  ) =>
    request<Envelope<WebhookEndpoint>>(`/api/v1/webhooks/${endpointId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    }).then((r) => r.data),

  deleteWebhook: (endpointId: string) =>
    request<void>(`/api/v1/webhooks/${endpointId}`, { method: 'DELETE' }),

  rotateWebhookSecret: (endpointId: string) =>
    request<Envelope<NewWebhookEndpoint>>(`/api/v1/webhooks/${endpointId}/rotate-secret`, {
      method: 'POST',
    }).then((r) => r.data),

  webhookDeliveries: (endpointId: string) =>
    request<CollectionEnvelope<WebhookDelivery>>(
      `/api/v1/webhooks/${endpointId}/deliveries`,
    ).then((r) => r.data),

  retryWebhookDelivery: (endpointId: string, deliveryId: string) =>
    request<Envelope<WebhookDelivery>>(
      `/api/v1/webhooks/${endpointId}/deliveries/${deliveryId}/retry`,
      { method: 'POST' },
    ).then((r) => r.data),

  testWebhook: (endpointId: string) =>
    request<Envelope<WebhookDelivery>>(`/api/v1/webhooks/${endpointId}/test`, {
      method: 'POST',
    }).then((r) => r.data),

  // ------------------------------------------------------- administration
  // Search rather than browse: an administrator looking for a specific person
  // should search for them, and paginating the whole user table is not a
  // workflow worth building.
  adminAccounts: (query: string, status?: string) =>
    request<CollectionEnvelope<AdminAccount>>(
      `/api/v1/admin/accounts?${new URLSearchParams({
        ...(query ? { q: query } : {}),
        ...(status ? { status } : {}),
      })}`,
    ).then((r) => r.data),

  setAccountSuspended: (userId: string, suspended: boolean, reason?: string) =>
    request<Envelope<AdminAccount>>(`/api/v1/admin/accounts/${userId}/suspend`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ suspended, reason }),
    }).then((r) => r.data),

  setAccountModerator: (userId: string, moderator: boolean) =>
    request<Envelope<AdminAccount>>(`/api/v1/admin/accounts/${userId}/moderator`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ moderator }),
    }).then((r) => r.data),

  // ------------------------------------------------------- verification
  requestVerification: (note?: string) =>
    request<Envelope<PublisherVerification>>('/api/v1/posts/verification', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ note }),
    }).then((r) => r.data),

  myVerification: () =>
    request<Envelope<PublisherVerification>>('/api/v1/posts/verification').then((r) => r.data),

  pendingVerifications: () =>
    request<CollectionEnvelope<PublisherVerification>>(
      '/api/v1/moderation/verifications',
    ).then((r) => r.data),

  decideVerification: (publisherId: string, approve: boolean, note?: string) =>
    request<Envelope<PublisherVerification>>(
      `/api/v1/moderation/verifications/${publisherId}`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ approve, note }),
      },
    ).then((r) => r.data),
}

export { BASE_URL }
