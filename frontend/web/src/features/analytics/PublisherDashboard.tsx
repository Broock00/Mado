/**
 * "How your posts are doing" (spec PUB-006, ANA-002).
 *
 * A publisher dashboard is easy to make dishonest. Three things this one does
 * to avoid it:
 *
 * - **No rate without a sample.** The server returns null rather than a
 *   percentage when there were too few views to divide by, and this renders
 *   that as an explanation rather than as a dash. "Too few views yet" tells a
 *   publisher something; "—" tells them the page is broken.
 * - **No metrics the platform does not collect.** Impressions and bookings are
 *   in the specification and absent here. A note says so, because a publisher
 *   who does not find impressions will otherwise assume they are zero.
 * - **Nothing about individual people.** The payload has no field for it, and
 *   this page has nowhere to put one.
 */

import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { BarChart3, Eye, Flag, Star, Users } from 'lucide-react'

import { api, ApiError } from '@/lib/api'
import { useAppStore } from '@/app/store'
import type { ExperienceMetrics } from '@/lib/types'
import { Badge, Button, Card, EmptyState } from '@/design-system/primitives'
import { Sparkline } from './Sparkline'

const WINDOWS = [7, 30, 90] as const

function Stat({
  label,
  value,
  hint,
  icon: Icon,
}: {
  label: string
  value: string
  hint?: string
  icon: typeof Eye
}) {
  return (
    <Card className="p-4">
      <p className="flex items-center gap-1.5 text-xs uppercase tracking-wide text-sand-500">
        <Icon className="size-3.5" aria-hidden />
        {label}
      </p>
      <p className="mt-1 text-2xl font-semibold tabular-nums text-sand-900">{value}</p>
      {hint && <p className="mt-0.5 text-xs text-sand-500">{hint}</p>}
    </Card>
  )
}

function rate(value: number | null | undefined, minSample: number): string {
  // Null means "not enough to divide by", which is different from zero and has
  // to read differently.
  if (value === null || value === undefined) return `Under ${minSample} views`
  return `${Math.round(value * 100)}%`
}

function ListingRow({ metrics, minSample }: { metrics: ExperienceMetrics; minSample: number }) {
  const withheld = metrics.moderationStatus === 'flagged' || metrics.moderationStatus === 'rejected'

  return (
    <tr className="border-t border-sand-200">
      <td className="py-3 pr-3">
        <Link
          to={`/experiences/${metrics.experienceId}`}
          className="font-medium text-sand-900 hover:underline"
        >
          {metrics.title}
        </Link>
        <span className="mt-0.5 flex flex-wrap items-center gap-1.5">
          {metrics.status !== 'published' && <Badge tone="neutral">{metrics.status}</Badge>}
          {withheld && <Badge tone="danger">Withheld</Badge>}
          {metrics.reportCount > 0 && (
            <Badge tone="neutral">
              <Flag className="size-3" aria-hidden />
              {metrics.reportCount}
            </Badge>
          )}
        </span>
      </td>
      <td className="py-3 pr-3 text-right tabular-nums text-sand-800">{metrics.views}</td>
      <td className="py-3 pr-3 text-right tabular-nums text-sand-800">{metrics.uniqueViewers}</td>
      <td className="py-3 pr-3 text-right tabular-nums text-sand-800">{metrics.netSaves}</td>
      <td className="py-3 pr-3 text-right text-sm text-sand-600">
        {metrics.views < minSample ? (
          <span className="text-sand-400">—</span>
        ) : (
          rate(metrics.saveRate, minSample)
        )}
      </td>
      <td className="py-3 text-right text-sm text-sand-700">
        {metrics.ratingCount > 0 ? (
          <>
            {metrics.ratingAverage?.toFixed(1)}
            <span className="text-sand-400"> ({metrics.ratingCount})</span>
          </>
        ) : (
          <span className="text-sand-400">—</span>
        )}
      </td>
    </tr>
  )
}

export function PublisherDashboard() {
  const user = useAppStore((s) => s.user)
  const [windowDays, setWindowDays] = useState<number>(30)

  const { data, isLoading, error } = useQuery({
    queryKey: ['publisher-analytics', windowDays],
    queryFn: () => api.publisherAnalytics(windowDays),
    enabled: Boolean(user),
    retry: false,
  })

  if (!user) {
    return (
      <div className="mx-auto max-w-lg px-4 py-16">
        <EmptyState
          icon={<BarChart3 className="size-8" />}
          title="Sign in to see how your posts are doing"
          description="Your numbers belong to your account."
          action={
            <Link to="/signin">
              <Button>Sign in</Button>
            </Link>
          }
        />
      </div>
    )
  }

  if (error instanceof ApiError && error.code === 'NO_PUBLISHER') {
    return (
      <div className="mx-auto max-w-lg px-4 py-16">
        <EmptyState
          icon={<BarChart3 className="size-8" />}
          title="Nothing to report on yet"
          description="Once you have posted something, this is where you will see how it is doing."
          action={
            <Link to="/compose">
              <Button>Share something</Button>
            </Link>
          }
        />
      </div>
    )
  }

  return (
    <div className="mx-auto w-full max-w-5xl px-4 pb-24 pt-6 sm:px-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-sand-900">
            How your posts are doing
          </h1>
          <p className="mt-1 text-sand-600">
            Aggregate numbers only. You can see how many people opened a listing, never
            which people.
          </p>
        </div>

        <div className="flex gap-1 rounded-lg border border-sand-200 p-0.5" role="group">
          {WINDOWS.map((days) => (
            <button
              key={days}
              type="button"
              onClick={() => setWindowDays(days)}
              aria-pressed={windowDays === days}
              className={
                windowDays === days
                  ? 'rounded-md bg-brand-100 px-3 py-1.5 text-sm font-medium text-brand-900'
                  : 'rounded-md px-3 py-1.5 text-sm text-sand-600 hover:text-sand-900'
              }
            >
              {days}d
            </button>
          ))}
        </div>
      </div>

      {isLoading && <Card className="mt-6 p-5 text-sm text-sand-500">Loading…</Card>}

      {data && (
        <>
          <div className="mt-6 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <Stat
              label="Views"
              value={String(data.totalViews)}
              hint={`in the last ${data.windowDays} days`}
              icon={Eye}
            />
            <Stat
              label="People"
              value={String(data.uniqueViewers)}
              hint="one person reading three posts counts once"
              icon={Users}
            />
            <Stat
              label="Saves"
              value={String(data.netSaves)}
              hint={
                data.saveRate === null || data.saveRate === undefined
                  ? `save rate needs ${data.minRateSample}+ views`
                  : `${Math.round(data.saveRate * 100)}% of views`
              }
              icon={Star}
            />
            <Stat
              label="Rating"
              value={data.ratingAverage ? data.ratingAverage.toFixed(2) : '—'}
              hint={
                data.reviewCount > 0
                  ? `${data.reviewCount} new ${data.reviewCount === 1 ? 'review' : 'reviews'}`
                  : 'across everything you have posted'
              }
              icon={Star}
            />
          </div>

          {data.withheldCount > 0 && (
            <Card className="mt-4 border-red-200 bg-red-50 p-4 text-sm text-red-900">
              {data.withheldCount} of your posts{' '}
              {data.withheldCount === 1 ? 'is' : 'are'} withheld from discovery pending a
              moderator. They are listed below.
            </Card>
          )}

          <Card className="mt-6 p-5">
            <Sparkline series={data.series} />
          </Card>

          <section className="mt-8">
            <h2 className="text-lg font-medium text-sand-900">Each post</h2>
            {data.experiences.length === 0 ? (
              <Card className="mt-3 p-5 text-sm text-sand-500">
                Nothing published yet.
              </Card>
            ) : (
              <Card className="mt-3 overflow-x-auto p-5">
                <table className="w-full min-w-[36rem] text-sm">
                  <thead>
                    <tr className="text-left text-xs uppercase tracking-wide text-sand-500">
                      <th className="pb-2 font-medium">Post</th>
                      <th className="pb-2 text-right font-medium">Views</th>
                      <th className="pb-2 text-right font-medium">People</th>
                      <th className="pb-2 text-right font-medium">Saves</th>
                      <th className="pb-2 text-right font-medium">Save rate</th>
                      <th className="pb-2 text-right font-medium">Rating</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.experiences.map((metrics) => (
                      <ListingRow
                        key={metrics.experienceId}
                        metrics={metrics}
                        minSample={data.minRateSample}
                      />
                    ))}
                  </tbody>
                </table>
              </Card>
            )}
          </section>

          {/* Said plainly rather than left to be inferred. A publisher who does
              not find impressions will otherwise assume they were zero. */}
          <p className="mt-6 text-xs leading-relaxed text-sand-500">
            A view means somebody opened the listing. Mado does not count how many
            times a card appeared in a feed, and there is no booking flow yet, so
            impressions and bookings are not shown rather than shown as zero. Save
            counts are net of people who later unsaved.
          </p>
        </>
      )}
    </div>
  )
}
