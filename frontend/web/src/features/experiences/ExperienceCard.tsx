/**
 * The experience card.
 *
 * Spec 11.06 "Cards" fixes what every card must convey: identity, category, time,
 * location, publisher, trust indicators and a primary action. Spec PRODUCT-00
 * principle 5 adds the explanation - the card states *why* it is being shown.
 *
 * This one component backs the canvas rails, search results and the saved list,
 * so a given experience looks identical wherever it appears.
 */

import { Link } from 'react-router-dom'
import { RepostButton } from '@/features/social/RepostButton'
import { BadgeCheck, Bookmark, Building2, Clock, MapPin, Sparkles, Star } from 'lucide-react'
import type { ExperienceSummary } from '@/lib/types'
import { Badge } from '@/design-system/primitives'
import { cn, formatDistance, formatPrice, formatWhen, isStartingSoon } from '@/lib/utils'

interface Props {
  experience: ExperienceSummary
  onToggleSave?: (experience: ExperienceSummary) => void
  className?: string
  /** Fixed width for horizontal rails; grids let the column define width. */
  fixedWidth?: boolean
}

export function ExperienceCard({ experience, onToggleSave, className, fixedWidth }: Props) {
  const image = experience.media[0]
  const when = formatWhen(experience.nextEvent?.startTime)
  const distance = formatDistance(experience.distanceKm)
  const soon = isStartingSoon(experience.nextEvent?.startTime)
  const cancelled = experience.nextEvent?.status === 'cancelled'
  const verified = experience.publisher?.verificationStatus === 'verified'
  const business = experience.publisher?.type === 'organization'

  return (
    <article
      className={cn(
        'group relative flex flex-col overflow-hidden rounded-card border border-sand-200 bg-white',
        'shadow-card transition-shadow duration-200 hover:shadow-lifted',
        fixedWidth && 'w-[17.5rem] shrink-0',
        className,
      )}
    >
      <div className="relative aspect-[4/3] overflow-hidden bg-sand-200">
        {image ? (
          <img
            src={image.url}
            alt={image.altText ?? experience.title}
            loading="lazy"
            className="size-full object-cover transition-transform duration-500 ease-[var(--ease-out-soft)] group-hover:scale-[1.03]"
          />
        ) : (
          <div className="flex size-full items-center justify-center text-sand-400">
            <MapPin className="size-8" aria-hidden />
          </div>
        )}

        {/* Time-critical status sits on the image where the eye lands first. */}
        <div className="absolute left-2.5 top-2.5 flex flex-wrap gap-1.5">
          {cancelled ? (
            <Badge tone="danger">Cancelled</Badge>
          ) : soon ? (
            <Badge tone="accent" icon={<Clock className="size-3" aria-hidden />}>
              Starting soon
            </Badge>
          ) : null}
          {experience.price.type === 'free' && !cancelled && <Badge tone="success">Free</Badge>}
        </div>

        {onToggleSave && (
          <button
            type="button"
            onClick={() => onToggleSave(experience)}
            aria-pressed={experience.isSaved}
            aria-label={experience.isSaved ? `Remove ${experience.title} from saved` : `Save ${experience.title}`}
            className={cn(
              'absolute right-2.5 top-2.5 grid size-9 place-items-center rounded-full',
              'bg-white/90 backdrop-blur transition-colors hover:bg-white',
              experience.isSaved ? 'text-brand-700' : 'text-sand-600',
            )}
          >
            <Bookmark
              className="size-4"
              // The filled state carries the meaning for anyone who cannot rely on
              // the colour shift alone.
              fill={experience.isSaved ? 'currentColor' : 'none'}
              aria-hidden
            />
          </button>
        )}
      </div>

      <div className="flex flex-1 flex-col gap-2 p-3.5">
        <div className="flex items-start justify-between gap-2">
          <h3 className="line-clamp-2 text-[0.95rem] font-semibold leading-snug text-sand-900">
            {/* Whole-card link target, but only the title is the accessible name. */}
            <Link to={`/experiences/${experience.id}`} className="after:absolute after:inset-0">
              {experience.title}
            </Link>
          </h3>
          {experience.ratingAverage != null && (
            <span className="flex shrink-0 items-center gap-0.5 text-xs font-medium text-sand-600">
              <Star className="size-3.5 fill-accent-500 text-accent-500" aria-hidden />
              {experience.ratingAverage.toFixed(1)}
            </span>
          )}
        </div>

        <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-sand-500">
          {experience.category && <span>{experience.category.name}</span>}
          {experience.venue?.neighborhood && (
            <>
              <span aria-hidden>·</span>
              <span>{experience.venue.neighborhood.name}</span>
            </>
          )}
          {when && !cancelled && (
            <>
              <span aria-hidden>·</span>
              <span className={cn(soon && 'font-medium text-accent-700')}>{when}</span>
            </>
          )}
        </div>

        {/* Who posted it, when that is a business.
            A person's name is not shown here: an individual posting about a
            place they like is the ordinary case and naming them adds a line
            without adding a fact. A business posting about itself is different
            - it is the subject describing itself, and an explorer weighing the
            claim deserves to know that before they read it. */}
        {business && (
          <p className="flex items-center gap-1.5 text-xs text-sand-600">
            <Building2 className="size-3 shrink-0 text-sand-400" aria-hidden />
            <span className="line-clamp-1">{experience.publisher?.name}</span>
            <span className="shrink-0 rounded-full bg-sand-200 px-1.5 py-0.5 text-[0.625rem] font-medium uppercase tracking-wide text-sand-600">
              Business
            </span>
          </p>
        )}

        {/* The explanation. Spec DISC-003 requires the explorer to understand why
            an item appeared without exposing the inference behind it. */}
        {experience.reason && (
          <p className="flex items-start gap-1.5 text-xs text-brand-800">
            <Sparkles className="mt-0.5 size-3 shrink-0" aria-hidden />
            <span className="line-clamp-1">{experience.reason}</span>
          </p>
        )}

        <div className="mt-auto flex items-center justify-between gap-2 pt-1">
          <span className="text-sm font-medium text-sand-800">
            {formatPrice(experience.price)}
          </span>
          <div className="flex items-center gap-2 text-xs text-sand-500">
            {distance && <span>{distance}</span>}
            {verified && (
              <BadgeCheck
                className="size-4 text-brand-600"
                aria-label={`${experience.publisher?.name} is a verified publisher`}
              />
            )}
          </div>
        </div>

        {/* Outside the link that wraps the rest of the card: this is a button,
            and nesting an interactive element inside an anchor makes the whole
            card fire on every tap. */}
        <div className="-mx-1 -mb-1 border-t border-sand-200 pt-1">
          <RepostButton experience={experience} />
        </div>
      </div>
    </article>
  )
}

export function ExperienceCardSkeleton({ fixedWidth }: { fixedWidth?: boolean }) {
  return (
    <div
      className={cn(
        'overflow-hidden rounded-card border border-sand-200 bg-white',
        fixedWidth && 'w-[17.5rem] shrink-0',
      )}
    >
      <div className="aspect-[4/3] animate-pulse bg-sand-200" />
      <div className="space-y-2 p-3.5">
        <div className="h-4 w-3/4 animate-pulse rounded bg-sand-200" />
        <div className="h-3 w-1/2 animate-pulse rounded bg-sand-200" />
        <div className="h-3 w-2/3 animate-pulse rounded bg-sand-200" />
      </div>
    </div>
  )
}
