/**
 * Experience detail (spec 57.03).
 *
 * The information hierarchy follows the spec: identity and hero, then time and
 * status, then price and action, then location, then trust/provenance, then the
 * long description. A visitor deciding whether to go needs the first four before
 * they need prose.
 */

import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import {
  Accessibility,
  ArrowLeft,
  BadgeCheck,
  Bookmark,
  Calendar,
  Clock,
  Flag,
  MapPin,
  Star,
  Users,
} from 'lucide-react'
import { api } from '@/lib/api'
import { useDiscoveryParams, useToggleSave } from '@/app/hooks'
import { Badge, Button, Card, EmptyState, SectionHeading, Skeleton } from '@/design-system/primitives'
import { ExperienceCard } from './ExperienceCard'
import { Reviews } from '@/features/reviews/Reviews'
import { ReportDialog } from '@/features/trust/ReportDialog'
import { formatDistance, formatPrice, formatWhen } from '@/lib/utils'

export function ExperienceDetailPage() {
  const { experienceId = '' } = useParams()
  const params = useDiscoveryParams()
  const { toggle, requiresAuth } = useToggleSave()

  const { data, isLoading, isError } = useQuery({
    queryKey: ['experience', experienceId, params.lat, params.lng],
    queryFn: () => api.experience(experienceId, { lat: params.lat, lng: params.lng }),
    enabled: Boolean(experienceId),
  })

  const [reporting, setReporting] = useState(false)

  const { data: similar } = useQuery({
    queryKey: ['similar', experienceId],
    queryFn: () => api.similar(experienceId),
    enabled: Boolean(experienceId),
  })

  if (isLoading) return <DetailSkeleton />

  if (isError || !data) {
    return (
      <div className="mx-auto max-w-3xl px-4 py-16">
        <EmptyState
          icon={<MapPin className="size-8" />}
          title="We could not find that experience"
          description="It may have been unpublished or archived."
          action={
            <Link to="/">
              <Button>Back to discovery</Button>
            </Link>
          }
        />
      </div>
    )
  }

  const hero = data.media[0]
  const accessibility = data.accessibility as { wheelchairAccessible?: boolean; notes?: string }
  const distance = formatDistance(data.distanceKm)

  return (
    <article className="pb-24">
      <div className="relative h-[38vh] min-h-64 w-full overflow-hidden bg-sand-200 sm:h-[46vh]">
        {hero && (
          <img
            src={hero.url}
            alt={hero.altText ?? data.title}
            className="size-full object-cover"
          />
        )}
        <div className="absolute inset-0 bg-gradient-to-t from-black/65 via-black/10 to-transparent" />

        <Link
          to="/"
          className="absolute left-4 top-4 inline-flex items-center gap-1.5 rounded-pill bg-white/90 px-3 py-1.5 text-sm font-medium text-sand-800 backdrop-blur transition-colors hover:bg-white"
        >
          <ArrowLeft className="size-4" aria-hidden />
          Back
        </Link>

        <div className="absolute inset-x-0 bottom-0 mx-auto w-full max-w-5xl px-4 pb-6 sm:px-6">
          <div className="mb-2 flex flex-wrap gap-2">
            {data.category && <Badge tone="brand">{data.category.name}</Badge>}
            {data.price.type === 'free' && <Badge tone="success">Free</Badge>}
            {data.nextEvent?.status === 'cancelled' && <Badge tone="danger">Cancelled</Badge>}
          </div>
          <h1 className="text-2xl font-semibold tracking-tight text-white drop-shadow sm:text-4xl">
            {data.title}
          </h1>
          {data.summary && (
            <p className="mt-2 max-w-2xl text-sm text-white/90 sm:text-base">{data.summary}</p>
          )}
        </div>
      </div>

      <div className="mx-auto grid w-full max-w-5xl gap-8 px-4 pt-6 sm:px-6 lg:grid-cols-[1fr_20rem]">
        <div className="space-y-8">
          <div className="flex flex-wrap items-center gap-x-5 gap-y-2 text-sm text-sand-600">
            {data.ratingAverage != null && (
              <span className="flex items-center gap-1.5">
                <Star className="size-4 fill-accent-500 text-accent-500" aria-hidden />
                <span className="font-medium text-sand-800">{data.ratingAverage.toFixed(1)}</span>
                <span>({data.ratingCount} reviews)</span>
              </span>
            )}
            {data.durationMinutes && (
              <span className="flex items-center gap-1.5">
                <Clock className="size-4" aria-hidden />
                About {Math.round(data.durationMinutes / 60 * 10) / 10} hrs
              </span>
            )}
            {data.venue?.neighborhood && (
              <span className="flex items-center gap-1.5">
                <MapPin className="size-4" aria-hidden />
                {data.venue.neighborhood.name}
                {distance && <span className="text-sand-400">· {distance}</span>}
              </span>
            )}
          </div>

          <section>
            <h2 className="mb-2 text-lg font-semibold text-sand-900">About</h2>
            <p className="whitespace-pre-line leading-relaxed text-sand-700">{data.description}</p>
          </section>

          {data.tags.length > 0 && (
            <div className="flex flex-wrap gap-2">
              {data.tags.map((tag) => (
                <Badge key={tag.id}>{tag.name}</Badge>
              ))}
            </div>
          )}

          {data.upcomingEvents.length > 0 && (
            <section>
              <h2 className="mb-3 text-lg font-semibold text-sand-900">Upcoming dates</h2>
              <ul className="divide-y divide-sand-200 overflow-hidden rounded-card border border-sand-200 bg-white">
                {data.upcomingEvents.slice(0, 6).map((event) => (
                  <li key={event.id} className="flex items-center justify-between gap-4 px-4 py-3">
                    <div className="flex items-center gap-3">
                      <Calendar className="size-4 shrink-0 text-sand-400" aria-hidden />
                      <div>
                        <p className="text-sm font-medium text-sand-800">
                          {new Date(event.startTime).toLocaleString(undefined, {
                            weekday: 'short',
                            day: 'numeric',
                            month: 'short',
                            hour: '2-digit',
                            minute: '2-digit',
                          })}
                        </p>
                        <p className="text-xs text-sand-500">{formatWhen(event.startTime)}</p>
                      </div>
                    </div>
                    {event.remaining != null && (
                      <span className="flex items-center gap-1 text-xs text-sand-500">
                        <Users className="size-3.5" aria-hidden />
                        {event.remaining} left
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            </section>
          )}

          {/* Above "you might also like": what people said about *this* matters
              more than what else there is. */}
          <Reviews experienceId={data.id} />

          {similar && similar.length > 0 && (
            <section>
              <SectionHeading title="You might also like" />
              <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
                {similar.slice(0, 3).map((item) => (
                  <ExperienceCard key={item.id} experience={item} />
                ))}
              </div>
            </section>
          )}
        </div>

        {/* Sticky action rail: price and the primary action stay reachable while
            reading, which is the decision the page exists to support. */}
        <aside className="lg:sticky lg:top-20 lg:self-start">
          <Card className="p-5">
            <p className="text-2xl font-semibold text-sand-900">{formatPrice(data.price)}</p>
            {data.nextEvent && (
              <p className="mt-1 text-sm text-sand-500">
                Next: {formatWhen(data.nextEvent.startTime)}
              </p>
            )}

            <Button
              className="mt-4 w-full"
              size="lg"
              onClick={() => toggle(data)}
              disabled={requiresAuth}
            >
              <Bookmark
                className="size-4"
                fill={data.isSaved ? 'currentColor' : 'none'}
                aria-hidden
              />
              {data.isSaved ? 'Saved' : 'Save this'}
            </Button>
            {requiresAuth && (
              <p className="mt-2 text-center text-xs text-sand-500">
                <Link to="/signin" className="font-medium text-brand-700 hover:underline">
                  Sign in
                </Link>{' '}
                to save experiences
              </p>
            )}

            {data.venue && (
              <div className="mt-5 border-t border-sand-200 pt-4">
                <h3 className="text-sm font-medium text-sand-800">{data.venue.name}</h3>
                <p className="mt-1 text-sm text-sand-500">{data.venue.address}</p>
                <a
                  href={`https://www.openstreetmap.org/?mlat=${data.venue.latitude}&mlon=${data.venue.longitude}#map=17/${data.venue.latitude}/${data.venue.longitude}`}
                  target="_blank"
                  rel="noreferrer noopener"
                  className="mt-2 inline-block text-sm font-medium text-brand-700 hover:underline"
                >
                  View on map
                </a>
              </div>
            )}

            {accessibility?.wheelchairAccessible != null && (
              <div className="mt-4 flex items-start gap-2 border-t border-sand-200 pt-4 text-sm">
                <Accessibility className="mt-0.5 size-4 shrink-0 text-sand-500" aria-hidden />
                <div>
                  <p className="text-sand-700">
                    {accessibility.wheelchairAccessible
                      ? 'Wheelchair accessible'
                      : 'Not wheelchair accessible'}
                  </p>
                  {accessibility.notes && (
                    <p className="text-xs text-sand-500">{accessibility.notes}</p>
                  )}
                </div>
              </div>
            )}

            {/* Provenance. Spec 57.03 s32-35 requires the explorer to see who
                published this and how trusted they are. */}
            {data.publisher && (
              <div className="mt-4 border-t border-sand-200 pt-4">
                <p className="text-xs uppercase tracking-wide text-sand-400">Published by</p>
                <p className="mt-1 flex items-center gap-1.5 text-sm font-medium text-sand-800">
                  {data.publisher.name}
                  {data.publisher.verificationStatus === 'verified' && (
                    <BadgeCheck
                      className="size-4 text-brand-600"
                      aria-label="Verified publisher"
                    />
                  )}
                </p>
              </div>
            )}

            {/* Anyone can post, so anyone must be able to flag what is wrong
                (spec BUSINESS-07). Quiet, but always findable. */}
            <button
              type="button"
              onClick={() => setReporting(true)}
              className="mt-4 flex w-full items-center justify-center gap-1.5 border-t border-sand-200 pt-4 text-xs text-sand-500 transition-colors hover:text-sand-800"
            >
              <Flag className="size-3.5" aria-hidden />
              Report a problem with this listing
            </button>
          </Card>
        </aside>
      </div>

      {reporting && (
        <ReportDialog
          experienceId={data.id}
          experienceTitle={data.title}
          onClose={() => setReporting(false)}
        />
      )}
    </article>
  )
}

function DetailSkeleton() {
  return (
    <div>
      <Skeleton className="h-[38vh] min-h-64 w-full rounded-none" />
      <div className="mx-auto max-w-5xl space-y-4 px-4 pt-6 sm:px-6">
        <Skeleton className="h-8 w-2/3" />
        <Skeleton className="h-4 w-1/3" />
        <Skeleton className="h-32 w-full" />
      </div>
    </div>
  )
}
