/**
 * Formatting money that arrived as an integer.
 *
 * Amounts cross the wire in minor units - santim, not birr - because a price
 * that goes anywhere near a float eventually acquires a rounding error, and
 * the place it surfaces is somebody's total. So the conversion to something
 * readable happens once, here, at the very edge, and by integer arithmetic
 * rather than by dividing.
 *
 * Its own module rather than living beside the component that uses it: a file
 * exporting both components and plain functions breaks React Fast Refresh for
 * everything in it.
 */

/**
 * How many minor units make one of the currency, mirroring `MINOR_UNITS` in
 * `app/integrations/payments.py`.
 *
 * This file assumed a hundred everywhere, which was true while the catalogue
 * was one city and stopped being true the moment it was not. Yen has no minor
 * unit at all: a ¥2000 ticket read as 2000 santim renders as "20 JPY" on the
 * card, and a publisher typing 2000 into the box would have created a ¥200,000
 * ticket. Both halves are silent, and both reach a payment provider.
 */
const MINOR_UNITS: Record<string, number> = {
  JPY: 1,
  KRW: 1,
  VND: 1,
  UGX: 1,
  RWF: 1,
}
const DEFAULT_MINOR_UNITS = 100

function factorFor(currency: string): number {
  return MINOR_UNITS[currency.toUpperCase()] ?? DEFAULT_MINOR_UNITS
}

export function money(minor: number, currency: string): string {
  const negative = minor < 0
  const absolute = Math.abs(minor)
  const factor = factorFor(currency)
  if (factor === 1) {
    return `${negative ? '-' : ''}${absolute} ${currency}`
  }
  const major = Math.trunc(absolute / factor)
  const remainder = absolute % factor
  const amount = remainder ? `${major}.${String(remainder).padStart(2, '0')}` : String(major)
  return `${negative ? '-' : ''}${amount} ${currency}`
}

/**
 * An amount typed into a box, to minor units.
 *
 * By string, not by `Math.round(value * 100)`. In IEEE 754, `19.99 * 100` is
 * 1998.9999999999998 and `1.005 * 100` is 100.49999999999999 - rounding hides
 * it for most values and not for all of them, which is the worst way for a
 * money bug to behave. Reading the digits either side of the point cannot
 * drift.
 *
 * Anything unparseable is zero rather than NaN: a free ticket is a real thing
 * and `NaN` santim is not.
 *
 * In a zero-decimal currency the digits after a point are dropped rather than
 * rounded up: somebody typing 2000.7 yen means 2000, and inventing a unit that
 * does not exist to round into is worse than ignoring it.
 */
export function toMinor(input: string, currency = 'ETB'): number {
  const cleaned = input.trim().replace(/[, ]/g, '')
  const match = /^(-?)(\d*)(?:[.](\d{0,2}))?\d*$/.exec(cleaned)
  if (!match) return 0
  const [, sign, whole, fraction = ''] = match
  const minor =
    factorFor(currency) === 1
      ? Number(whole || '0')
      : Number(whole || '0') * 100 + Number(fraction.padEnd(2, '0') || '0')
  return sign === '-' ? -minor : minor
}

/**
 * Minor units back to what somebody would type, for an edit box.
 *
 * Not for display - `money` is for that. This is the inverse of `toMinor`, so
 * loading a tier into a form and saving it unchanged cannot alter the price.
 */
export function toMajorInput(minor: number, currency: string): string {
  const factor = factorFor(currency)
  if (factor === 1) return String(minor)
  const major = Math.trunc(minor / factor)
  const remainder = minor % factor
  return remainder ? `${major}.${String(remainder).padStart(2, '0')}` : String(major)
}
