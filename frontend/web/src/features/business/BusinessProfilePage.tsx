/**
 * A business, as an explorer sees it (spec EXP-003, feature phase 4).
 *
 * The page answers, in order: what is this, where is it, what is on, and can I
 * trust it. That last one is why the verification state is at the top rather
 * than buried — an unverified business is a claim somebody made about
 * themselves, and saying so plainly is more useful than a badge whose absence
 * nobody notices.
 *
 * Everything on the page comes from one request. Listings are the same catalogue
 * read every other discovery surface uses, so a post withheld by moderation
 * leaves here at the same moment it leaves search — rather than lingering on the
 * one page that wrote its own filter.
 *
 * **What's on and the gallery are two tabs, not two sections.** They answer the
 * same question — what is this place like — from opposite ends: one is dated and
 * expires, the other is the building and does not. Stacked, the pictures would
 * sit below however many listings a busy venue happens to have that week, which
 * is where nobody scrolls. Both tabs stay visible even when empty so the layout
 * does not shift once a business adds its first photo.
 */

import { useState, type ReactNode } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import {
  BadgeCheck,
  Building2,
  Globe,
  Images,
  Mail,
  MapPin,
  Phone,
  Settings2,
} from 'lucide-react'

import { api } from '@/lib/api'
import { useAppStore } from '@/app/store'
import type { PublicBusiness } from '@/lib/types'
import { ExperienceCard } from '@/features/experiences/ExperienceCard'
import { Badge, Button, Card, EmptyState, Skeleton } from '@/design-system/primitives'
import { AddMedia, MEDIA_HINT } from './AddMedia'
import { BusinessGallery } from './BusinessGallery'
import { SocialIcon } from './SocialIcon'
import { listedSocials } from './social'

function Header({
  business,
  canManage,
  planName,
  planKey,
}: {
  business: PublicBusiness
  canManage: boolean
  /** Only passed when the viewer may see what the business is on. */
  planName?: string | null
  planKey?: string | null
}) {
  return (
    <div className="overflow-hidden rounded-2xl border border-sand-200 bg-sand-100 shadow-card">
      {business.coverUrl ? (
        <img
          src={business.coverUrl}
          alt=""
          className="h-40 w-full object-cover sm:h-52"
          loading="lazy"
        />
      ) : (
        // Not an error state. Plenty of real businesses have no cover photo, and
        // a broken-image icon would say something untrue about them.
        <div className="h-20 w-full bg-sand-100 sm:h-28" aria-hidden />
      )}

      <div className="flex flex-wrap items-start gap-4 p-5">
        {business.logoUrl ? (
          <img
            src={business.logoUrl}
            alt=""
            className="size-16 shrink-0 rounded-xl object-cover ring-1 ring-sand-200"
            loading="lazy"
          />
        ) : (
          <span className="grid size-16 shrink-0 place-items-center rounded-xl bg-brand-900/30 ring-1 ring-sand-200">
            <Building2 className="size-7 text-brand-700" aria-hidden />
          </span>
        )}

        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h1 className="text-xl font-semibold tracking-tight text-sand-950">{business.name}</h1>
            {planName && (
              <Badge tone={planKey === 'free' ? 'neutral' : 'brand'}>{planName}</Badge>
            )}
            {/* Says what this account is, next to its name. Separate from
                verification below, which says whether anybody has checked -
                two different questions that a single badge would blur. */}
            <span className="rounded-full bg-sand-200 px-2 py-0.5 text-[0.625rem] font-medium uppercase tracking-wide text-sand-600">
              Business
            </span>
            {business.verificationStatus === 'verified' ? (
              <Badge tone="success" icon={<BadgeCheck className="size-3.5" aria-hidden />}>
                Verified
              </Badge>
            ) : (
              // Said plainly rather than left blank. The absence of a badge is
              // not something an explorer notices; a sentence is.
              <Badge tone="neutral">Not verified by Mado</Badge>
            )}
            {/* Same row as the name: manage belongs on the profile you are
                looking at, not buried in settings. Only when this account owns
                the business — explorers never see it. */}
            {canManage && (
              <Link to={`/businesses/${business.id}/manage`} className="ml-auto sm:ml-0">
                <Button variant="secondary" size="sm">
                  <Settings2 className="size-3.5" aria-hidden />
                  Manage
                </Button>
              </Link>
            )}
          </div>

          {business.businessTypeLabel && (
            <p className="mt-1 flex items-center gap-1.5 text-sm text-sand-600">
              <Building2 className="size-4 shrink-0" aria-hidden />
              {business.businessTypeLabel}
            </p>
          )}

          {business.description && (
            <p className="mt-3 whitespace-pre-line text-sm leading-relaxed text-sand-700">
              {business.description}
            </p>
          )}
        </div>
      </div>
    </div>
  )
}

function ContactRow({ business }: { business: PublicBusiness }) {
  const phone = business.contact?.phone
  const email = business.contact?.email
  const social = listedSocials(business.social)

  if (!business.website && !phone && !email && social.length === 0) return null

  return (
    <Card className="flex flex-wrap items-center gap-2 p-3.5">
      {business.website && (
        <a href={business.website} target="_blank" rel="noreferrer noopener">
          <Button variant="secondary" size="sm">
            <Globe className="size-4" aria-hidden /> Website
          </Button>
        </a>
      )}
      {phone && (
        <a href={`tel:${phone}`}>
          <Button variant="secondary" size="sm">
            <Phone className="size-4" aria-hidden /> Call
          </Button>
        </a>
      )}
      {email && (
        <a href={`mailto:${email}`}>
          <Button variant="secondary" size="sm">
            <Mail className="size-4" aria-hidden /> Email
          </Button>
        </a>
      )}

      {/* The mark alone. A brand's own glyph is more recognisable than its name
          set in our type, and seven of them written out would crowd out the
          website and phone number — which is what somebody came here for.
          `aria-label` carries the name that the icon dropped, so the link is
          still announced as "Instagram" rather than as its address. */}
      {social.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5">
          {(business.website || phone || email) && (
            <span className="mx-1 hidden h-6 w-px bg-sand-200 sm:block" aria-hidden />
          )}
          {social.map(({ platform, url }) => (
            <a
              key={platform.value}
              href={url}
              target="_blank"
              rel="noreferrer noopener"
              title={platform.label}
              aria-label={platform.label}
              className="grid size-9 place-items-center rounded-full border border-sand-300 text-sand-600 transition-colors hover:border-brand-500 hover:bg-sand-200 hover:text-brand-600 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500/40"
            >
              <SocialIcon platform={platform} />
            </a>
          ))}
        </div>
      )}
    </Card>
  )
}

type Tab = 'listings' | 'gallery'

function Tabs({
  business,
  listingCount,
  galleryCount,
  children,
}: {
  business: PublicBusiness
  listingCount: number
  galleryCount: number
  children: (tab: Tab) => ReactNode
}) {
  const [tab, setTab] = useState<Tab>('listings')

  const tabs: { id: Tab; label: string; count: number; icon: ReactNode }[] = [
    {
      id: 'listings',
      label: "What's on",
      count: listingCount,
      icon: <MapPin className="size-4" aria-hidden />,
    },
    {
      id: 'gallery',
      label: 'Photos & videos',
      count: galleryCount,
      icon: <Images className="size-4" aria-hidden />,
    },
  ]

  // Derived rather than stored, so a gallery emptied under the viewer — the
  // owner deleting the last photo in another tab, and a refetch arriving —
  // falls back instead of leaving a selected tab that no longer exists.
  const active: Tab = tabs.some((entry) => entry.id === tab) ? tab : 'listings'

  return (
    <section aria-label={`${business.name} content`}>
      {/* The roving-tabindex pattern: one stop for the whole strip, and the
          arrow keys move within it. Without it, reaching the content of the
          last tab means tabbing past every other tab first. */}
      <div role="tablist" aria-label="Profile sections" className="mb-4 flex gap-1.5">
        {tabs.map(({ id, label, count, icon }) => {
          const selected = active === id
          return (
            <button
              key={id}
              role="tab"
              type="button"
              id={`business-tab-${id}`}
              aria-selected={selected}
              aria-controls={`business-panel-${id}`}
              tabIndex={selected ? 0 : -1}
              onClick={() => setTab(id)}
              onKeyDown={(event) => {
                if (event.key !== 'ArrowRight' && event.key !== 'ArrowLeft') return
                const delta = event.key === 'ArrowRight' ? 1 : -1
                const from = tabs.findIndex((entry) => entry.id === active)
                const next = tabs[(from + delta + tabs.length) % tabs.length]
                setTab(next.id)
                // Focus follows selection, which is what a tablist is expected
                // to do — otherwise the next arrow press comes from the tab that
                // is no longer selected and moves relative to the wrong one.
                document.getElementById(`business-tab-${next.id}`)?.focus()
              }}
              className={`flex items-center gap-2 rounded-full px-3.5 py-2 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500/40 ${
                selected
                  ? 'bg-brand-600 text-white'
                  : 'bg-sand-200 text-sand-700 hover:bg-sand-300'
              }`}
            >
              {icon}
              {label}
              {count > 0 && (
                <span
                  className={`rounded-full px-1.5 text-xs tabular-nums ${
                    selected ? 'bg-white/20' : 'bg-sand-100 text-sand-600'
                  }`}
                >
                  {count}
                </span>
              )}
            </button>
          )
        })}
      </div>

      <div
        role="tabpanel"
        id={`business-panel-${active}`}
        aria-labelledby={`business-tab-${active}`}
      >
        {children(active)}
      </div>
    </section>
  )
}

export function BusinessProfilePage() {
  const { slug = '' } = useParams()
  const user = useAppStore((s) => s.user)

  const {
    data: business,
    isLoading,
    isError,
    error,
  } = useQuery({
    queryKey: ['business', slug],
    queryFn: () => api.businessBySlug(slug),
    enabled: Boolean(slug),
  })

  // Whether this account *is* the business on the page. Fetched rather than
  // assumed from the session name: a business account owns exactly one profile,
  // and that is the only visitor who should see Manage here.
  const { data: accountType } = useQuery({
    queryKey: ['account-type'],
    queryFn: () => api.accountType(),
    enabled: Boolean(user),
    staleTime: 60 * 60_000,
  })

  // Gallery upload follows `profile:edit` — the same gate the server uses. Only
  // fetched for signed-in visitors; strangers and explorers never see the button.
  const { data: permissions, isSuccess: permissionsLoaded } = useQuery({
    queryKey: ['business-permissions', business?.id ?? ''],
    queryFn: () => api.businessPermissions(business!.id),
    enabled: Boolean(user && business?.id),
    retry: false,
  })

  const canViewPlan =
    Boolean(user) &&
    Boolean(business?.id) &&
    permissionsLoaded &&
    (permissions ?? []).includes('profile:view')

  const { data: planData } = useQuery({
    queryKey: ['business-plans', business?.id ?? ''],
    queryFn: () => api.businessPlans(business!.id),
    enabled: canViewPlan,
    staleTime: 30_000,
  })

  if (isLoading) {
    return (
      <div className="mx-auto max-w-4xl space-y-4 px-4 py-6">
        <Skeleton className="h-52 w-full rounded-2xl" />
        <Skeleton className="h-12 w-full rounded-xl" />
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {Array.from({ length: 6 }).map((_, i) => (
            <Skeleton key={i} className="h-40 w-full rounded-card" />
          ))}
        </div>
      </div>
    )
  }

  if (isError || !business) {
    return (
      <div className="mx-auto max-w-lg px-4 py-16">
        <EmptyState
          icon={<Building2 className="size-8" />}
          title="Not found"
          description={
            (error as Error | undefined)?.message ??
            'This business does not exist, or it is no longer listed.'
          }
          action={
            <Link to="/search">
              <Button variant="secondary">Search Mado</Button>
            </Link>
          }
        />
      </div>
    )
  }

  const count = business.listings.length
  const gallery = business.gallery ?? []
  const canManage =
    accountType?.accountType === 'business' && accountType.business?.id === business.id
  const canEditGallery =
    Boolean(user) && permissionsLoaded && (permissions ?? []).includes('profile:edit')

  return (
    <div className="mx-auto max-w-4xl space-y-5 px-4 py-6">
      <Header
        business={business}
        canManage={canManage}
        planName={canViewPlan ? planData?.current.planName : undefined}
        planKey={canViewPlan ? planData?.current.plan : undefined}
      />
      <ContactRow business={business} />

      <Tabs business={business} listingCount={count} galleryCount={gallery.length}>
        {(tab) =>
          tab === 'gallery' ? (
            <div className="space-y-4">
              {gallery.length === 0 ? (
                <EmptyState
                  icon={<Images className="size-8" />}
                  title="No photos or videos yet"
                  description={
                    canEditGallery
                      ? MEDIA_HINT
                      : `${business.name} has not added any photos or videos yet.`
                  }
                  action={
                    canEditGallery ? (
                      <AddMedia
                        businessId={business.id}
                        slug={slug}
                        label="Add photos or videos"
                      />
                    ) : undefined
                  }
                />
              ) : (
                <>
                  {canEditGallery && (
                    <div className="flex flex-wrap items-center justify-between gap-3">
                      <AddMedia businessId={business.id} slug={slug} />
                      <p className="text-xs text-sand-500">{MEDIA_HINT}</p>
                    </div>
                  )}
                  <BusinessGallery items={gallery} />
                </>
              )}
            </div>
          ) : count === 0 ? (
            // An empty state, not an error. A business that has published
            // nothing yet is an ordinary business, and the page still answered
            // what and where.
            <EmptyState
              icon={<MapPin className="size-8" />}
              title="Nothing published yet"
              description={`${business.name} has not posted anything on Mado so far.`}
            />
          ) : (
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {business.listings.map((experience) => (
                <ExperienceCard
                  key={experience.id}
                  experience={experience}
                  compact
                  hidePublisher
                />
              ))}
            </div>
          )
        }
      </Tabs>
    </div>
  )
}
