/**
 * The form that turns an account into a business.
 *
 * Shared by the welcome step (a new account choosing) and settings (an existing
 * individual converting). One form, because they are the same act and drifting
 * copies would eventually say different things about something irreversible.
 */

import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'

import { ApiError, api } from '@/lib/api'
import { BUSINESS_TYPES } from '@/lib/business'
import { Button, Card, Input } from '@/design-system/primitives'

export function BecomeBusinessForm({ onDone }: { onDone: (slug: string) => void }) {
  const queryClient = useQueryClient()
  const [form, setForm] = useState({ name: '', businessType: '', description: '', website: '' })
  const [error, setError] = useState<string | null>(null)

  const convert = useMutation({
    mutationFn: () =>
      api.becomeBusiness({
        name: form.name.trim(),
        businessType: form.businessType || null,
        description: form.description.trim() || null,
        website: form.website.trim() || null,
      }),
    onSuccess: (state) => {
      setError(null)
      // The account itself changed, so anything that reads who you are is
      // stale - the header name, what you may post as, the settings row.
      void queryClient.invalidateQueries({ queryKey: ['session'] })
      void queryClient.invalidateQueries({ queryKey: ['account-type'] })
      void queryClient.invalidateQueries({ queryKey: ['publishing-identities'] })
      if (state.business) onDone(state.business.slug)
    },
    onError: (caught) =>
      setError(caught instanceof ApiError ? caught.message : 'That could not be saved.'),
  })

  return (
    <Card className="space-y-4 p-5">
      <div>
        <h2 className="text-sm font-medium text-sand-700">Tell us about the business</h2>
        <p className="mt-1 text-xs text-sand-500">
          This becomes your profile. Your sign-in does not change — the same email and
          password, the same account. What changes is who you are on Mado.
        </p>
      </div>

      <div className="space-y-3">
        <div>
          <label htmlFor="biz-name" className="mb-1 block text-sm text-sand-700">
            Name
          </label>
          <Input
            id="biz-name"
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
            placeholder="Entoto View Hotel"
            maxLength={200}
          />
        </div>

        <div>
          <label htmlFor="biz-type" className="mb-1 block text-sm text-sand-700">
            What kind of place is it? <span className="text-sand-400">(optional)</span>
          </label>
          <select
            id="biz-type"
            value={form.businessType}
            onChange={(e) => setForm({ ...form, businessType: e.target.value })}
            className="w-full rounded-lg border border-sand-300 bg-sand-100 px-3 py-2 text-sm text-sand-900"
          >
            <option value="">Not saying yet</option>
            {BUSINESS_TYPES.map((type) => (
              <option key={type.value} value={type.value}>
                {type.label}
              </option>
            ))}
          </select>
        </div>

        <div>
          <label htmlFor="biz-description" className="mb-1 block text-sm text-sand-700">
            Describe it <span className="text-sand-400">(optional)</span>
          </label>
          <textarea
            id="biz-description"
            value={form.description}
            onChange={(e) => setForm({ ...form, description: e.target.value })}
            rows={3}
            maxLength={4000}
            placeholder="What is it, and what is it like to be there?"
            className="w-full rounded-lg border border-sand-300 bg-sand-100 px-3 py-2 text-sm text-sand-900"
          />
        </div>

        <div>
          <label htmlFor="biz-website" className="mb-1 block text-sm text-sand-700">
            Website <span className="text-sand-400">(optional)</span>
          </label>
          <Input
            id="biz-website"
            value={form.website}
            onChange={(e) => setForm({ ...form, website: e.target.value })}
            placeholder="https://…"
            maxLength={2000}
          />
        </div>
      </div>

      <p className="rounded-lg bg-sand-100 px-3 py-2 text-xs text-sand-600">
        This cannot be undone from here. Once the account is a business, anything it
        publishes is published by the business — and turning back would leave those
        listings, and any tickets sold, attributed to something that no longer exists.
      </p>

      {error && <p className="text-sm text-red-300">{error}</p>}

      <Button
        onClick={() => convert.mutate()}
        loading={convert.isPending}
        disabled={form.name.trim().length < 2}
      >
        Make this a business account
      </Button>
    </Card>
  )
}
