/**
 * Time and duration formatting for plan timelines.
 *
 * Split out of PlanTimeline so that file exports only components. Mixing
 * component and non-component exports in one module breaks Fast Refresh: an
 * edit anywhere in the file forces a full reload instead of preserving state.
 */

/** Local wall-clock time. The planner works in UTC; explorers do not. */
export function clockTime(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

export function duration(minutes: number): string {
  if (minutes < 60) return `${minutes} min`
  const hours = Math.floor(minutes / 60)
  const rest = minutes % 60
  return rest === 0 ? `${hours} hr` : `${hours} hr ${rest} min`
}
