/**
 * What the client puts on the wire, rather than what the server does with it.
 *
 * One test, for one bug that the backend suite cannot see by construction: a
 * JSON body sent without `Content-Type: application/json` is labelled
 * `text/plain` by `fetch`, and FastAPI rejects it with a bare "The request
 * failed validation" naming no field — because the body was never read.
 *
 * It was invisible for two reasons at once. Every call site set the header by
 * hand, so the one that forgot looked like all the others; and every backend
 * test drives the API through httpx's `json=`, which sets the header itself. The
 * failure existed only in a browser.
 *
 * So the header is set once, in `buildHeaders`, and this is the test that says
 * so — including the case that must *not* get it, because a FormData upload
 * needs the multipart boundary the browser generates.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { api } from './api'

function headersOf(call: unknown): Headers {
  const [, init] = call as [string, RequestInit]
  return new Headers(init.headers)
}

describe('what the client sends', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(JSON.stringify({ data: {} }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    )
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('labels a JSON body as JSON', async () => {
    await api.startSubscription('a-business', 'professional', 'ETB')

    const fetchMock = fetch as unknown as ReturnType<typeof vi.fn>
    expect(headersOf(fetchMock.mock.calls[0]).get('Content-Type')).toBe('application/json')
  })

  it('leaves a multipart upload alone', async () => {
    // Setting a Content-Type by hand here would omit the boundary, and the
    // server cannot parse the body without it.
    await api.visualSearch(new File(['x'], 'photo.jpg', { type: 'image/jpeg' }))

    const fetchMock = fetch as unknown as ReturnType<typeof vi.fn>
    expect(headersOf(fetchMock.mock.calls[0]).has('Content-Type')).toBe(false)
  })

  it('sends nothing extra on a request with no body', async () => {
    await api.cancelSubscription('a-business')

    const fetchMock = fetch as unknown as ReturnType<typeof vi.fn>
    expect(headersOf(fetchMock.mock.calls[0]).has('Content-Type')).toBe(false)
  })
})
