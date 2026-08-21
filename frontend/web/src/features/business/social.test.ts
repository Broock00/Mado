/**
 * The rules here are the server's rules, restated so the dashboard can say what
 * is wrong before it posts rather than after the value has been dropped. The
 * cases mirror `test_business.py`; if one side changes, these are the pair that
 * should change with it.
 */

import { describe, expect, it } from 'vitest'

import { SOCIAL_PLATFORMS, listedSocials, normaliseSocialUrl } from './social'

describe('normaliseSocialUrl', () => {
  it('keeps an address that already has a scheme', () => {
    expect(normaliseSocialUrl('https://instagram.com/mado')).toBe('https://instagram.com/mado')
    expect(normaliseSocialUrl('  http://t.me/mado  ')).toBe('http://t.me/mado')
  })

  it('completes what people actually paste', () => {
    expect(normaliseSocialUrl('instagram.com/mado')).toBe('https://instagram.com/mado')
    expect(normaliseSocialUrl('t.me/mado')).toBe('https://t.me/mado')
  })

  it('refuses a scheme it will not render', () => {
    // Completing this would produce `https://javascript:alert(1)`; storing the
    // original is the XSS, since these are rendered as anchors.
    expect(normaliseSocialUrl('javascript:alert(1)')).toBeNull()
    expect(normaliseSocialUrl('data:text/html,<script>')).toBeNull()
  })

  it('refuses a handle, which would complete into a link that opens nothing', () => {
    expect(normaliseSocialUrl('@mado')).toBeNull()
    expect(normaliseSocialUrl('mado')).toBeNull()
  })

  it('treats blank as nothing listed rather than as a value', () => {
    expect(normaliseSocialUrl('   ')).toBeNull()
  })
})

describe('listedSocials', () => {
  it('returns only what was listed, in display order', () => {
    const listed = listedSocials({ telegram: 'https://t.me/mado', instagram: 'https://ig/mado' })
    expect(listed.map((entry) => entry.platform.value)).toEqual(['instagram', 'telegram'])
  })

  it('reads a cleared link and an absent one the same way', () => {
    expect(listedSocials({ x: '   ' })).toEqual([])
    expect(listedSocials(null)).toEqual([])
  })

  it('has a mark for every platform, since the link is shown as the mark alone', () => {
    for (const platform of SOCIAL_PLATFORMS) {
      expect(platform.path.length).toBeGreaterThan(0)
      expect(platform.label).toBeTruthy()
    }
  })
})
