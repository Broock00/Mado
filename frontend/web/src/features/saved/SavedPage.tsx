/** Saved experiences (spec 10.01.03 "Saved for Later"). */

import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Bookmark } from 'lucide-react'
import { api } from '@/lib/api'
import { useAppStore } from '@/app/store'
import { useToggleSave } from '@/app/hooks'
import { ExperienceCard, ExperienceCardSkeleton } from '@/features/experiences/ExperienceCard'
import { Button, EmptyState } from '@/design-system/primitives'

export function SavedPage() {
  const user = useAppStore((s) => s.user)
  const { toggle } = useToggleSave()

  const { data: saved, isLoading } = useQuery({
    queryKey: ['saved'],
    queryFn: () => api.savedItems(),
    enabled: Boolean(user),
  })

  // Saved items store only ids, so the experiences themselves are fetched
  // alongside. A dedicated batch endpoint would be better at scale; this keeps
  // the contract simple while the saved list is small.
  const { data: experiences, isLoading: loadingExperiences } = useQuery({
    queryKey: ['saved-experiences', saved?.map((item) => item.entityId)],
    queryFn: async () => {
      const ids = (saved ?? [])
        .filter((item) => item.entityType === 'experience')
        .map((item) => item.entityId)
      return Promise.all(ids.map((id) => api.experience(id)))
    },
    enabled: Boolean(saved && saved.length > 0),
  })

  if (!user) {
    return (
      <div className="mx-auto max-w-3xl px-4 py-16">
        <EmptyState
          icon={<Bookmark className="size-8" />}
          title="Sign in to keep your list"
          description="Save experiences and they will be here across your devices."
          action={
            <Link to="/signin">
              <Button>Sign in</Button>
            </Link>
          }
        />
      </div>
    )
  }

  const loading = isLoading || loadingExperiences

  return (
    <div className="mx-auto w-full max-w-7xl px-4 pb-24 pt-6 sm:px-6 lg:px-8">
      <h1 className="mb-6 text-2xl font-semibold tracking-tight text-sand-900">Saved</h1>

      {loading && (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
          {[0, 1, 2].map((index) => (
            <ExperienceCardSkeleton key={index} />
          ))}
        </div>
      )}

      {!loading && (!experiences || experiences.length === 0) && (
        <EmptyState
          icon={<Bookmark className="size-8" />}
          title="Nothing saved yet"
          description="Tap the bookmark on anything that looks good and it will land here."
          action={
            <Link to="/">
              <Button>Start discovering</Button>
            </Link>
          }
        />
      )}

      {experiences && experiences.length > 0 && (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
          {experiences.map((experience) => (
            <ExperienceCard key={experience.id} experience={experience} onToggleSave={toggle} />
          ))}
        </div>
      )}
    </div>
  )
}
