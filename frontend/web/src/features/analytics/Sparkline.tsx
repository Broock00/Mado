/**
 * A small inline chart, drawn as SVG.
 *
 * No charting library. This draws two polylines over a fixed viewBox; pulling
 * in a few hundred kilobytes of chart engine to do that would cost every
 * explorer who never opens the dashboard.
 *
 * The y-axis always starts at zero. Charts that crop the axis to the data
 * make a flat fortnight look like a rocket, which on a dashboard somebody uses
 * to judge their own work is not a stylistic choice - it is a false statement.
 */

import { useId } from 'react'

import type { DayPoint } from '@/lib/types'

const WIDTH = 600
const HEIGHT = 120
const PADDING = 4

function path(values: number[], max: number): string {
  if (values.length === 0) return ''
  const usable = HEIGHT - PADDING * 2
  const step = values.length > 1 ? WIDTH / (values.length - 1) : 0
  return values
    .map((value, index) => {
      const x = index * step
      // Zero-based: value / max, never (value - min) / (max - min).
      const y = HEIGHT - PADDING - (max === 0 ? 0 : (value / max) * usable)
      return `${index === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`
    })
    .join(' ')
}

export function Sparkline({ series }: { series: DayPoint[] }) {
  const titleId = useId()

  if (series.length === 0) return null

  const views = series.map((p) => p.views)
  const saves = series.map((p) => p.saves)
  // One scale for both lines, so saves are visibly a fraction of views rather
  // than two lines of similar height meaning wildly different things.
  const max = Math.max(1, ...views, ...saves)

  const total = views.reduce((sum, n) => sum + n, 0)
  const busiest = series.reduce((best, p) => (p.views > best.views ? p : best), series[0])

  return (
    <figure className="m-0">
      <svg
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        preserveAspectRatio="none"
        role="img"
        aria-labelledby={titleId}
        className="h-32 w-full"
      >
        <title id={titleId}>
          {`${total} views over ${series.length} days. Busiest day ${busiest.day} with ${busiest.views}.`}
        </title>
        <path
          d={path(views, max)}
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          vectorEffect="non-scaling-stroke"
          className="text-brand-600"
        />
        <path
          d={path(saves, max)}
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeDasharray="4 3"
          vectorEffect="non-scaling-stroke"
          className="text-sand-400"
        />
      </svg>

      <figcaption className="mt-2 flex flex-wrap items-center justify-between gap-3 text-xs text-sand-500">
        <span className="flex items-center gap-3">
          <span className="flex items-center gap-1.5">
            <span className="inline-block h-0.5 w-4 bg-brand-600" aria-hidden />
            Views
          </span>
          <span className="flex items-center gap-1.5">
            <span
              className="inline-block h-0.5 w-4 border-t-2 border-dashed border-sand-400"
              aria-hidden
            />
            Saves
          </span>
        </span>
        <span>
          {series[0].day} to {series[series.length - 1].day}
        </span>
      </figcaption>
    </figure>
  )
}
