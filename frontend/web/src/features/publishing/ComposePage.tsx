/**
 * Compose and edit a post.
 *
 * The design follows from the product decision that a publisher is just an
 * explorer: this is a posting form, not a catalogue-management console. There is
 * no organization setup, no verification step, and no approval queue in the way.
 *
 * Two things it does that a plain form would not:
 *  - shows what still stands between the draft and going live, continuously,
 *    rather than failing on submit
 *  - saves as a draft first, so nothing is lost if the explorer stops halfway
 */

import { Suspense, lazy, useEffect, useMemo, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle,
  ArrowLeft,
  Check,
  Clock,
  ImagePlus,
  MapPin,
  Ticket,
  Upload,
  X,
} from 'lucide-react'
import { ApiError, api } from '@/lib/api'
import { useAppStore } from '@/app/store'
import type { PickedLocation } from '@/features/map/LocationPicker'
import { Badge, Button, Card, Input } from '@/design-system/primitives'
import { cn } from '@/lib/utils'
import type { CreatePostInput, OwnPost } from '@/lib/types'
import { TicketPlanEditor } from '@/features/commerce/TicketPlanEditor'
import type { DraftTicket } from '@/features/commerce/TicketPlanEditor'
import { WritingHelp } from './WritingHelp'

const TYPES: { value: CreatePostInput['type']; label: string; hint: string }[] = [
  { value: 'place', label: 'A place', hint: 'Somewhere people can go any time it is open' },
  { value: 'event', label: 'An event', hint: 'Happens at specific dates and times' },
  { value: 'activity', label: 'An activity', hint: 'Something people take part in or book' },
]

// Loaded on demand - the confirmation map is only reached by publishers.
const LocationPicker = lazy(() =>
  import('@/features/map/LocationPicker').then((m) => ({ default: m.LocationPicker })),
)

export function ComposePage() {
  const { experienceId } = useParams()
  const isEditing = Boolean(experienceId)
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  const user = useAppStore((s) => s.user)
  const citySlug = useAppStore((s) => s.citySlug)
  const explorerLocation = useAppStore((s) => s.location)

  // A listing belongs to a city, and the app no longer assumes one. The centre
  // the map picker opens on comes from whichever city is chosen rather than
  // from a hardcoded table that only ever held one entry.
  const { data: cities } = useQuery({ queryKey: ['cities'], queryFn: () => api.cities() })

  const [draftId, setDraftId] = useState<string | null>(experienceId ?? null)
  const [error, setError] = useState<string | null>(null)

  const [form, setForm] = useState<CreatePostInput>({
    title: '',
    description: '',
    citySlug: citySlug ?? '',
    type: 'place',
    summary: '',
    categorySlug: null,
    priceType: 'free',
    priceAmount: null,
    currency: null,
  })

  // Venue is captured inline. Requiring people to find a venue in a picker before
  // they can describe their own event is the kind of friction that stops them
  // posting at all.
  const [venue, setVenue] = useState({ name: '' })
  const [venueId, setVenueId] = useState<string | null>(null)
  const [picked, setPicked] = useState<PickedLocation | null>(null)
  const [dateInput, setDateInput] = useState('')
  const [imageUrl, setImageUrl] = useState('')

  // Dates and tickets are edited here and written down once, in dependency
  // order, when the post is saved or published. They used to be four separate
  // trips to the server that could only happen in one sequence - price, save,
  // date, ticket - which is an implementation detail nobody writing a post
  // should have had to learn.
  const [dates, setDates] = useState<{ id: string | null; startTime: string }[]>([])
  const [tickets, setTickets] = useState<DraftTicket[]>([])
  const [seeded, setSeeded] = useState(false)
  // Which button is waiting, so only that one shows a spinner.
  const [publishing, setPublishing] = useState(false)

  const { data: categories } = useQuery({
    queryKey: ['categories'],
    queryFn: () => api.categories(),
    staleTime: 60 * 60_000,
  })

  const { data: existing } = useQuery({
    queryKey: ['my-post', experienceId],
    queryFn: () => api.myPost(experienceId!),
    enabled: isEditing,
  })

  useEffect(() => {
    if (!existing) return
    setForm({
      title: existing.title,
      description: existing.description,
      citySlug: existing.citySlug ?? citySlug ?? '',
      type: existing.type,
      summary: existing.summary ?? '',
      categorySlug: existing.category?.slug ?? null,
      priceType: existing.price.type,
      priceAmount: existing.price.amount ?? null,
      currency: existing.price.currency ?? null,
    })
    setVenueId(existing.venue?.id ?? null)
    setDraftId(existing.id)
    setDates(
      (existing.upcomingEvents ?? [])
        .filter((event) => event.status !== 'cancelled')
        .map((event) => ({ id: event.id, startTime: event.startTime })),
    )
  }, [existing, citySlug])

  // The saved ticket plan, folded in once. Seeded rather than kept in sync,
  // because after that the list on screen is the one being edited and a refetch
  // landing on top of it would throw away what somebody just typed.
  const { data: savedPlan } = useQuery({
    queryKey: ['ticket-plan', draftId],
    queryFn: () => api.ticketPlan(draftId!),
    enabled: Boolean(draftId),
  })

  useEffect(() => {
    if (seeded || !savedPlan) return
    setTickets(
      savedPlan.map((entry) => ({
        name: entry.name,
        description: entry.description ?? '',
        priceMinor: entry.priceMinor,
        quantity: entry.quantity ?? null,
        dates: entry.dates,
        sold: entry.sold,
        varies: entry.varies,
      })),
    )
    setSeeded(true)
  }, [savedPlan, seeded])

  const post: OwnPost | undefined = existing
  // Images still go straight to the server, because an upload needs somewhere
  // to belong and is not something anybody expects to be batched.
  const uploadImage = useMutation({
    mutationFn: (file: File) => api.uploadPostImage(draftId!, file, form.title),
    onSuccess: () => {
      setError(null)
      void queryClient.invalidateQueries({ queryKey: ['my-post', draftId] })
    },
    onError: (err) =>
      setError(err instanceof ApiError ? err.message : 'Could not upload that image.'),
  })

  const addImage = useMutation({
    mutationFn: () => api.addPostImage(draftId!, imageUrl, form.title),
    onSuccess: () => {
      setImageUrl('')
      queryClient.invalidateQueries({ queryKey: ['my-post', draftId] })
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : 'Could not add that image.'),
  })

  const chosenCity = cities?.find((c) => c.slug === form.citySlug) ?? null
  // The city decides unless the publisher says otherwise. Sending nothing lets
  // the server apply the same rule, so the two cannot disagree.
  const effectiveCurrency = form.currency ?? chosenCity?.currency ?? 'ETB'
  // Every currency Mado has a city in, plus the one being used. Built from the
  // cities rather than hard-coded so a new city brings its own along.
  const currencyChoices = Array.from(
    new Set([effectiveCurrency, ...(cities ?? []).map((c) => c.currency)]),
  ).sort()

  const canSave =
    form.title.trim().length >= 4 &&
    form.description.trim().length > 0 &&
    Boolean(form.citySlug)

  /**
   * What is still missing, judged from the screen rather than from the last
   * save.
   *
   * This used to be the server's list about the saved draft, which meant it
   * could tell somebody to add a location they had just added and leave Publish
   * dead while the fix sat in front of them. The server still decides - it
   * re-checks on publish and answers with its own list, which is what fills the
   * error - but the thing shown while typing has to describe what is on screen.
   *
   * Kept deliberately in step with `readiness_problems` in
   * app/domains/publisher/service.py.
   */
  const outstanding = useMemo(() => {
    const missing: string[] = []
    if (form.title.trim().length < 4) missing.push('Give it a title of at least 4 characters.')
    if (form.description.trim().length < 40)
      missing.push('Add a description of at least 40 characters.')
    if (!form.citySlug) missing.push('Choose a city.')
    if (!form.categorySlug) missing.push('Choose a category so people can find it.')
    if (!venueId && !picked) missing.push('Add a location.')
    if (form.type === 'event' && dates.length === 0)
      missing.push('Add at least one date and time.')
    if (form.priceType !== 'free' && form.priceAmount == null)
      missing.push('Set a price, or mark it as free.')
    return missing
  }, [form, venueId, picked, dates])

  /**
   * Write everything down, in the order the server needs it.
   *
   * A venue has to exist before a listing can point at one, a listing before a
   * date can hang off it, and a date before a ticket can be sold for it. That
   * ordering is real and is not going away - but it was being enforced on the
   * person filling in the form, who had to save, wait, add a date, wait, then
   * discover where tickets lived. Everything is edited at once now and this
   * puts it in order at the end.
   */
  const commit = useMutation({
    mutationFn: async ({ thenPublish }: { thenPublish: boolean }) => {
      // 1. The place, if a pin has been dropped and it is not saved yet.
      let venue = venueId
      if (!venue && picked && venue_name()) {
        const created = await api.createVenue({
          name: venue_name(),
          address:
            picked.label ?? `${picked.latitude.toFixed(5)}, ${picked.longitude.toFixed(5)}`,
          citySlug: form.citySlug,
          latitude: picked.latitude,
          longitude: picked.longitude,
        })
        venue = created.id
        setVenueId(created.id)
      }

      // 2. The listing.
      const payload = { ...form, venueId: venue }
      const saved = draftId
        ? await api.updatePost(draftId, payload)
        : await api.createPost(payload)
      setDraftId(saved.id)

      // 3. Its dates. Only the ones that are not on the server yet; the id is
      //    what distinguishes them.
      for (const date of dates) {
        if (date.id) continue
        await api.addPostDate(saved.id, new Date(date.startTime).toISOString())
      }

      // 4. Its tickets, but only once there is a date to sell them for -
      //    applying them to nothing would be refused, and a listing with no
      //    dates yet is a perfectly ordinary draft.
      const hasDates = dates.length > 0
      if (hasDates) {
        for (const ticket of tickets) {
          await api.sellTicket(saved.id, {
            name: ticket.name,
            description: ticket.description || null,
            priceMinor: ticket.priceMinor,
            quantity: ticket.quantity,
            currency: effectiveCurrency,
          })
        }
        // Anything removed on screen stops being sold. Withdrawn, not deleted.
        for (const entry of savedPlan ?? []) {
          if (!tickets.some((ticket) => ticket.name === entry.name)) {
            await api.stopSellingTicket(saved.id, entry.name)
          }
        }
      }

      if (thenPublish) return api.postAction(saved.id, 'publish')
      return saved
    },
    onSuccess: (result, variables) => {
      setError(null)
      void queryClient.invalidateQueries({ queryKey: ['my-posts'] })
      void queryClient.invalidateQueries({ queryKey: ['my-post', result.id] })
      void queryClient.invalidateQueries({ queryKey: ['ticket-plan', result.id] })
      if (variables.thenPublish) {
        void queryClient.invalidateQueries({ queryKey: ['canvas'] })
        navigate(`/posts?published=${result.id}`)
      }
    },
    onSettled: () => setPublishing(false),
    onError: (err) => {
      if (err instanceof ApiError && err.code === 'EXPERIENCE_INCOMPLETE') {
        const list = (err.details?.problems as string[] | undefined) ?? []
        setError(list.join(' '))
        void queryClient.invalidateQueries({ queryKey: ['my-post', draftId] })
        return
      }
      setError(err instanceof ApiError ? err.message : 'Could not save that.')
    },
  })

  // Read at commit time rather than closed over, so a name typed a moment ago
  // is the one that gets used.
  function venue_name() {
    return venue.name.trim()
  }

  if (!user) {
    return (
      <div className="mx-auto max-w-md px-4 py-20 text-center">
        <h1 className="text-xl font-semibold text-sand-900">Sign in to post</h1>
        <p className="mt-2 text-sand-500">
          Anyone with an account can share something happening in the city.
        </p>
        <Button className="mt-5" onClick={() => navigate('/signin')}>
          Sign in
        </Button>
      </div>
    )
  }

  return (
    <div className="mx-auto w-full max-w-6xl px-4 pb-28 pt-6 sm:px-6">
      <button
        type="button"
        onClick={() => navigate('/posts')}
        className="mb-4 inline-flex items-center gap-1.5 text-sm font-medium text-sand-600 hover:text-sand-900"
      >
        <ArrowLeft className="size-4" aria-hidden />
        Your posts
      </button>

      <h1 className="text-2xl font-semibold tracking-tight text-sand-900">
        {isEditing ? 'Edit your post' : 'Share something'}
      </h1>
      <p className="mt-1.5 text-sand-500">
        Tell people what it is, where, and when. You can save and come back to it.
      </p>

      {error && (
        <p role="alert" className="mt-4 rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700">
          {error}
        </p>
      )}

      {/* Two columns on a wide screen. What the thing *is* on the left, what
          it costs and when it happens on the right - they are consulted
          together, edited together and were previously several screens apart.
          One column below the breakpoint, in the same order. */}
      <div className="mt-6 grid items-start gap-5 lg:grid-cols-[minmax(0,1fr)_360px]">
        <div className="space-y-5">
        <Card className="space-y-4 p-5">
          <fieldset>
            <legend className="mb-2 text-sm font-medium text-sand-700">What kind of thing?</legend>
            <div className="grid gap-2 sm:grid-cols-3">
              {TYPES.map((option) => (
                <button
                  key={option.value}
                  type="button"
                  onClick={() => setForm({ ...form, type: option.value })}
                  aria-pressed={form.type === option.value}
                  className={cn(
                    'rounded-lg border p-3 text-left transition-colors',
                    form.type === option.value
                      ? 'border-brand-600 bg-brand-50'
                      : 'border-sand-300 hover:bg-sand-100',
                  )}
                >
                  <span className="block text-sm font-medium text-sand-900">{option.label}</span>
                  <span className="mt-0.5 block text-xs text-sand-500">{option.hint}</span>
                </button>
              ))}
            </div>
          </fieldset>

          <div>
            <label htmlFor="title" className="mb-1.5 block text-sm font-medium text-sand-700">
              Title
            </label>
            <Input
              id="title"
              value={form.title}
              onChange={(e) => setForm({ ...form, title: e.target.value })}
              placeholder="Sunset poetry on the rooftop"
              maxLength={240}
            />
          </div>

          <div>
            <label htmlFor="summary" className="mb-1.5 block text-sm font-medium text-sand-700">
              One line <span className="font-normal text-sand-400">(optional)</span>
            </label>
            <Input
              id="summary"
              value={form.summary ?? ''}
              onChange={(e) => setForm({ ...form, summary: e.target.value })}
              placeholder="Spoken word above Bole, with tea"
              maxLength={400}
            />
          </div>

          <div>
            <label htmlFor="description" className="mb-1.5 block text-sm font-medium text-sand-700">
              Description
            </label>
            <textarea
              id="description"
              value={form.description}
              onChange={(e) => setForm({ ...form, description: e.target.value })}
              rows={6}
              placeholder="What happens, who it suits, anything worth knowing before turning up."
              className="w-full rounded-lg border border-sand-300 bg-white px-3.5 py-2.5 text-sm placeholder:text-sand-400 focus:border-brand-600 focus:outline-none focus:ring-2 focus:ring-brand-600/20"
            />
            <p className="mt-1 text-xs text-sand-400">
              {form.description.trim().length} characters — at least 40 to publish
            </p>
          </div>

          {/* Directly under the description it helps with, rather than at the
              top of the form. Nothing to suggest until something is written. */}
          <WritingHelp
            title={form.title}
            description={form.description}
            summary={form.summary}
            onApply={(patch) => setForm((current) => ({ ...current, ...patch }))}
          />

          <div className="grid gap-4 sm:grid-cols-2">
            <div>
              <label htmlFor="city" className="mb-1.5 block text-sm font-medium text-sand-700">
                City
              </label>
              <select
                id="city"
                value={form.citySlug}
                onChange={(e) => setForm({ ...form, citySlug: e.target.value })}
                className="h-11 w-full rounded-lg border border-sand-300 bg-white px-3 text-sm focus:border-brand-600 focus:outline-none focus:ring-2 focus:ring-brand-600/20"
              >
                <option value="">Choose one…</option>
                {cities?.map((c) => (
                  <option key={c.id} value={c.slug}>
                    {c.name}, {c.country}
                  </option>
                ))}
              </select>
            </div>

            <div>
              <label htmlFor="category" className="mb-1.5 block text-sm font-medium text-sand-700">
                Category
              </label>
              <select
                id="category"
                value={form.categorySlug ?? ''}
                onChange={(e) => setForm({ ...form, categorySlug: e.target.value || null })}
                className="h-11 w-full rounded-lg border border-sand-300 bg-white px-3 text-sm focus:border-brand-600 focus:outline-none focus:ring-2 focus:ring-brand-600/20"
              >
                <option value="">Choose one…</option>
                {categories?.map((c) => (
                  <option key={c.id} value={c.slug}>
                    {c.name}
                  </option>
                ))}
              </select>
            </div>

          </div>
        </Card>

        <Card className="space-y-3 p-5">
          <h2 className="flex items-center gap-2 text-sm font-medium text-sand-700">
            <MapPin className="size-4" aria-hidden />
            Where is it?
          </h2>
          {venueId ? (
            <p className="flex items-center gap-2 rounded-lg bg-brand-50 px-3 py-2 text-sm text-brand-800">
              <Check className="size-4" aria-hidden />
              {venue.name || post?.venue?.name || 'Location added'}
            </p>
          ) : (
            <>
              <Input
                value={venue.name}
                onChange={(e) => setVenue({ ...venue, name: e.target.value })}
                placeholder="What is this place called?"
                aria-label="Place name"
              />

              {/* The map is the input, not a preview of one. Typing an address
                  asked publishers for something they do not know - nobody
                  standing outside their own cafe knows the postal address a
                  geocoder wants - and gave no feedback until after they had
                  committed. Placing a pin is the same act as knowing where you
                  are. */}
              {!explorerLocation.latitude && !chosenCity ? (
                <p className="rounded-lg bg-sand-100 px-3 py-2 text-sm text-sand-600">
                  Choose a city above, or share your location, and the map will open there.
                </p>
              ) : (
              <Suspense
                fallback={<div className="h-72 w-full animate-pulse rounded-xl bg-sand-200" />}
              >
                <LocationPicker
                  centre={
                    explorerLocation.latitude != null && explorerLocation.longitude != null
                      ? {
                          latitude: explorerLocation.latitude,
                          longitude: explorerLocation.longitude,
                        }
                      : { latitude: chosenCity!.latitude, longitude: chosenCity!.longitude }
                  }
                  value={picked}
                  onChange={setPicked}
                  citySlug={form.citySlug}
                />
              </Suspense>
              )}

              {/* No "use this location" button any more. Dropping the pin is
                  the act of choosing where it is; asking somebody to confirm
                  the thing they just did, and hiding the rest of the form
                  behind that confirmation, was a step that existed only because
                  a venue row has to be created before a listing can point at
                  one. It is created when the post is saved, along with
                  everything else. */}
              {picked && (
                <p className="flex items-center gap-1.5 text-sm text-brand-800">
                  <Check className="size-4 shrink-0" aria-hidden />
                  {picked.label ?? `${picked.latitude.toFixed(4)}, ${picked.longitude.toFixed(4)}`}
                </p>
              )}
            </>
          )}
        </Card>

        {draftId && (
          <Card className="space-y-3 p-5">
            <h2 className="flex items-center gap-2 text-sm font-medium text-sand-700">
              <ImagePlus className="size-4" aria-hidden />
              A photo <span className="font-normal text-sand-400">(optional)</span>
            </h2>
            {post?.media && post.media.length > 0 && (
              <div className="flex gap-2 overflow-x-auto">
                {post.media.map((m) => (
                  <img
                    key={m.id}
                    src={m.url}
                    alt={m.altText ?? ''}
                    className="size-20 shrink-0 rounded-lg object-cover"
                  />
                ))}
              </div>
            )}
            {/* Upload first, paste-a-URL second. Almost nobody photographing a
                venue has somewhere to host the picture already, so asking for a
                URL was in practice asking most publishers not to add a photo. */}
            <label className="flex cursor-pointer items-center justify-center gap-2 rounded-lg border border-dashed border-sand-300 px-4 py-6 text-sm text-sand-600 hover:bg-sand-100">
              <Upload className="size-4" aria-hidden />
              {uploadImage.isPending ? 'Uploading…' : 'Choose a photo'}
              <input
                type="file"
                accept="image/jpeg,image/png,image/webp"
                className="sr-only"
                disabled={uploadImage.isPending}
                onChange={(event) => {
                  const file = event.target.files?.[0]
                  if (file) uploadImage.mutate(file)
                  // Reset so choosing the same file twice still fires a change.
                  event.target.value = ''
                }}
              />
            </label>
            <p className="text-xs text-sand-500">
              JPEG, PNG or WebP, up to 12&nbsp;MB. Photos are resized for the web and
              their location data is removed before anything is stored.
            </p>

            <details className="text-sm">
              <summary className="cursor-pointer text-sand-600">
                Or paste an image address
              </summary>
              <div className="mt-2 flex gap-2">
                <Input
                  value={imageUrl}
                  onChange={(e) => setImageUrl(e.target.value)}
                  placeholder="https://…"
                  aria-label="Image URL"
                />
                <Button
                  variant="secondary"
                  onClick={() => addImage.mutate()}
                  loading={addImage.isPending}
                  disabled={!imageUrl.startsWith('http')}
                >
                  Add
                </Button>
              </div>
            </details>
          </Card>
        )}
        </div>

        {/* Sticky, because it is the part being adjusted while the left side
            is read back. */}
        <div className="space-y-5 lg:sticky lg:top-20">
        {form.type === 'event' && (
          <Card className="space-y-3 p-5">
            <h2 className="flex items-center gap-2 text-sm font-medium text-sand-700">
              <Clock className="size-4" aria-hidden />
              When does it happen?
            </h2>
            {/* Editable before anything is saved. Dates used to need a listing
                to hang off, which is true of the row and was never a reason to
                make somebody save first and come back. */}
            {dates.length > 0 && (
              <ul className="space-y-1.5">
                {dates.map((date) => (
                  <li
                    key={date.id ?? date.startTime}
                    className="flex items-center justify-between gap-2 rounded-lg bg-sand-100 px-3 py-2 text-sm text-sand-700"
                  >
                    <span>
                      {new Date(date.startTime).toLocaleString(undefined, {
                        weekday: 'short',
                        day: 'numeric',
                        month: 'short',
                        hour: '2-digit',
                        minute: '2-digit',
                      })}
                    </span>
                    {/* Only one not yet written down can be taken back here.
                        Removing a date people may already hold tickets for is a
                        different act with different consequences. */}
                    {date.id === null && (
                      <Button
                        variant="ghost"
                        size="sm"
                        aria-label="Remove this date"
                        onClick={() =>
                          setDates(dates.filter((d) => d.startTime !== date.startTime))
                        }
                      >
                        <X className="size-4" aria-hidden />
                      </Button>
                    )}
                  </li>
                ))}
              </ul>
            )}
            <div className="flex gap-2">
              <Input
                type="datetime-local"
                value={dateInput}
                onChange={(e) => setDateInput(e.target.value)}
                aria-label="Date and time"
              />
              <Button
                variant="secondary"
                disabled={!dateInput}
                onClick={() => {
                  if (!dateInput) return
                  setDates([...dates, { id: null, startTime: dateInput }])
                  setDateInput('')
                }}
              >
                Add
              </Button>
            </div>
          </Card>
        )}

        {/* Price and tickets together, because they are one decision. The
            headline figure is what a card shows; the tickets are what somebody
            actually buys, and having them in different parts of the form meant
            publishers set the first and never found the second. */}
        <Card className="space-y-4 p-5">
          <h2 className="flex items-center gap-2 text-sm font-medium text-sand-700">
            <Ticket className="size-4" aria-hidden />
            What does it cost?
          </h2>

            <div>
              <label htmlFor="price" className="mb-1.5 block text-sm font-medium text-sand-700">
                Price
              </label>
              <div className="flex gap-2">
                <select
                  id="price"
                  value={form.priceType}
                  onChange={(e) =>
                    setForm({
                      ...form,
                      priceType: e.target.value as CreatePostInput['priceType'],
                      priceAmount: e.target.value === 'free' ? null : form.priceAmount,
                    })
                  }
                  className="h-11 rounded-lg border border-sand-300 bg-white px-3 text-sm focus:border-brand-600 focus:outline-none focus:ring-2 focus:ring-brand-600/20"
                >
                  <option value="free">Free</option>
                  <option value="fixed">Fixed</option>
                  <option value="range">From</option>
                </select>
                {form.priceType !== 'free' && (
                  <>
                    <Input
                      type="number"
                      min={0}
                      value={form.priceAmount ?? ''}
                      onChange={(e) =>
                        setForm({ ...form, priceAmount: e.target.value ? Number(e.target.value) : null })
                      }
                      placeholder={effectiveCurrency}
                    />
                    {/* Defaults to the city's own currency, which is right
                        almost always, and is overridable because it is not
                        always: a tour priced in dollars in a city that is not. */}
                    <select
                      value={effectiveCurrency}
                      onChange={(e) => setForm({ ...form, currency: e.target.value })}
                      aria-label="Currency"
                      className="h-11 rounded-lg border border-sand-300 bg-white px-3 text-sm focus:border-brand-600 focus:outline-none focus:ring-2 focus:ring-brand-600/20"
                    >
                      {currencyChoices.map((code) => (
                        <option key={code} value={code}>
                          {code}
                        </option>
                      ))}
                    </select>
                  </>
                )}
              </div>
              {form.priceType !== 'free' && (
                <p className="mt-1.5 text-xs text-sand-500">
                  This is the headline price people see on the card. To actually sell
                  tickets - general admission, VIP, VVIP, each with its own price and
                  what it includes -{' '}
                  {form.type !== 'event'
                    ? 'set the type to Event, then add a date below and put tickets on it.'
                    : draftId
                      ? 'add a date below and put tickets on it.'
                      : 'save this first, then add a date below and put tickets on it.'}
                </p>
              )}
            </div>

          {form.type === 'event' && (
            <TicketPlanEditor
              tickets={tickets}
              onChange={setTickets}
              currency={effectiveCurrency}
              dateCount={dates.length}
            />
          )}

          {form.type !== 'event' && form.priceType !== 'free' && (
            <p className="text-xs text-sand-500">
              Only an event sells tickets, because a ticket is for a date. A place
              or an activity shows this price and people pay when they arrive.
            </p>
          )}

        </Card>


        {/* Shown continuously rather than only on failure, so nothing is a
            surprise at the moment of publishing. */}
        {outstanding.length > 0 && (
          <Card className="border-accent-300 bg-accent-100/40 p-4">
            <p className="flex items-center gap-2 text-sm font-medium text-accent-700">
              <AlertTriangle className="size-4" aria-hidden />
              Before this can go live
            </p>
            <ul className="mt-2 space-y-1 pl-6 text-sm text-accent-700">
              {outstanding.map((problem) => (
                <li key={problem} className="list-disc">
                  {problem}
                </li>
              ))}
            </ul>
          </Card>
        )}

        {post?.moderationStatus === 'pending' && (
          <Card className="border-accent-300 bg-accent-100/40 p-4 text-sm text-accent-700">
            This post is waiting for a quick review before it appears in discovery.
            {post.moderationNotes && <p className="mt-1 text-xs">{post.moderationNotes}</p>}
          </Card>
        )}
        </div>
      </div>

      {/* Sticky action bar: saving and publishing stay reachable however long the
          form gets. */}
      <div className="fixed inset-x-0 bottom-0 z-30 border-t border-sand-200 bg-white/95 backdrop-blur sm:bottom-0">
        <div className="mx-auto flex max-w-6xl items-center justify-between gap-3 px-4 py-3 sm:px-6">
          <div className="min-w-0 text-sm text-sand-500">
            {draftId ? (
              <span className="flex items-center gap-1.5">
                <Badge tone={post?.status === 'published' ? 'success' : 'neutral'}>
                  {post?.status ?? 'draft'}
                </Badge>
                {commit.isPending
                  ? 'Saving…'
                  : post?.status === 'published'
                    ? 'Live'
                    : 'Saved as a draft'}
              </span>
            ) : (
              'Not saved yet'
            )}
          </div>
          <div className="flex shrink-0 gap-2">
            {/* Both do the whole job. Save writes everything down and leaves it
                a draft; Publish writes the same things down and then makes it
                live. Neither is a prerequisite for the other, which is what
                "save first, then publish" had quietly made them. */}
            <Button
              variant="secondary"
              onClick={() => commit.mutate({ thenPublish: false })}
              loading={commit.isPending && !publishing}
              disabled={!canSave || commit.isPending}
            >
              Save
            </Button>
            <Button
              onClick={() => {
                setPublishing(true)
                commit.mutate({ thenPublish: true })
              }}
              loading={commit.isPending && publishing}
              disabled={outstanding.length > 0 || commit.isPending}
            >
              Publish
            </Button>
          </div>
        </div>
      </div>
    </div>
  )
}
