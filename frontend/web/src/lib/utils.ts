import { clsx, type ClassValue } from 'clsx'
import { twMerge } from 'tailwind-merge'

/** Merge class names, letting later Tailwind utilities win over earlier ones. */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

/**
 * Format a price for display.
 *
 * Spec 11.06 wants price legible at a glance on a card, so "Free" is a word
 * rather than a zero, and amounts drop decimals - ETB prices are whole numbers in
 * practice and the cents add noise.
 */
export function formatPrice(price: {
  type: string
  amount?: number | null
  maxAmount?: number | null
  currency: string
}): string {
  if (price.type === 'free') return 'Free'
  if (price.amount == null) return 'Price varies'
  const format = (value: number) => `${Math.round(value).toLocaleString()} ${price.currency}`
  if (price.type === 'range' && price.maxAmount != null) {
    return `${format(price.amount)}–${format(price.maxAmount)}`
  }
  return format(price.amount)
}

/**
 * Describe when something starts, relative to now.
 *
 * Spec 57.03 s16 asks for relative time: "in 40 min" is actionable in a way that
 * a timestamp is not when deciding what to do next.
 */
export function formatWhen(iso?: string | null): string | null {
  if (!iso) return null
  const start = new Date(iso)
  if (Number.isNaN(start.getTime())) return null

  const now = new Date()
  const minutes = Math.round((start.getTime() - now.getTime()) / 60000)

  if (minutes < -120) return 'Ended'
  if (minutes < 0) return 'On now'
  if (minutes < 60) return `In ${minutes} min`
  if (minutes < 60 * 10) {
    const hours = Math.floor(minutes / 60)
    return `In ${hours} hr${hours > 1 ? 's' : ''}`
  }

  const timeLabel = start.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
  const isToday = start.toDateString() === now.toDateString()
  if (isToday) return `Today ${timeLabel}`

  const tomorrow = new Date(now)
  tomorrow.setDate(now.getDate() + 1)
  if (start.toDateString() === tomorrow.toDateString()) return `Tomorrow ${timeLabel}`

  // Within the week the weekday alone locates it ("Fri 08:30 PM"); beyond that a
  // date is needed. Asking Intl for weekday *and* day without a month yields
  // "7 Fri", which reads backwards.
  const withinWeek = start.getTime() - now.getTime() < 7 * 86400000
  const datePart = withinWeek
    ? start.toLocaleDateString(undefined, { weekday: 'short' })
    : start.toLocaleDateString(undefined, { day: 'numeric', month: 'short' })
  return `${datePart} ${timeLabel}`
}

/** Distance phrased the way a person would say it. */
export function formatDistance(km?: number | null): string | null {
  if (km == null) return null
  if (km < 1) return `${Math.round(km * 1000)} m away`
  return `${km.toFixed(1)} km away`
}

/** True when a start time is close enough to warrant an urgency treatment. */
export function isStartingSoon(iso?: string | null): boolean {
  if (!iso) return false
  const minutes = (new Date(iso).getTime() - Date.now()) / 60000
  return minutes >= 0 && minutes <= 180
}
