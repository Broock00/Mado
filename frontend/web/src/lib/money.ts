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

export function money(minor: number, currency: string): string {
  const negative = minor < 0
  const absolute = Math.abs(minor)
  const major = Math.trunc(absolute / 100)
  const remainder = absolute % 100
  const amount = remainder ? `${major}.${String(remainder).padStart(2, '0')}` : String(major)
  return `${negative ? '-' : ''}${amount} ${currency}`
}

/**
 * Birr typed into a box, to santim.
 *
 * By string, not by `Math.round(value * 100)`. In IEEE 754, `19.99 * 100` is
 * 1998.9999999999998 and `1.005 * 100` is 100.49999999999999 - rounding hides
 * it for most values and not for all of them, which is the worst way for a
 * money bug to behave. Reading the digits either side of the point cannot
 * drift.
 *
 * Anything unparseable is zero rather than NaN: a free ticket is a real thing
 * and `NaN` santim is not.
 */
export function toMinor(input: string): number {
  const cleaned = input.trim().replace(/[, ]/g, '')
  const match = /^(-?)(\d*)(?:[.](\d{0,2}))?\d*$/.exec(cleaned)
  if (!match) return 0
  const [, sign, whole, fraction = ''] = match
  const minor = Number(whole || '0') * 100 + Number(fraction.padEnd(2, '0') || '0')
  return sign === '-' ? -minor : minor
}
