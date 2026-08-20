/**
 * The pin map. Google when a key is configured, MapLibre otherwise.
 */

import { Suspense, lazy } from 'react'

import { usingGoogleMaps } from './google'
import type { PinMapProps } from './types'

const Implementation = lazy(() =>
  usingGoogleMaps
    ? import('./GooglePinMap').then((m) => ({ default: m.GooglePinMap }))
    : import('./MapLibrePinMap').then((m) => ({ default: m.MapLibrePinMap })),
)

export function PinMap(props: PinMapProps) {
  return (
    <Suspense
      fallback={
        <div
          className={
            props.className ?? 'h-72 w-full overflow-hidden rounded-xl border border-sand-200'
          }
          aria-hidden
        >
          <div className="size-full animate-pulse bg-sand-200" />
        </div>
      }
    >
      <Implementation {...props} />
    </Suspense>
  )
}

export type { PinMapProps }
export default PinMap
