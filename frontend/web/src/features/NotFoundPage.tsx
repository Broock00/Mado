import { Link } from 'react-router-dom'
import { Compass } from 'lucide-react'
import { Button, EmptyState } from '@/design-system/primitives'

/**
 * Spec 11.03 "Error Recovery": explain, suggest somewhere to go, never dead-end.
 */
export function NotFoundPage() {
  return (
    <div className="mx-auto max-w-3xl px-4 py-20">
      <EmptyState
        icon={<Compass className="size-8" />}
        title="That page does not exist"
        description="The link may be out of date, or the experience may have been unpublished."
        action={
          <Link to="/">
            <Button>Back to discovery</Button>
          </Link>
        }
      />
    </div>
  )
}
