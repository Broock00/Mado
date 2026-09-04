/**
 * Experience detail (spec 57.03).
 *
 * Layout philosophy: one strong first impression, then a clear two-column
 * reading path. Photography sets the scene; the title lands on it. Booking
 * lives permanently in the right rail — no hunting for a CTA. Each content
 * section has a single visual role and is given enough room to breathe.
 *
 * No embedded map; "View on map" is an explicit external action.
 */

import { useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import {
  Accessibility,
  ArrowLeft,
  Baby,
  BadgeCheck,
  Bookmark,
  Building2,
  Car,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  Clock,
  CreditCard,
  ExternalLink,
  Flag,
  Flame,
  Heart,
  Home,
  Leaf,
  MapPin,
  Moon,
  Share2,
  Star,
  Sun,
  Wifi,
  Wind,
  X,
} from 'lucide-react'

import type { LucideIcon } from 'lucide-react'
import { api } from '@/lib/api'
import { useDiscoveryParams, useToggleSave } from '@/app/hooks'
import { AddToCollection } from '@/features/collections/AddToCollection'
import { Button, EmptyState, Skeleton } from '@/design-system/primitives'
import { ExperienceCard } from './ExperienceCard'
import { Reviews } from '@/features/reviews/Reviews'
import { TicketPanel } from '@/features/commerce/TicketPanel'
import { ReportDialog } from '@/features/trust/ReportDialog'
import { SUITABILITY_LABELS } from '@/lib/suitability'
import type { EventInstance, ExperienceDetail } from '@/lib/types'
import { cn, formatDistance, formatPrice, formatWhen } from '@/lib/utils'

/* Map common suitability slugs to an icon. Unknown slugs fall back to a
   generic checkmark. The icon is purely decorative — the label is always
   present beside it. */
const SUITABILITY_ICONS: Partial<Record<string, LucideIcon>> = {
  vegan: Leaf,
  vegetarian: Leaf,
  halal: CheckCircle2,
  kosher: CheckCircle2,
  gluten_free: Heart,
  nut_free: Heart,
  dairy_free: Heart,
  fasting_menu: Moon,
  alcohol_free: CheckCircle2,
  serves_late: Moon,
  childrens_play_area: Baby,
  child_menu: Baby,
  high_chairs: Baby,
  baby_changing: Baby,
  child_friendly: Baby,
  pushchair_access: Baby,
  step_free_access: Accessibility,
  accessible_toilet: Accessibility,
  accessible_parking: Accessibility,
  hearing_loop: Accessibility,
  sign_language: Accessibility,
  quiet_space: Home,
  indoor_seating: Home,
  outdoor_seating: Sun,
  shaded_seating: Sun,
  heated: Flame,
  air_conditioned: Wind,
  covered: Home,
  parking: Car,
  wifi: Wifi,
  prayer_room: Moon,
  pet_friendly: Heart,
  card_accepted: CreditCard,
}

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
  const [selectedEventId, setSelectedEventId] = useState<string | null>(null)
  const [photoIndex, setPhotoIndex] = useState(0)

  const { data: similar } = useQuery({
    queryKey: ['similar', experienceId],
    queryFn: () => api.similar(experienceId),
    enabled: Boolean(experienceId),
  })

  useEffect(() => {
    if (!data?.upcomingEvents.length) {
      setSelectedEventId(null)
      return
    }
    setSelectedEventId((prev) => {
      if (prev && data.upcomingEvents.some((e) => e.id === prev)) return prev
      return data.upcomingEvents[0].id
    })
  }, [data])

  if (isLoading) return <DetailSkeleton />

  if (isError || !data) {
    return (
      <div className="mx-auto max-w-3xl px-4 py-16">
        <EmptyState
          icon={<MapPin className="size-8" />}
          title="We could not find that experience"
          description="It may have been unpublished or archived."
          action={
            <Link to="/"><Button>Back to discovery</Button></Link>
          }
        />
      </div>
    )
  }

  const selectedEvent =
    data.upcomingEvents.find((e) => e.id === selectedEventId) ??
    data.upcomingEvents[0] ??
    null

  const distance = formatDistance(data.distanceKm)
  const durationLabel = data.durationMinutes
    ? `${Math.round((data.durationMinutes / 60) * 10) / 10} hrs`
    : null
  const photos = data.media.filter((m) => m.type !== 'video')
  const mapUrl = data.venue
    ? `https://www.openstreetmap.org/?mlat=${data.venue.latitude}&mlon=${data.venue.longitude}#map=17/${data.venue.latitude}/${data.venue.longitude}`
    : null

  const share = async () => {
    const url = window.location.href
    try {
      if (navigator.share) { await navigator.share({ title: data.title, url }); return }
    } catch { /* cancelled */ }
    try { await navigator.clipboard.writeText(url) } catch { /* ignore */ }
  }

  return (
    <article className="pb-40 sm:pb-28 lg:pb-12">
      {/* ── Hero ─────────────────────────────────────────────────────────── */}
      <div className="relative h-[62vh] min-h-[22rem] w-full overflow-hidden bg-sand-950 sm:h-[68vh]">
        {photos[photoIndex] ? (
          <img
            key={photos[photoIndex].id}
            src={photos[photoIndex].url}
            alt={photos[photoIndex].altText ?? data.title}
            className="size-full object-cover"
          />
        ) : (
          <div className="flex size-full items-center justify-center">
            <MapPin className="size-16 text-sand-700" aria-hidden />
          </div>
        )}

        {/* Two-layer gradient: top for controls, bottom for title legibility */}
        <div className="absolute inset-0 bg-gradient-to-b from-black/45 via-transparent to-transparent" />
        <div className="absolute inset-0 bg-gradient-to-t from-black/80 via-black/30 to-transparent" />

        {/* Top bar */}
        <div className="absolute inset-x-0 top-0 flex items-center justify-between px-4 pt-5 sm:px-6">
          <Link
            to="/"
            className="inline-flex items-center gap-1.5 rounded-full bg-black/30 px-3.5 py-2 text-sm font-medium text-white backdrop-blur-md ring-1 ring-white/15 transition hover:bg-black/45"
          >
            <ArrowLeft className="size-4" aria-hidden />
            Back
          </Link>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={share}
              aria-label="Share"
              className="grid size-10 place-items-center rounded-full bg-black/30 text-white backdrop-blur-md ring-1 ring-white/15 transition hover:bg-black/45"
            >
              <Share2 className="size-4" aria-hidden />
            </button>
            <button
              type="button"
              onClick={() => toggle(data)}
              disabled={requiresAuth}
              aria-pressed={data.isSaved}
              aria-label={data.isSaved ? 'Saved' : 'Save'}
              className={cn(
                'grid size-10 place-items-center rounded-full bg-black/30 backdrop-blur-md ring-1 ring-white/15 transition hover:bg-black/45 disabled:opacity-50',
                data.isSaved ? 'text-accent-300' : 'text-white',
              )}
            >
              <Bookmark className="size-4" fill={data.isSaved ? 'currentColor' : 'none'} aria-hidden />
            </button>
          </div>
        </div>

        {/* Photo counter */}
        {photos.length > 1 && (
          <div className="absolute right-4 top-[4.5rem] sm:right-6">
            <span className="rounded-full bg-black/35 px-2.5 py-1 text-xs font-medium text-white/90 backdrop-blur-sm ring-1 ring-white/10">
              {photoIndex + 1} / {photos.length}
            </span>
          </div>
        )}

        {/* Title block — lives on the hero */}
        <div className="absolute inset-x-0 bottom-0 px-4 pb-8 sm:px-8 sm:pb-10">
          <div className="mx-auto max-w-6xl">
            {data.category && (
              <p className="mb-2 text-xs font-semibold uppercase tracking-[0.18em] text-white/70">
                {data.category.name}
              </p>
            )}
            <h1 className="max-w-3xl text-3xl font-semibold leading-[1.1] tracking-tight text-white drop-shadow-sm sm:text-5xl sm:leading-[1.08]">
              {data.title}
            </h1>
            <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-2">
              {data.ratingAverage != null && (
                <a
                  href="#reviews"
                  className="inline-flex items-center gap-1.5 text-sm font-medium text-white/90 hover:text-white"
                >
                  <Star className="size-4 fill-accent-400 text-accent-400" aria-hidden />
                  {data.ratingAverage.toFixed(1)}
                  <span className="font-normal text-white/65">
                    · {data.ratingCount} {data.ratingCount === 1 ? 'review' : 'reviews'}
                  </span>
                </a>
              )}
              {durationLabel && (
                <span className="inline-flex items-center gap-1.5 text-sm text-white/75">
                  <Clock className="size-3.5" aria-hidden />
                  {durationLabel}
                </span>
              )}
              {data.venue && (
                <span className="inline-flex items-center gap-1.5 text-sm text-white/75">
                  <MapPin className="size-3.5" aria-hidden />
                  {data.venue.neighborhood?.name ?? data.venue.name}
                  {distance ? ` · ${distance}` : ''}
                </span>
              )}
              <div className="flex gap-1.5">
                {data.price.type === 'free' && (
                  <span className="rounded-full bg-brand-600/80 px-2.5 py-0.5 text-xs font-semibold text-white backdrop-blur-sm">Free</span>
                )}
                {data.nextEvent?.status === 'cancelled' && (
                  <span className="rounded-full bg-danger/80 px-2.5 py-0.5 text-xs font-semibold text-white backdrop-blur-sm">Cancelled</span>
                )}
              </div>
            </div>
          </div>
        </div>

        {/* Photo nav arrows */}
        {photos.length > 1 && (
          <>
            <button
              type="button"
              aria-label="Previous photo"
              onClick={() => setPhotoIndex((i) => (i - 1 + photos.length) % photos.length)}
              className="absolute bottom-8 right-16 grid size-9 place-items-center rounded-full bg-black/35 text-white backdrop-blur-sm ring-1 ring-white/15 transition hover:bg-black/55 sm:bottom-10 sm:right-20"
            >
              <ChevronLeft className="size-4" aria-hidden />
            </button>
            <button
              type="button"
              aria-label="Next photo"
              onClick={() => setPhotoIndex((i) => (i + 1) % photos.length)}
              className="absolute bottom-8 right-4 grid size-9 place-items-center rounded-full bg-black/35 text-white backdrop-blur-sm ring-1 ring-white/15 transition hover:bg-black/55 sm:bottom-10 sm:right-6"
            >
              <ChevronRight className="size-4" aria-hidden />
            </button>
          </>
        )}
      </div>

      {/* ── Main content ─────────────────────────────────────────────────── */}
      <div className="mx-auto max-w-6xl px-4 pt-8 sm:px-6 lg:pt-10">
        <div className="grid items-start gap-8 lg:grid-cols-[minmax(0,1fr)_21rem] lg:gap-12">

          {/* ── Left column ── */}
          <div className="min-w-0 space-y-10">

            {data.summary && (
              <p className="text-base leading-[1.75] text-sand-600">
                {data.summary}
              </p>
            )}

            {/* About */}
            <Section title="About this experience">
              <p className="mt-3 whitespace-pre-line text-[0.9375rem] leading-[1.8] text-sand-700">
                {data.description}
              </p>
              {(data.tags.length > 0 || data.suitability.length > 0) && (
                <div className="mt-4 flex flex-wrap gap-2">
                  {data.tags.map((tag) => (
                    <span
                      key={tag.id}
                      className="rounded-full bg-[#1a1a1a] px-3 py-1.5 text-xs font-medium text-zinc-300"
                    >
                      {tag.name}
                    </span>
                  ))}
                  {data.suitability.map((slug) => (
                    <span
                      key={slug}
                      className="rounded-full bg-[#1a1a1a] px-3 py-1.5 text-xs font-medium capitalize text-zinc-300"
                    >
                      {SUITABILITY_LABELS[slug] ?? slug.replace(/_/g, ' ')}
                    </span>
                  ))}
                </div>
              )}
            </Section>

            {/* Good to know — only shown if there are suitability items worth calling out */}
            {data.suitability.length > 0 && (
              <Section title="Good to know">
                <ul className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-3">
                  {data.suitability.map((slug) => {
                    const Icon = SUITABILITY_ICONS[slug] ?? CheckCircle2
                    const label = SUITABILITY_LABELS[slug] ?? slug.replace(/_/g, ' ')
                    return (
                      <li
                        key={slug}
                        className="flex items-center gap-2.5 rounded-xl bg-[#1a1a1a] px-3 py-2.5"
                      >
                        <span className="grid size-6 shrink-0 place-items-center rounded-lg bg-black/40 text-zinc-400">
                          <Icon className="size-3" aria-hidden />
                        </span>
                        <span className="text-xs font-medium capitalize text-zinc-300">{label}</span>
                      </li>
                    )
                  })}
                </ul>
              </Section>
            )}

            {/* Choose a date */}
            {data.upcomingEvents.length > 0 && (
              <Section id="reserve" title="Choose a date">
                <DateList
                  events={data.upcomingEvents.slice(0, 10)}
                  selectedId={selectedEvent?.id ?? null}
                  onSelect={setSelectedEventId}
                />
                {/* Ticket panel: mobile only — desktop lives in the booking widget */}
                {selectedEvent && (
                  <>
                    <div className="mt-3 overflow-hidden rounded-xl border border-sand-200 bg-sand-100 lg:hidden">
                      <div className="border-b border-sand-100 px-4 py-2.5">
                        <p className="text-sm font-medium text-sand-900">
                          {formatLongDate(selectedEvent.startTime)}
                        </p>
                        {selectedEvent.remaining != null && (
                          <p className="mt-0.5 text-xs text-sand-500">
                            {selectedEvent.remaining} left
                          </p>
                        )}
                      </div>
                      <div className="p-4">
                        <TicketPanel experienceId={data.id} occurrence={selectedEvent} embedded />
                      </div>
                    </div>
                    <div className="mt-4 rounded-xl border border-sand-200 bg-sand-100 px-4 py-4 lg:hidden">
                      <Reviews experienceId={data.id} minimal />
                    </div>
                  </>
                )}
              </Section>
            )}

            {/* Reviews on mobile when there is no ticket flow */}
            {data.upcomingEvents.length === 0 && (
              <div className="lg:hidden">
                <Reviews experienceId={data.id} minimal />
              </div>
            )}

            {/* More like this */}
            {similar && similar.length > 0 && (
              <Section title="More like this">
                <HorizontalScrollRow>
                  {similar.slice(0, 6).map((item) => (
                    <ExperienceCard key={item.id} experience={item} fixedWidth />
                  ))}
                </HorizontalScrollRow>
              </Section>
            )}

            <button
              type="button"
              onClick={() => setReporting(true)}
              className="inline-flex items-center gap-1.5 text-xs text-sand-400 transition-colors hover:text-sand-700"
            >
              <Flag className="size-3.5" aria-hidden />
              Report a problem with this listing
            </button>
          </div>

          {/* ── Right column: booking widget + reviews ── */}
          <aside className="hidden lg:block">
            <div className="sticky top-24 space-y-5">
              <BookingWidget
                data={data}
                selectedEvent={selectedEvent}
                requiresAuth={requiresAuth}
                onToggleSave={() => toggle(data)}
                onReport={() => setReporting(true)}
                mapUrl={mapUrl}
                distance={distance}
              />
              <div className="rounded-2xl border border-sand-200/80 bg-sand-100 px-5 py-4 shadow-card">
                <Reviews experienceId={data.id} minimal />
              </div>
            </div>
          </aside>
        </div>
      </div>

      {/* ── Mobile dock ── */}
      <MobileDock
        data={data}
        selectedEvent={selectedEvent}
        requiresAuth={requiresAuth}
        onToggleSave={() => toggle(data)}
        onReport={() => setReporting(true)}
        mapUrl={mapUrl}
        distance={distance}
      />

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

/* ────────────────────────────────────── Shared section wrapper */

function Section({
  id,
  title,
  children,
}: {
  id?: string
  title: string
  children: React.ReactNode
}) {
  return (
    <section id={id} className="scroll-mt-28">
      <h2 className="flex items-center gap-3 text-xl font-semibold tracking-tight text-sand-950">
        <span className="block h-5 w-1 shrink-0 rounded-full bg-brand-600" aria-hidden />
        {title}
      </h2>
      {children}
    </section>
  )
}

/* ────────────────────────────────────── Publisher card */

function PublisherCard({ publisher }: { publisher: ExperienceDetail['publisher'] }) {
  if (!publisher) return null

  const avatar = publisher.logoUrl ? (
    <img
      src={publisher.logoUrl}
      alt=""
      className="size-12 shrink-0 rounded-full object-cover ring-2 ring-sand-100"
      loading="lazy"
    />
  ) : (
    <span className="grid size-12 shrink-0 place-items-center rounded-full bg-brand-900/40 ring-2 ring-brand-700/50">
      <Building2 className="size-5 text-brand-400" aria-hidden />
    </span>
  )

  const inner = (
    <div className="flex items-center gap-3">
      {avatar}
      <div className="min-w-0">
        <div className="flex items-center gap-1.5">
          <span className="truncate text-sm font-semibold text-sand-950">{publisher.name}</span>
          {publisher.verificationStatus === 'verified' && (
            <BadgeCheck className="size-4 shrink-0 text-brand-600" aria-label="Verified" />
          )}
        </div>
        <p className="mt-0.5 text-xs text-sand-500">
          {publisher.type === 'organization'
            ? (publisher.businessTypeLabel ?? 'Business')
            : 'Individual host'}
        </p>
      </div>
    </div>
  )

  if (publisher.type === 'organization') {
    return (
      <Link
        to={`/businesses/${publisher.slug}`}
        className="group flex items-center gap-1 hover:opacity-80"
      >
        {inner}
        <ExternalLink className="ml-1 size-3.5 shrink-0 text-sand-400 opacity-0 transition group-hover:opacity-100" aria-hidden />
      </Link>
    )
  }

  return inner
}

/* ────────────────────────────────────── Horizontal scroll row */

function HorizontalScrollRow({ children }: { children: React.ReactNode }) {
  const ref = useRef<HTMLDivElement>(null)

  const scroll = (direction: -1 | 1) => {
    const el = ref.current
    if (!el) return
    el.scrollBy({ left: direction * Math.max(el.clientWidth * 0.75, 240), behavior: 'smooth' })
  }

  return (
    <div className="relative mt-4">
      <button
        type="button"
        onClick={() => scroll(-1)}
        aria-label="Scroll left"
        className="absolute -left-3 top-1/2 z-10 grid size-9 -translate-y-1/2 place-items-center rounded-full border border-sand-200 bg-sand-100 text-sand-700 shadow-card transition hover:bg-sand-200"
      >
        <ChevronLeft className="size-4" aria-hidden />
      </button>
      <div
        ref={ref}
        className="scrollbar-none flex gap-4 overflow-x-auto scroll-smooth px-1 pb-1"
      >
        {children}
      </div>
      <button
        type="button"
        onClick={() => scroll(1)}
        aria-label="Scroll right"
        className="absolute -right-3 top-1/2 z-10 grid size-9 -translate-y-1/2 place-items-center rounded-full border border-sand-200 bg-sand-100 text-sand-700 shadow-card transition hover:bg-sand-200"
      >
        <ChevronRight className="size-4" aria-hidden />
      </button>
    </div>
  )
}

/* ────────────────────────────────────── Date list */

function DateList({
  events,
  selectedId,
  onSelect,
}: {
  events: EventInstance[]
  selectedId: string | null
  onSelect: (id: string) => void
}) {
  return (
    <ul className="scrollbar-none mt-3 flex gap-2 overflow-x-auto pb-1">
      {events.map((event) => {
        const selected = event.id === selectedId
        const start = new Date(event.startTime)
        const cancelled = event.status === 'cancelled'
        const almostGone = event.remaining != null && event.remaining <= 5

        return (
          <li key={event.id} className="shrink-0">
            <button
              type="button"
              disabled={cancelled}
              aria-pressed={selected}
              onClick={() => onSelect(event.id)}
              className={cn(
                'whitespace-nowrap rounded-full px-3 py-2 text-left text-xs transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500',
                selected
                  ? 'bg-[#2a2a2a] font-medium text-zinc-100'
                  : 'bg-[#1a1a1a] text-zinc-300 hover:bg-[#222] hover:text-zinc-100',
                cancelled && 'cursor-not-allowed opacity-40',
              )}
            >
              <span className="block font-medium">
                {start.toLocaleDateString(undefined, {
                  weekday: 'short',
                  day: 'numeric',
                  month: 'short',
                })}
              </span>
              <span className={cn('mt-0.5 block tabular-nums', selected ? 'text-zinc-300' : 'text-zinc-500')}>
                {start.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })}
                {almostGone && !cancelled && (
                  <span className="ml-1.5 font-medium text-brand-400">{event.remaining} left</span>
                )}
              </span>
            </button>
          </li>
        )
      })}
    </ul>
  )
}

function formatLongDate(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    weekday: 'long',
    day: 'numeric',
    month: 'long',
    hour: '2-digit',
    minute: '2-digit',
  })
}

/* ────────────────────────────────────── Booking widget (desktop rail) */

function BookingWidget({
  data,
  selectedEvent,
  requiresAuth,
  onToggleSave,
  onReport,
  mapUrl,
  distance,
}: {
  data: ExperienceDetail
  selectedEvent: EventInstance | null
  requiresAuth: boolean
  onToggleSave: () => void
  onReport: () => void
  mapUrl: string | null
  distance: string | null
}) {
  return (
    <div className="overflow-hidden rounded-2xl border border-sand-200/80 bg-sand-100 shadow-lifted">
      {/* Price header */}
      <div className="bg-gradient-to-br from-brand-700 to-brand-800 px-5 py-4">
        <p className="text-2xl font-bold tracking-tight text-white">
          {formatPrice(data.price)}
        </p>
        {selectedEvent ? (
          <p className="mt-1 text-sm text-white/70">
            {formatLongDate(selectedEvent.startTime)}
          </p>
        ) : data.nextEvent ? (
          <p className="mt-1 text-sm text-white/70">
            Next · {formatWhen(data.nextEvent.startTime)}
          </p>
        ) : (
          <p className="mt-1 text-sm text-white/55">No upcoming dates</p>
        )}
      </div>

      {/* Ticket panel or prompt */}
      <div className="px-5 pt-4 pb-4">
        {selectedEvent ? (
          <TicketPanel experienceId={data.id} occurrence={selectedEvent} embedded />
        ) : (
          <a href="#reserve" className="block">
            <Button className="w-full" variant="secondary" size="sm">
              See available dates
            </Button>
          </a>
        )}

        <div className="mt-3 flex gap-2">
          <Button
            className="flex-1"
            variant="secondary"
            size="sm"
            onClick={onToggleSave}
            disabled={requiresAuth}
          >
            <Bookmark
              className="size-3.5 shrink-0"
              fill={data.isSaved ? 'currentColor' : 'none'}
              aria-hidden
            />
            {data.isSaved ? 'Saved' : 'Save'}
          </Button>
          <AddToCollection experienceId={data.id} citySlug={data.citySlug} size="sm" />
        </div>

        {requiresAuth && (
          <p className="mt-2 text-center text-xs text-sand-500">
            <Link to="/signin" className="font-semibold text-brand-700 hover:underline">Sign in</Link>{' '}
            to save
          </p>
        )}
      </div>

      {/* Venue */}
      {data.venue && (
        <div className="border-t border-sand-100 px-5 py-4">
          <p className="text-[0.6rem] font-bold uppercase tracking-[0.14em] text-sand-400">Venue</p>
          <p className="mt-1.5 text-sm font-semibold text-sand-950">{data.venue.name}</p>
          <p className="mt-0.5 text-sm leading-snug text-sand-500">{data.venue.address}</p>
          {distance && <p className="mt-1 text-xs text-sand-400">{distance} away</p>}
          {mapUrl && (
            <a
              href={mapUrl}
              target="_blank"
              rel="noreferrer noopener"
              className="mt-2.5 inline-flex items-center gap-1.5 text-sm font-medium text-brand-700 hover:underline"
            >
              <MapPin className="size-3.5" aria-hidden />
              View on map
              <ExternalLink className="size-3 text-brand-500" aria-hidden />
            </a>
          )}
          {/* Accessibility note beside the venue */}
          {(data.accessibility as { wheelchairAccessible?: boolean })?.wheelchairAccessible != null && (
            <p className="mt-2 flex items-center gap-1.5 text-xs text-sand-500">
              <Accessibility className="size-3.5 shrink-0" aria-hidden />
              {(data.accessibility as { wheelchairAccessible?: boolean }).wheelchairAccessible
                ? 'Wheelchair accessible'
                : 'Not wheelchair accessible'}
            </p>
          )}
        </div>
      )}

      {/* Publisher */}
      {data.publisher && (
        <div className="border-t border-sand-100 px-5 py-4">
          <p className="text-[0.6rem] font-bold uppercase tracking-[0.14em] text-sand-400">Published by</p>
          <div className="mt-2">
            <PublisherCard publisher={data.publisher} />
          </div>
        </div>
      )}

      <button
        type="button"
        onClick={onReport}
        className="flex w-full items-center justify-center gap-1.5 border-t border-sand-200 py-3 text-xs text-sand-400 transition-colors hover:bg-sand-200 hover:text-sand-700"
      >
        <Flag className="size-3.5" aria-hidden />
        Report a problem
      </button>
    </div>
  )
}

/* ────────────────────────────────────── Mobile dock */

function MobileDock({
  data,
  selectedEvent,
  requiresAuth,
  onToggleSave,
  onReport,
  mapUrl,
  distance,
}: {
  data: ExperienceDetail
  selectedEvent: EventInstance | null
  requiresAuth: boolean
  onToggleSave: () => void
  onReport: () => void
  mapUrl: string | null
  distance: string | null
}) {
  const [open, setOpen] = useState(false)

  useEffect(() => {
    if (!open) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false)
    }
    window.addEventListener('keydown', onKey)
    const previous = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      window.removeEventListener('keydown', onKey)
      document.body.style.overflow = previous
    }
  }, [open])

  return (
    <>
      <div className="fixed inset-x-0 bottom-16 z-40 border-t border-sand-100 bg-sand-100/95 shadow-[0_-1px_0_0_rgb(0_0_0/0.04),0_-8px_24px_rgb(0_0_0/0.07)] backdrop-blur-xl sm:bottom-0 lg:hidden">
        <div className="mx-auto flex max-w-6xl items-center gap-2 px-4 py-3 sm:gap-3">
          {/* Price strip opens the full booking card as a sheet. Save and Reserve
              stay as one-tap actions so the bar is not only a disclosure. */}
          <button
            type="button"
            onClick={() => setOpen(true)}
            aria-haspopup="dialog"
            aria-expanded={open}
            className="min-w-0 flex-1 rounded-lg text-left transition-colors hover:bg-sand-200/60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500"
          >
            <p className="truncate text-base font-bold text-sand-950 sm:text-lg">
              {formatPrice(data.price)}
            </p>
            <p className="truncate text-xs text-sand-500">
              {selectedEvent
                ? (formatWhen(selectedEvent.startTime) ?? formatLongDate(selectedEvent.startTime))
                : 'Details & actions'}
            </p>
          </button>
          <button
            type="button"
            onClick={onToggleSave}
            disabled={requiresAuth}
            aria-label={data.isSaved ? 'Saved' : 'Save'}
            className={cn(
              'grid size-11 shrink-0 place-items-center rounded-xl border border-sand-200 transition-colors hover:bg-sand-200 disabled:opacity-40',
              data.isSaved ? 'border-brand-600 bg-brand-900/30 text-brand-400' : 'text-sand-600',
            )}
          >
            <Bookmark className="size-5" fill={data.isSaved ? 'currentColor' : 'none'} aria-hidden />
          </button>
          {data.upcomingEvents.length > 0 ? (
            <a href="#reserve" className="shrink-0">
              <Button size="lg" className="shrink-0 px-5 sm:px-6">Reserve</Button>
            </a>
          ) : (
            <Button size="lg" onClick={() => setOpen(true)} className="shrink-0">
              Details
            </Button>
          )}
        </div>
      </div>

      {open && (
        <div
          className="fixed inset-0 z-50 lg:hidden"
          role="dialog"
          aria-modal="true"
          aria-label="Booking and actions"
        >
          <button
            type="button"
            className="absolute inset-0 bg-sand-950/40 backdrop-blur-[2px]"
            onClick={() => setOpen(false)}
            aria-label="Close"
          />
          <div className="absolute inset-x-0 bottom-0 max-h-[min(90vh,40rem)] overflow-y-auto rounded-t-2xl bg-sand-50 shadow-lifted sm:inset-x-auto sm:left-1/2 sm:w-full sm:max-w-md sm:-translate-x-1/2">
            <div className="sticky top-0 z-10 flex items-center justify-between border-b border-sand-200/80 bg-sand-50/95 px-4 py-3 backdrop-blur-sm">
              <p className="text-sm font-semibold text-sand-950">Booking & details</p>
              <button
                type="button"
                onClick={() => setOpen(false)}
                aria-label="Close"
                className="grid size-9 place-items-center rounded-full text-sand-500 transition-colors hover:bg-sand-200 hover:text-sand-800"
              >
                <X className="size-4" aria-hidden />
              </button>
            </div>
            <div className="p-4 pb-[max(1rem,env(safe-area-inset-bottom))]">
              <BookingWidget
                data={data}
                selectedEvent={selectedEvent}
                requiresAuth={requiresAuth}
                onToggleSave={onToggleSave}
                onReport={() => {
                  setOpen(false)
                  onReport()
                }}
                mapUrl={mapUrl}
                distance={distance}
              />
            </div>
          </div>
        </div>
      )}
    </>
  )
}

/* ────────────────────────────────────── Loading skeleton */

function DetailSkeleton() {
  return (
    <div>
      <Skeleton className="h-[62vh] min-h-[22rem] w-full rounded-none" />
      <div className="mx-auto max-w-6xl space-y-6 px-4 pt-10 sm:px-6">
        <div className="grid gap-10 lg:grid-cols-[1fr_21rem]">
          <div className="space-y-6">
            <Skeleton className="h-6 w-3/4" />
            <Skeleton className="h-20 w-full rounded-2xl" />
            <Skeleton className="h-32 w-full rounded-2xl" />
            <Skeleton className="h-40 w-full rounded-2xl" />
          </div>
          <Skeleton className="hidden h-80 w-full rounded-2xl lg:block" />
        </div>
      </div>
    </div>
  )
}
