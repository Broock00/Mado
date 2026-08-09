/**
 * A publisher's own standing (spec TRST-004).
 *
 * Shown here and nowhere else. Explorers see the verified badge and the ratings
 * other people left, which are things they can interpret; a trust number on a
 * card would be meaningless without its scale and an invitation to work out
 * what moves it rather than to do the thing it measures.
 *
 * Three things the design is careful about:
 *
 * - **The signals are the point, not the number.** A score somebody cannot act
 *   on is a grievance rather than feedback, so the reasons come first and the
 *   worst one is at the top - somebody opening this wants to know what to fix,
 *   not to be congratulated.
 * - **Thin evidence says so.** A new publisher sees "not enough yet" rather
 *   than a flattering figure, because inventing confidence about somebody with
 *   two reviews is how a reputation system starts lying.
 * - **It says what it can and cannot do.** This affects where listings rank. It
 *   does not withhold, suspend or reject anything - that stays with the
 *   moderation queue and a person reading it. A publisher who thinks a number
 *   can ban them will treat it as a threat rather than as feedback.
 */

import { useQuery } from '@tanstack/react-query'
import { Minus, ShieldCheck, TrendingDown, TrendingUp } from 'lucide-react'

import { api } from '@/lib/api'
import type { PublisherReputation, ReputationSignal } from '@/lib/types'
import { Badge, Card, SectionHeading } from '@/design-system/primitives'
import { cn } from '@/lib/utils'

const BANDS: Record<string, { label: string; tone: 'success' | 'brand' | 'neutral' | 'danger' }> = {
  excellent: { label: 'Excellent', tone: 'success' },
  good: { label: 'Good', tone: 'brand' },
  mixed: { label: 'Mixed', tone: 'neutral' },
  poor: { label: 'Needs attention', tone: 'danger' },
  provisional: { label: 'Not enough yet', tone: 'neutral' },
}

function SignalRow({ signal }: { signal: ReputationSignal }) {
  const negative = signal.direction < 0
  const Icon = signal.direction === 0 ? Minus : negative ? TrendingDown : TrendingUp

  return (
    <li className="flex items-start gap-3 py-3">
      <Icon
        className={cn('mt-0.5 size-4 shrink-0', negative ? 'text-red-600' : 'text-brand-600')}
        aria-hidden
      />
      <span className="min-w-0">
        <span className="block font-medium text-sand-900">{signal.label}</span>
        <span className="block text-sm text-sand-600">{signal.detail}</span>
      </span>
    </li>
  )
}

function Provisional({ reputation }: { reputation: PublisherReputation }) {
  return (
    <Card className="mt-3 p-5">
      <p className="flex flex-wrap items-center gap-2 font-medium text-sand-900">
        <ShieldCheck className="size-4 text-sand-400" aria-hidden />
        Not enough to judge yet
      </p>
      <p className="mt-1.5 text-sm text-sand-600">
        Standing is worked out from dates that actually went ahead and what people
        thought afterwards. You have {reputation.completedDates}{' '}
        {reputation.completedDates === 1 ? 'date' : 'dates'} behind you and{' '}
        {reputation.ratings} {reputation.ratings === 1 ? 'rating' : 'ratings'} so far.
      </p>
      {/* Said plainly, because the alternative reading of "not enough yet" is
          "you are being held back", and that is not what happens: an unproven
          publisher ranks in the middle rather than at the bottom. */}
      <p className="mt-2 text-xs text-sand-500">
        Until then you are treated as unproven rather than poor - a new publisher is
        not penalised for being new.
      </p>
    </Card>
  )
}

export function Standing() {
  const { data, isError } = useQuery({
    queryKey: ['my-reputation'],
    queryFn: () => api.myReputation(),
    retry: false,
  })

  // A publisher with nothing published gets NO_PUBLISHER from the API. That is
  // not an error worth showing on a dashboard they opened to look at numbers
  // they do not have yet.
  if (isError || !data) return null

  const band = BANDS[data.band] ?? BANDS.provisional

  return (
    <section className="mt-10">
      <SectionHeading
        title="Your standing"
        subtitle="Yours only - nobody else can see this, and it is not on your listings."
      />

      {data.isProvisional ? (
        <Provisional reputation={data} />
      ) : (
        <Card className="p-5">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <p className="flex items-center gap-2 font-medium text-sand-900">
              <ShieldCheck className="size-4 text-brand-600" aria-hidden />
              How Mado reads your record
            </p>
            <Badge tone={band.tone}>{band.label}</Badge>
          </div>

          {data.signals.length > 0 && (
            <ul className="mt-2 divide-y divide-sand-200">
              {data.signals.map((signal) => (
                <SignalRow key={signal.key} signal={signal} />
              ))}
            </ul>
          )}

          <p className="mt-4 text-xs text-sand-500">
            This affects where your listings appear in discovery. It cannot withhold,
            suspend or reject anything - a person decides that. Recent months count for
            more than old ones, so a bad patch fades.
          </p>
        </Card>
      )}
    </section>
  )
}
