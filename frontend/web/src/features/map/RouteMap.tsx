/**
 * The route map. Google when a key is configured, MapLibre otherwise.
 *
 * Unlike the results map, this one is imported eagerly by the page that guides
 * somebody along a route - so the Suspense boundary here is doing real work
 * rather than deferring to a caller's.
 */

import { Suspense, lazy } from 'react'

import { usingGoogleMaps } from './google'
import type { RouteMapProps } from './types'

const Implementation = lazy(() =>
  usingGoogleMaps
    ? import('./GoogleRouteMap').then((m) => ({ default: m.GoogleRouteMap }))
    : import('./MapLibreRouteMap').then((m) => ({ default: m.MapLibreRouteMap })),
)

export function RouteMap(props: RouteMapProps) {
  return (
    <Suspense
      fallback={
        <div className={props.className ?? 'h-72 w-full'} aria-hidden>
          <div className="size-full animate-pulse bg-sand-200" />
        </div>
      }
    >
      <Implementation {...props} />
    </Suspense>
  )
}

export type { RouteMapProps }
export default RouteMap
