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
import { BadgeCheck, Bookmark, Building2, Clock, MapPin, Megaphone, Sparkles, Star } from 'lucide-react'
import { api } from '@/lib/api'
import { useAppStore } from '@/app/store'
import type { ExperienceSummary } from '@/lib/types'
import { Badge } from '@/design-system/primitives'
import { cn, formatDistance, formatPrice, formatWhen, isStartingSoon } from '@/lib/utils'
import { AddToPlan } from '@/features/planning/AddToPlan'

interface Props {
  experience: ExperienceSummary
  onToggleSave?: (experience: ExperienceSummary) => void
  className?: string
  /** Fixed width for horizontal rails; grids let the column define width. */
  fixedWidth?: boolean
  /** Denser card for profile grids and rails where space is tight. */
  compact?: boolean
  /** Hide the publisher row — redundant when every card is from the same business. */
  hidePublisher?: boolean
}

export function ExperienceCard({
  experience,
  onToggleSave,
  className,
  fixedWidth,
  compact,
  hidePublisher,
}: Props) {
  const activeDraftId = useAppStore((s) => s.activeDraftId)
  const image = experience.media[0]
  const when = formatWhen(experience.nextEvent?.startTime)
  const distance = formatDistance(experience.distanceKm)
  const soon = isStartingSoon(experience.nextEvent?.startTime)
  const cancelled = experience.nextEvent?.status === 'cancelled'
  const verified = experience.publisher?.verificationStatus === 'verified'
  const business = experience.publisher?.type === 'organization'

  /**
   * A click on a promoted card, counted for the business that paid for it.
   *
   * Here rather than on each page that renders results, for the reason the
   * label is here: this one component backs the canvas rails, search, visual
   * search and the saved list, so a surface added later reports without anybody
   * remembering to make it. Wiring it per page is how impressions came to be
   * counted everywhere and clicks nowhere.
   *
   * On the link rather than the article, so it means "opened" and only that.
   * The title's `after:absolute inset-0` makes the whole card that link, so an
   * ordinary click anywhere on it still counts — while the save button, which
   * sits outside the anchor, does not. Counting a save as a click would inflate
   * the one number a publisher renews on with something that is not it.
   *
   * Nothing is awaited and nothing is prevented: the navigation is what the
   * explorer asked for.
   */
  const onOpen = () => {
    if (experience.sponsored && experience.promotionId) {
      void api.recordPromotionClick(experience.promotionId)
    }
  }

  return (
    <article
      className={cn(
        'group relative flex flex-col overflow-hidden rounded-card border border-sand-200 bg-sand-100',
        'shadow-card transition-shadow duration-200 hover:shadow-lifted hover:border-sand-300',
        fixedWidth && cn(compact ? 'w-[13rem]' : 'w-[17.5rem]', 'shrink-0'),
        className,
      )}
    >
      <div
        className={cn(
          'relative overflow-hidden bg-sand-200',
          compact ? 'aspect-[16/10]' : 'aspect-[4/3]',
        )}
      >
        {image ? (
          <img
            src={image.url}
            alt={image.altText ?? experience.title}
            loading="lazy"
            className="size-full object-cover transition-transform duration-500 ease-[var(--ease-out-soft)] group-hover:scale-[1.03]"
          />
        ) : (
          <div className="flex size-full items-center justify-center text-sand-600">
            <MapPin className="size-8" aria-hidden />
          </div>
        )}

        {/* Time-critical status sits on the image where the eye lands first. */}
        <div className="absolute left-2 top-2 flex flex-wrap gap-1">
          {cancelled ? (
            <Badge tone="danger">Cancelled</Badge>
          ) : soon ? (
            <Badge tone="accent" icon={<Clock className="size-3" aria-hidden />}>
              {compact ? 'Soon' : 'Starting soon'}
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
              'absolute right-2 top-2 grid place-items-center rounded-full',
              'bg-black/50 backdrop-blur transition-colors hover:bg-black/70',
              compact ? 'size-7' : 'size-9',
              experience.isSaved ? 'text-brand-700' : 'text-white/80',
            )}
          >
            <Bookmark
              className={compact ? 'size-3.5' : 'size-4'}
              fill={experience.isSaved ? 'currentColor' : 'none'}
              aria-hidden
            />
          </button>
        )}
      </div>

      <div className={cn('flex flex-1 flex-col', compact ? 'gap-1 p-2.5' : 'gap-2 p-3.5')}>
        <div className="flex items-start justify-between gap-2">
          <h3
            className={cn(
              'font-semibold leading-snug text-white',
              compact ? 'line-clamp-1 text-sm' : 'line-clamp-2 text-[0.95rem]',
            )}
          >
            {/* Whole-card link target, but only the title is the accessible name. */}
            <Link
              to={`/experiences/${experience.id}`}
              onClick={onOpen}
              className="after:absolute after:inset-0"
            >
              {experience.title}
            </Link>
          </h3>
          {experience.ratingAverage != null && (
            <span className="flex shrink-0 items-center gap-0.5 text-xs font-medium text-sand-500">
              <Star className="size-3.5 fill-accent-500 text-accent-500" aria-hidden />
              {experience.ratingAverage.toFixed(1)}
            </span>
          )}
        </div>

        <div className="flex flex-wrap items-center gap-x-1.5 gap-y-0.5 text-xs text-sand-500">
          {experience.category && !compact && <span>{experience.category.name}</span>}
          {experience.category && !compact && experience.venue?.neighborhood && (
            <span aria-hidden>·</span>
          )}
          {experience.venue?.neighborhood && (
            <span>{experience.venue.neighborhood.name}</span>
          )}
          {(experience.category || experience.venue?.neighborhood) && when && !cancelled && (
            <span aria-hidden>·</span>
          )}
          {when && !cancelled && (
            <span className={cn(soon && 'font-medium text-mado-400')}>{when}</span>
          )}
        </div>

        {/* Who posted it, when that is a business.
            A person's name is not shown here: an individual posting about a
            place they like is the ordinary case and naming them adds a line
            without adding a fact. A business posting about itself is different
            - it is the subject describing itself, and an explorer weighing the
            claim deserves to know that before they read it. */}
        {business && !hidePublisher && (
          <p className="flex items-center gap-1.5 text-xs text-sand-500">
            <Building2 className="size-3 shrink-0 text-sand-600" aria-hidden />
            <span className="line-clamp-1">{experience.publisher?.name}</span>
            <span className="shrink-0 rounded-full bg-sand-200 px-1.5 py-0.5 text-[0.625rem] font-medium uppercase tracking-wide text-sand-500">
              Business
            </span>
          </p>
        )}

        {/* The explanation. Spec DISC-003 requires the explorer to understand why
            an item appeared without exposing the inference behind it.

            A sponsored card gets its own mark rather than the Sparkles the
            ranking uses, and shows it even in the compact layout — the label is
            the condition BUSINESS-90.01 §7 sells paid placement under, so it is
            the one thing on the card that must never be dropped to save space.
            The reason text beside it says "Promoted", set by the server, not
            an organic-sounding explanation this component invented. */}
        {experience.sponsored ? (
          <p className="flex items-start gap-1.5 text-xs text-sand-400">
            <Megaphone className="mt-0.5 size-3 shrink-0" aria-hidden />
            <span className="line-clamp-1">{experience.reason ?? 'Promoted'}</span>
          </p>
        ) : (
          experience.reason &&
          !compact && (
            <p className="flex items-start gap-1.5 text-xs text-mado-400">
              <Sparkles className="mt-0.5 size-3 shrink-0" aria-hidden />
              <span className="line-clamp-1">{experience.reason}</span>
            </p>
          )
        )}

        <div className={cn('mt-auto flex items-center justify-between gap-2', compact ? 'pt-0.5' : 'pt-1')}>
          <span className={cn('font-semibold text-white', compact ? 'text-xs' : 'text-sm')}>
            {formatPrice(experience.price)}
          </span>
          <div className="flex items-center gap-2 text-xs text-sand-500">
            {distance && <span>{distance}</span>}
            {verified && (
              <BadgeCheck
                className="size-4 text-brand-700"
                aria-label={`${experience.publisher?.name} is a verified publisher`}
              />
            )}
            {/* Quick-add to active draft — shown only when a draft is open so
                existing surfaces that render many cards stay uncluttered. */}
            {activeDraftId && !compact && (
              <AddToPlan
                experienceId={experience.id}
                eventInstanceId={experience.nextEvent?.id ?? null}
                size="sm"
              />
            )}
          </div>
        </div>
      </div>
    </article>
  )
}

export function ExperienceCardSkeleton({ fixedWidth }: { fixedWidth?: boolean }) {
  return (
    <div
      className={cn(
        'overflow-hidden rounded-card border border-sand-200 bg-sand-100',
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
