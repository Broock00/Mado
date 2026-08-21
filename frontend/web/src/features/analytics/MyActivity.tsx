/**
 * The explorer's own summary (spec ANA-001).
 *
 * Placed on the privacy page rather than given a tab of its own, and that is
 * the whole design. The page already says "this is what Mado uses to
 * personalize your results"; this is the evidence for that sentence. Separating
 * them would leave the claim on one page and the proof on another.
 *
 * The categories are scored with the same weights that rank the feed, so this
 * is an explanation of what someone is being shown - not a second, prettier
 * calculation that happens to disagree with it.
 *
 * Nothing here is gamified. No streaks, no badges, no "you are in the top 5%".
 * The point is to show someone their own record, not to get them to come back.
 */

import { useQuery } from '@tanstack/react-query'
import { Activity } from 'lucide-react'

import { api } from '@/lib/api'
import { useAppStore } from '@/app/store'
import { Card, SectionHeading } from '@/design-system/primitives'

function Figure({ value, label }: { value: number; label: string }) {
  return (
    <div>
      <p className="text-2xl font-semibold tabular-nums text-sand-900">{value}</p>
      <p className="text-sm text-sand-600">{label}</p>
    </div>
  )
}

export function MyActivity() {
  const user = useAppStore((s) => s.user)

  const { data } = useQuery({
    queryKey: ['my-activity'],
    queryFn: () => api.myActivity(),
    enabled: Boolean(user),
  })

  if (!user || !data) return null

  const strongest = data.topCategories[0]?.weight ?? 0

  return (
    <section className="mt-10">
      <SectionHeading
        title="Your city, so far"
        subtitle="Only you can see this. It is the same data that decides what you are shown."
      />

      {data.isEmpty ? (
        <Card className="mt-3 flex items-start gap-3 p-5">
          <Activity className="mt-0.5 size-5 shrink-0 text-sand-400" aria-hidden />
          <p className="text-sm text-sand-600">
            Nothing to show yet. Once you have looked around, saved a few places or
            made a plan, this is where you will see the pattern.
          </p>
        </Card>
      ) : (
        <>
          <Card className="mt-3 grid grid-cols-2 gap-5 p-5 sm:grid-cols-5">
            <Figure value={data.exploredCount} label="places looked into" />
            <Figure value={data.savedCount} label="saved" />
            <Figure value={data.collectionCount} label="collections" />
            <Figure value={data.planCount} label="plans" />
            <Figure value={data.reviewCount} label="reviews written" />
          </Card>

          {data.topCategories.length > 0 && (
            <Card className="mt-3 p-5">
              <p className="font-medium text-sand-900">What you gravitate to</p>
              <p className="mt-0.5 text-sm text-sand-600">
                Worked out from the last {data.recentDays} days. This is what tilts your
                recommendations - turn off personalized recommendations above and it
                stops being used.
              </p>

              <ul className="mt-4 space-y-2">
                {data.topCategories.map((category) => (
                  <li key={category.slug} className="flex items-center gap-3">
                    <span className="w-28 shrink-0 truncate text-sm text-sand-800">
                      {category.name}
                    </span>
                    {/* A relative bar, deliberately unlabelled with a number.
                        The score is a weighted total, not a count of anything
                        the explorer did - printing "11" would invite them to
                        read it as eleven visits. */}
                    <span className="h-2 flex-1 overflow-hidden rounded-full bg-sand-200">
                      <span
                        className="block h-full rounded-full bg-brand-700"
                        style={{
                          width: `${strongest ? Math.max(6, (category.weight / strongest) * 100) : 0}%`,
                        }}
                      />
                    </span>
                  </li>
                ))}
              </ul>
            </Card>
          )}
        </>
      )}
    </section>
  )
}
