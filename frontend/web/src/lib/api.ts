/**
 * API client.
 *
 * One place that knows about transport concerns: auth headers, the platform's
 * error envelope, the anonymous-id header, and unwrapping `{ data }`. Feature
 * code deals in domain types and never sees a Response object.
 */

import type {
  ApiErrorBody,
  AuthResponse,
  Category,
  City,
  CollectionEnvelope,
  ConciergeResponse,
  DiscoveryCanvas,
  Envelope,
  ExperienceDetail,
  ExperienceSummary,
  Me,
  SavedItem,
  SearchResponse,
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

function buildHeaders(extra?: HeadersInit): Headers {
  const headers = new Headers(extra)
  headers.set('Accept', 'application/json')
  headers.set('X-Mado-Anonymous-Id', anonymousId())
  headers.set('X-Mado-Platform', 'web')
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
}

export { BASE_URL }
