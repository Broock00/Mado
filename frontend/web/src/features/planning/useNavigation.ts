/**
 * Following a plan's route in real time.
 *
 * Turns a stream of GPS fixes into the two things somebody walking actually
 * wants: where am I on this route, and how far to the next stop.
 *
 * The whole calculation is a projection of the live fix onto the route
 * polyline, which gives distance along the line and distance off it. Everything
 * else - which stop is next, what remains, whether they have wandered - falls
 * out of those two numbers.
 *
 * Two judgements worth stating:
 *
 * **Progress only moves forward.** A GPS fix jitters by tens of metres, and in
 * a street with a parallel road nearby it can briefly project onto the wrong
 * part of the line. Letting progress jump backwards makes the covered portion
 * of the route flicker and the remaining distance bounce. Progress is therefore
 * monotonic, and only a sustained move backwards - somebody genuinely turning
 * round - resets it.
 *
 * **Being lost is reported, not hidden.** Past `OFF_ROUTE_METRES` the interface
 * says so rather than continuing to report a progress figure that has stopped
 * describing anything. Mado does not re-route: the route came from a vendor
 * call, and silently issuing more of them while someone walks is a cost and a
 * complexity nobody asked for. Saying "you are off the route" and drawing the
 * line is enough to get back to it.
 */

import { useEffect, useMemo, useRef, useState } from 'react'

import type { PlanRoute } from '@/lib/types'
import { type Position, pathLengthMetres, projectOntoPath, routePath } from '@/lib/geo'
import type { LiveFix } from '@/features/map/useLiveLocation'

/** Beyond this from the line, progress has stopped meaning anything. */
export const OFF_ROUTE_METRES = 60

/** A fix vaguer than this cannot place somebody on a street. */
export const UNUSABLE_ACCURACY_METRES = 100

/** How far progress may fall back before it is believed rather than smoothed. */
const BACKWARD_TOLERANCE_METRES = 40

/** Walking pace, metres per second, for the remaining-time estimate. */
const WALK_SPEED_MS = 1.25

export interface NavigationState {
  /** Metres along the route, monotonic. Null before the first usable fix. */
  alongMetres: number | null
  /** Metres from the route line. */
  offRouteMetres: number | null
  isOffRoute: boolean
  /** True when the fix is too vague to place them on a street. */
  isFixTooVague: boolean
  /** Index of the stop being walked towards, or null once past the last one. */
  nextStopIndex: number | null
  metresToNextStop: number | null
  minutesToNextStop: number | null
  metresRemaining: number | null
  hasArrived: boolean
}

const EMPTY: NavigationState = {
  alongMetres: null,
  offRouteMetres: null,
  isOffRoute: false,
  isFixTooVague: false,
  nextStopIndex: null,
  metresToNextStop: null,
  minutesToNextStop: null,
  metresRemaining: null,
  hasArrived: false,
}

export function useNavigation(
  route: PlanRoute | null | undefined,
  fix: LiveFix | null,
  active: boolean,
): NavigationState {
  const [state, setState] = useState<NavigationState>(EMPTY)
  const furthest = useRef(0)

  const path = useMemo(() => routePath(route), [route])
  const totalMetres = useMemo(() => pathLengthMetres(path), [path])

  // Where each stop sits along the line, so "distance to the next stop" is a
  // subtraction rather than another projection on every tick. Stop 0 is the
  // start; each subsequent stop ends the leg before it.
  const stopDistances = useMemo(() => {
    const distances: number[] = [0]
    let travelled = 0
    for (const leg of route?.legs ?? []) {
      travelled += pathLengthMetres(leg.geometry.map((p) => [p[0], p[1]] as Position))
      distances.push(travelled)
    }
    return distances
  }, [route])

  useEffect(() => {
    if (!active) {
      furthest.current = 0
      setState(EMPTY)
    }
  }, [active])

  useEffect(() => {
    if (!active || !fix || path.length < 2) return

    if (fix.accuracyMetres > UNUSABLE_ACCURACY_METRES) {
      // Reported rather than acted on. Projecting a 300-metre fix produces a
      // confident position that is simply invented.
      setState((current) => ({ ...current, isFixTooVague: true }))
      return
    }

    const projection = projectOntoPath(path, [fix.longitude, fix.latitude])
    if (!projection) return

    const isOffRoute = projection.offRouteMetres > OFF_ROUTE_METRES

    if (isOffRoute) {
      // Progress is frozen while off the route, not recomputed. Somebody 500 m
      // away projects onto whatever part of the line happens to be nearest,
      // which is meaningless - and acting on it sent the display backwards to a
      // stop already passed, and the covered portion of the line with it. What
      // they need here is "you have wandered", not a fresh guess at progress.
      setState((current) => ({
        ...current,
        offRouteMetres: projection.offRouteMetres,
        isOffRoute: true,
        isFixTooVague: false,
      }))
      return
    }

    const along =
      projection.alongMetres < furthest.current - BACKWARD_TOLERANCE_METRES
        ? projection.alongMetres // A real turn back, not jitter.
        : Math.max(furthest.current, projection.alongMetres)
    furthest.current = along

    // The first stop whose distance along the line is still ahead.
    const nextIndex = stopDistances.findIndex((distance) => distance > along + 5)
    const nextStopIndex = nextIndex === -1 ? null : nextIndex
    const metresToNextStop = nextStopIndex === null ? null : stopDistances[nextStopIndex] - along

    setState({
      alongMetres: along,
      offRouteMetres: projection.offRouteMetres,
      isOffRoute,
      isFixTooVague: false,
      nextStopIndex,
      metresToNextStop,
      minutesToNextStop:
        metresToNextStop === null
          ? null
          : Math.max(1, Math.round(metresToNextStop / WALK_SPEED_MS / 60)),
      metresRemaining: Math.max(0, totalMetres - along),
      hasArrived: nextStopIndex === null,
    })
  }, [active, fix, path, stopDistances, totalMetres])

  return state
}
