/**
 * Design system primitives (spec 80.06 s10 component hierarchy).
 *
 * Foundation-level components only. Feature code composes these rather than
 * writing bare elements, which is what keeps spacing, radius, focus treatment and
 * contrast consistent across surfaces.
 */

import type { ButtonHTMLAttributes, HTMLAttributes, InputHTMLAttributes, ReactNode } from 'react'
import { forwardRef } from 'react'
import { cn } from '@/lib/utils'
import { buttonSizes, buttonVariants } from './button-styles'
import type { ButtonSize, ButtonVariant } from './button-styles'

/* -------------------------------------------------------------------- Button */

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant
  size?: ButtonSize
  loading?: boolean
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { className, variant = 'primary', size = 'md', loading, disabled, children, ...props },
  ref,
) {
  return (
    <button
      ref={ref}
      disabled={disabled || loading}
      // aria-busy lets assistive tech announce the pending state, which a spinner
      // alone does not communicate.
      aria-busy={loading || undefined}
      className={cn(
        'inline-flex items-center justify-center rounded-lg font-medium',
        'transition-colors duration-150',
        'disabled:cursor-not-allowed disabled:opacity-50',
        buttonVariants[variant],
        buttonSizes[size],
        className,
      )}
      {...props}
    >
      {loading && (
        <span
          aria-hidden
          className="size-4 animate-spin rounded-full border-2 border-current border-t-transparent"
        />
      )}
      {children}
    </button>
  )
})

/* ---------------------------------------------------------------------- Badge */

type BadgeTone = 'neutral' | 'brand' | 'accent' | 'success' | 'ai' | 'danger'

const badgeTones: Record<BadgeTone, string> = {
  neutral: 'bg-sand-200 text-sand-700',
  brand: 'bg-brand-100 text-brand-800',
  accent: 'bg-accent-100 text-accent-700',
  success: 'bg-brand-100 text-brand-800',
  ai: 'bg-[var(--color-ai-soft)] text-[var(--color-ai)]',
  danger: 'bg-red-950 text-red-300',
}

export function Badge({
  tone = 'neutral',
  icon,
  children,
  className,
}: {
  tone?: BadgeTone
  icon?: ReactNode
  children: ReactNode
  className?: string
}) {
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1 rounded-pill px-2 py-0.5 text-xs font-medium',
        badgeTones[tone],
        className,
      )}
    >
      {icon}
      {children}
    </span>
  )
}

/* ----------------------------------------------------------------------- Card */

export function Card({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn(
        'rounded-card border border-sand-200 bg-sand-100 shadow-card',
        className,
      )}
      {...props}
    />
  )
}

/* ---------------------------------------------------------------------- Input */

export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(
  function Input({ className, ...props }, ref) {
    return (
      <input
        ref={ref}
        className={cn(
          'h-11 w-full rounded-lg border border-sand-300 bg-sand-100 px-3.5 text-sm text-sand-900',
          'placeholder:text-sand-400',
          'focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-500/25',
          'disabled:bg-sand-200 disabled:text-sand-400',
          className,
        )}
        {...props}
      />
    )
  },
)

/* -------------------------------------------------------------------- Skeleton */

/**
 * Loading placeholder.
 *
 * Spec 11.06 "Loading States" prefers skeletons over spinners: they preserve
 * layout, so content does not jump when it arrives.
 */
export function Skeleton({ className }: { className?: string }) {
  return <div className={cn('animate-pulse rounded-md bg-sand-200', className)} aria-hidden />
}

/* ------------------------------------------------------------------ EmptyState */

export function EmptyState({
  icon,
  title,
  description,
  action,
}: {
  icon?: ReactNode
  title: string
  description?: string
  action?: ReactNode
}) {
  return (
    // Spec 11.06: empty states should feel encouraging, not broken - so each one
    // carries an explanation and a way forward.
    <div className="flex flex-col items-center justify-center rounded-card border border-dashed border-sand-300 px-6 py-12 text-center">
      {icon && <div className="mb-3 text-sand-400">{icon}</div>}
      <p className="text-base font-medium text-sand-800">{title}</p>
      {description && <p className="mt-1.5 max-w-sm text-sm text-sand-500">{description}</p>}
      {action && <div className="mt-5">{action}</div>}
    </div>
  )
}

/* ----------------------------------------------------------------- SectionHead */

export function SectionHeading({
  title,
  subtitle,
  action,
}: {
  title: string
  subtitle?: string | null
  action?: ReactNode
}) {
  return (
    <div className="mb-3 flex items-end justify-between gap-4">
      <div className="min-w-0">
        <h2 className="text-lg font-semibold tracking-tight text-sand-900">{title}</h2>
        {subtitle && <p className="mt-0.5 text-sm text-sand-500">{subtitle}</p>}
      </div>
      {action}
    </div>
  )
}
