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
