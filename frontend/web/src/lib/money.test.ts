/**
 * Money at the edge of the client.
 *
 * Amounts cross the wire as integers, so the only place a rounding error can
 * get in is here - parsing what a publisher typed, and formatting what the
 * server sent. Both directions are pure functions over strings and integers,
 * and these tests exist because the obvious implementation of the first one is
 * wrong in a way that passes casual testing.
 */

import { describe, expect, it } from 'vitest'

import { money, toMajorInput, toMinor } from './money'

describe('reading a price somebody typed', () => {
  it('reads whole numbers', () => {
    expect(toMinor('150')).toBe(15000)
  })

  it('reads a decimal', () => {
    expect(toMinor('19.99')).toBe(1999)
  })

  it('does not use floating point', () => {
    // `Math.round(19.99 * 100)` happens to give 1999, but 19.99 * 100 is
    // 1998.9999999999998 and the same trick fails elsewhere. Asserting the
    // values where multiplying is visibly wrong is the point of the test.
    expect(19.99 * 100).not.toBe(1999)
    expect(toMinor('19.99')).toBe(1999)
    expect(toMinor('1.005')).toBe(100)
    expect(toMinor('4.35')).toBe(435)
    expect(toMinor('1.10')).toBe(110)
  })

  it('pads a single decimal place', () => {
    // "5.5" is five and a half birr, not five birr five santim.
    expect(toMinor('5.5')).toBe(550)
  })

  it('ignores digits below a santim rather than rounding up into one', () => {
    // Nobody can pay a fraction of a santim, and rounding up would charge more
    // than the publisher typed.
    expect(toMinor('10.999')).toBe(1099)
  })

  it('accepts a bare decimal point', () => {
    expect(toMinor('.5')).toBe(50)
  })

  it('tolerates spaces and thousands separators', () => {
    expect(toMinor(' 1,500.00 ')).toBe(150000)
  })

  it('treats nonsense as free rather than as NaN', () => {
    // A free ticket is a real thing. NaN santim is not, and it would travel to
    // the API as null and fail validation somewhere far from here.
    expect(toMinor('')).toBe(0)
    expect(toMinor('abc')).toBe(0)
    expect(Number.isNaN(toMinor('abc'))).toBe(false)
  })

  it('round-trips through the formatter', () => {
    // Written out rather than computed. An expected value derived by the same
    // kind of string manipulation as the code under test can agree with a bug.
    const cases: [string, string][] = [
      ['0', '0 ETB'],
      ['5', '5 ETB'],
      ['19.99', '19.99 ETB'],
      ['150', '150 ETB'],
      ['1250.50', '1250.50 ETB'],
      ['5.5', '5.50 ETB'],
    ]
    for (const [typed, shown] of cases) {
      expect(money(toMinor(typed), 'ETB')).toBe(shown)
    }
  })
})

describe('showing a price the server sent', () => {
  it('drops empty santim', () => {
    expect(money(15000, 'ETB')).toBe('150 ETB')
  })

  it('keeps them when there are any', () => {
    expect(money(1999, 'ETB')).toBe('19.99 ETB')
  })

  it('pads a single santim digit', () => {
    // 1005 santim is 10.05 birr, never 10.5.
    expect(money(1005, 'ETB')).toBe('10.05 ETB')
  })

  it('handles zero', () => {
    expect(money(0, 'ETB')).toBe('0 ETB')
  })

  it('carries whichever currency it was given', () => {
    // The platform is worldwide; the formatter has no opinion about birr.
    expect(money(2500, 'EUR')).toBe('25 EUR')
    expect(money(2500, 'USD')).toBe('25 USD')
  })

  it('keeps a negative amount negative', () => {
    expect(money(-1999, 'ETB')).toBe('-19.99 ETB')
  })
})

describe('currencies with no minor unit', () => {
  // The catalogue reached Tokyo and this file still assumed a hundredth of
  // everything. A 2000 yen ticket read as santim renders as "20 JPY", and a
  // publisher typing 2000 would have stored 200000 - a factor of a hundred, in
  // both directions, silently, all the way to a payment provider.
  it('renders yen whole', () => {
    expect(money(2000, 'JPY')).toBe('2000 JPY')
  })

  it('does not multiply yen into existence', () => {
    expect(toMinor('2000', 'JPY')).toBe(2000)
  })

  it('drops a fraction of a yen rather than rounding one up', () => {
    expect(toMinor('2000.7', 'JPY')).toBe(2000)
  })

  it('still treats birr as hundredths', () => {
    expect(money(2000, 'ETB')).toBe('20 ETB')
    expect(toMinor('20', 'ETB')).toBe(2000)
  })

  it('round-trips through the edit box without moving the price', () => {
    for (const [minor, currency] of [
      [1999, 'ETB'],
      [2000, 'JPY'],
      [50, 'USD'],
    ] as const) {
      expect(toMinor(toMajorInput(minor, currency), currency)).toBe(minor)
    }
  })
})
