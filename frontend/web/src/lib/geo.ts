/**
 * Geometry for live navigation.
 *
 * All of it runs on the device. The explorer's live position is never sent
 * anywhere - not to Mado, not to a routing vendor - because following a line
 * that was already downloaded needs no server, and a platform that streams
 * somebody's minute-by-minute location to itself has to justify that. This does
 * not.
 *
 * Distances are computed on a local planar approximation rather than with
 * haversine per segment. Over a city the error is centimetres, and the
 * projection maths below needs a flat plane to be simple and fast enough to run
 * on every GPS tick.
 */

const EARTH_RADIUS_M = 6_371_000
const DEG = Math.PI / 180

/** GeoJSON order, as the API returns it: [longitude, latitude]. */
export type Position = [number, number]

export interface Projection {
  /** Nearest point on the route to the given position. */
  point: Position
  /** Metres from the given position to that point - how far off the line they are. */
  offRouteMetres: number
  /** Metres from the start of the route to that point. */
  alongMetres: number
  /** Index of the polyline segment the point fell on. */
  segmentIndex: number
}

export function distanceMetres(a: Position, b: Position): number {
  // Equirectangular. Exact enough below a few hundred kilometres and far
  // cheaper than haversine, which matters when this runs per segment per tick.
  const meanLat = ((a[1] + b[1]) / 2) * DEG
  const x = (b[0] - a[0]) * DEG * Math.cos(meanLat)
  const y = (b[1] - a[1]) * DEG
  return Math.sqrt(x * x + y * y) * EARTH_RADIUS_M
}

/**
 * Nearest point on a polyline, and how far along it that is.
 *
 * The whole basis of progress: a GPS fix is never exactly on the drawn line, so
 * "where am I on this route" means "what is the closest point on it". Returning
 * the distance *off* the line as well is what lets the interface admit somebody
 * has wandered rather than confidently reporting progress they are not making.
 */
export function projectOntoPath(path: Position[], position: Position): Projection | null {
  if (path.length < 2) return null

  let best: Projection | null = null
  let travelled = 0

  for (let i = 0; i < path.length - 1; i++) {
    const start = path[i]
    const end = path[i + 1]
    const segmentLength = distanceMetres(start, end)

    // Work in metres relative to the segment start, so the projection is plain
    // vector arithmetic rather than spherical trigonometry.
    const meanLat = ((start[1] + end[1]) / 2) * DEG
    const scaleX = DEG * Math.cos(meanLat) * EARTH_RADIUS_M
    const scaleY = DEG * EARTH_RADIUS_M

    const ex = (end[0] - start[0]) * scaleX
    const ey = (end[1] - start[1]) * scaleY
    const px = (position[0] - start[0]) * scaleX
    const py = (position[1] - start[1]) * scaleY

    const lengthSquared = ex * ex + ey * ey
    // Clamped to the segment: without this, a position beyond the end of one
    // segment projects onto an imaginary extension of it and reports progress
    // along road that does not exist.
    const t = lengthSquared === 0 ? 0 : Math.max(0, Math.min(1, (px * ex + py * ey) / lengthSquared))

    const closest: Position = [
      start[0] + (end[0] - start[0]) * t,
      start[1] + (end[1] - start[1]) * t,
    ]
    const off = distanceMetres(position, closest)

    if (best === null || off < best.offRouteMetres) {
      best = {
        point: closest,
        offRouteMetres: off,
        alongMetres: travelled + segmentLength * t,
        segmentIndex: i,
      }
    }
    travelled += segmentLength
  }

  return best
}

/** Total length of a polyline, in metres. */
export function pathLengthMetres(path: Position[]): number {
  let total = 0
  for (let i = 0; i < path.length - 1; i++) total += distanceMetres(path[i], path[i + 1])
  return total
}

/**
 * Split a path at a distance along it.
 *
 * Used to draw the part already walked differently from the part still ahead -
 * which is the thing that makes a map feel like it is tracking you rather than
 * displaying a picture.
 */
export function splitPathAt(path: Position[], alongMetres: number): [Position[], Position[]] {
  if (path.length < 2) return [[], path]

  const behind: Position[] = []
  let travelled = 0

  for (let i = 0; i < path.length - 1; i++) {
    const start = path[i]
    const end = path[i + 1]
    const segmentLength = distanceMetres(start, end)
    behind.push(start)

    if (travelled + segmentLength >= alongMetres) {
      const t = segmentLength === 0 ? 0 : (alongMetres - travelled) / segmentLength
      const cut: Position = [
        start[0] + (end[0] - start[0]) * t,
        start[1] + (end[1] - start[1]) * t,
      ]
      behind.push(cut)
      return [behind, [cut, ...path.slice(i + 1)]]
    }
    travelled += segmentLength
  }

  return [path, []]
}

export function formatDistance(metres: number): string {
  if (metres < 950) return `${Math.round(metres / 10) * 10} m`
  return `${(metres / 1000).toFixed(1)} km`
}


/**
 * Flatten a route's legs into one continuous path.
 *
 * Everything is drawn and measured as a single line so the route reads as one
 * journey rather than a set of disconnected hops - and so progress along it is
 * a single number instead of a leg index plus an offset.
 */
export function routePath(
  route: { legs: { geometry: number[][] }[] } | null | undefined,
): Position[] {
  const path: Position[] = []
  for (const leg of route?.legs ?? []) {
    for (const point of leg.geometry) {
      const next: Position = [point[0], point[1]]
      const last = path[path.length - 1]
      // Consecutive legs share an endpoint; repeating it makes a zero-length
      // segment that the projection maths would then have to guard against.
      if (!last || last[0] !== next[0] || last[1] !== next[1]) path.push(next)
    }
  }
  return path
}
