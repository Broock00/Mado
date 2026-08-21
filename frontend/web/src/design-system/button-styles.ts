/**
 * The button's appearance, separated from the button.
 *
 * Its own module for two reasons. A download has to be an `<a>` - the browser
 * owns the progress, the cancellation and the filename the server chose in
 * `Content-Disposition`, and none of that survives being reimplemented behind
 * an onClick. Wrapping a `<button>` in an `<a>` is invalid HTML and hands a
 * screen reader two nested controls for one action, so the element stays an
 * anchor and borrows the appearance instead.
 *
 * And it lives here rather than in `primitives.tsx` because a file that exports
 * both components and plain functions breaks React Fast Refresh for everything
 * in it - the whole design system would stop hot-reloading to save one import.
 */

import { cn } from '@/lib/utils'

export type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger'
export type ButtonSize = 'sm' | 'md' | 'lg'

export const buttonVariants: Record<ButtonVariant, string> = {
  // Matches the Ask Mado launcher: darker orange, not the bright mid-tone.
  primary: 'bg-brand-700 text-white hover:bg-brand-800 active:bg-brand-900 shadow-sm',
  secondary:
    'bg-sand-100 text-sand-800 border border-sand-300 hover:bg-sand-200 active:bg-sand-300',
  ghost: 'bg-transparent text-sand-700 hover:bg-sand-200/70 active:bg-sand-300/70',
  danger: 'bg-danger text-white hover:opacity-90',
}

export const buttonSizes: Record<ButtonSize, string> = {
  // Minimum 40px tall: spec 11.03 requires large touch targets.
  sm: 'h-9 px-3 text-sm gap-1.5',
  md: 'h-10 px-4 text-sm gap-2',
  lg: 'h-12 px-6 text-base gap-2',
}

export function buttonClasses(
  variant: ButtonVariant = 'primary',
  size: ButtonSize = 'md',
  className?: string,
): string {
  return cn(
    'inline-flex items-center justify-center rounded-lg font-medium',
    'transition-colors duration-150',
    buttonVariants[variant],
    buttonSizes[size],
    className,
  )
}
