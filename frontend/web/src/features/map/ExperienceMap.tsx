/**
 * The results map. Google when a key is configured, MapLibre otherwise.
 *
 * Both implementations are built, and exactly one is ever fetched: the choice is
 * a dynamic import behind a constant, so the browser downloads the vendor in use
 * and never the other. That matters because either one is a couple of hundred
 * kilobytes, and loading both to use one would be the worst of the arrangement.
 *
 * The lazy boundary is here rather than in the pages: a page asking for a map
 * should not have to know that there are two of them.
 */

import { Suspense, lazy } from 'react'

import { usingGoogleMaps } from './google'
import type { ExperienceMapProps } from './types'

const Implementation = lazy(() =>
  usingGoogleMaps
    ? import('./GoogleExperienceMap').then((m) => ({ default: m.GoogleExperienceMap }))
    : import('./MapLibreExperienceMap').then((m) => ({ default: m.MapLibreExperienceMap })),
)

export function ExperienceMap(props: ExperienceMapProps) {
  return (
    <Suspense
      fallback={
        <div
          className={props.className ?? 'h-[28rem] w-full overflow-hidden rounded-xl'}
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

export type { ExperienceMapProps }
export default ExperienceMap
