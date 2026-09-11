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
import { isLocationReady, useAppStore } from '@/app/store'
import type { PickedLocation } from '@/features/map/LocationPicker'
import { CityPicker } from '@/features/map/CityPicker'
import { FALLBACK_CENTRE, isAreaPick } from '@/features/map/types'
import { Badge, Button, Card, Input } from '@/design-system/primitives'
import { toMajorInput, toMinor } from '@/lib/money'
import { cn } from '@/lib/utils'
import type { CreatePostInput, OwnPost, Place, SuitabilitySlug } from '@/lib/types'
import { SUITABILITY_GROUPS, suitabilityLabel } from '@/lib/suitability'
import { TicketPlanEditor } from '@/features/commerce/TicketPlanEditor'
import type { DraftTicket } from '@/features/commerce/TicketPlanEditor'
import { WritingHelp } from './WritingHelp'

/** What the listing's own price becomes, once it is a thing to buy. */
export const ORDINARY_TICKET = 'General admission'

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
  const explorerLocation = useAppStore((s) => s.location)
  // Wherever they were last looking on the discovery page. Only a starting
  // position for the map, never the answer.
  const browsingPlace = useAppStore((s) => s.place)

  // Kept only to populate the currency list. A post's city is no longer chosen
  // from this - it is worked out from where the pin is - so a city Mado has
  // never heard of is now perfectly postable.
  const { data: cities } = useQuery({ queryKey: ['cities'], queryFn: () => api.cities() })

  const [draftId, setDraftId] = useState<string | null>(experienceId ?? null)
  const [error, setError] = useState<string | null>(null)

  const [form, setForm] = useState<CreatePostInput>({
    title: '',
    description: '',
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
  // Held apart from `form` because it is a set being toggled rather than a field
  // being typed, and because an empty array and an absent key mean the same
  // thing here - nobody has claimed anything.
  const [suitability, setSuitability] = useState<SuitabilitySlug[]>([])

  const toggleSuitability = (slug: SuitabilitySlug) =>
    setSuitability((current) =>
      current.includes(slug)
        ? current.filter((item) => item !== slug)
        : [...current, slug],
    )

  // Which publisher this is posted by. Null means the personal publisher, which
  // is what the server assumes when the field is absent - so the default costs
  // nothing and needs no extra request.
  //
  // Seeded from `?publisher=` so "Post as <business>" on the business dashboard
  // arrives here already pointing at the right one.
  const [publisherId, setPublisherId] = useState<string | null>(
    () => new URLSearchParams(window.location.search).get('publisher'),
  )

  // Who this account may post as, answered by the server rather than assembled
  // here. Usually one entry - an individual posts as themselves, a business as
  // itself - and the control below never appears. A second entry exists only for
  // somebody invited to another business, who genuinely has two identities.
  const { data: identities } = useQuery({
    queryKey: ['publishing-identities'],
    queryFn: () => api.publishingIdentities(),
    staleTime: 5 * 60_000,
  })

  const publisherOptions = useMemo(
    () =>
      (identities ?? []).map((identity) => ({
        // The default identity is sent as null: the server resolves it from the
        // account, and hard-coding an id here would be a second answer to
        // "who is publishing this" that could disagree with it.
        id: identity.isDefault ? null : identity.id,
        name: identity.name,
      })),
    [identities],
  )

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
      type: existing.type,
      summary: existing.summary ?? '',
      categorySlug: existing.category?.slug ?? null,
      priceType: existing.price.type,
      priceAmount: existing.price.amount ?? null,
      currency: existing.price.currency ?? null,
    })
    setVenueId(existing.venue?.id ?? null)
    setDraftId(existing.id)
    // Shown, not changed: the selector is disabled while editing.
    setPublisherId(existing.publisher?.id ?? null)
    // The card carries the union of the listing's claims and its venue's, so it
    // is filtered back to what this listing itself can edit. Seeding the union
    // would let a publisher "untick" a venue facility from here and then find it
    // still true, because this form does not own that record.
    setSuitability(
      (existing.suitability ?? []).filter(
        (slug) => !(existing.venue?.facilities ?? []).includes(slug),
      ),
    )
    setDates(
      (existing.upcomingEvents ?? [])
        .filter((event) => event.status !== 'cancelled')
        .map((event) => ({ id: event.id, startTime: event.startTime })),
    )
  }, [existing])

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

  /**
   * Where the map opens before anything has been placed.
   *
   * Best available guess, in the order it is worth trusting: where the explorer
   * actually is, then wherever they were last looking on the discovery page,
   * then the platform's first city. None of these is the answer - the pin is -
   * so being wrong here only costs a pan, where withholding the map entirely
   * used to cost the whole post.
   */
  const mapCentre = useMemo(() => {
    if (isLocationReady(explorerLocation)) {
      return { latitude: explorerLocation.latitude!, longitude: explorerLocation.longitude! }
    }
    if (browsingPlace) {
      return { latitude: browsingPlace.latitude, longitude: browsingPlace.longitude }
    }
    return FALLBACK_CENTRE
  }, [explorerLocation, browsingPlace])

  // Where it is decides, unless the publisher says otherwise. Sending nothing
  // lets the server apply the same rule from the venue's own city, so the two
  // cannot disagree.
  const effectiveCurrency = form.currency ?? picked?.currency ?? 'ETB'
  // The currency of wherever the pin is, plus every currency Mado already has a
  // city in - which now grows on its own, because a city row is created the
  // first time somebody posts in one.
  const currencyChoices = Array.from(
    new Set([effectiveCurrency, ...(cities ?? []).map((c) => c.currency)]),
  ).sort()

  /**
   * Whether the pin stands for a whole city rather than one address.
   *
   * True right after a city is chosen above and false again the moment the pin
   * is moved to a building. The only thing it changes is whether the pin can
   * name the venue for us.
   */
  const cityOnly = isAreaPick(picked?.kind)

  /**
   * Whether the location is in a state that can actually become a venue.
   *
   * One condition, used by both buttons and by the outstanding list, because
   * the three disagreeing is what produced the bug this replaces: the list
   * accepted a bare pin, the commit required a typed name, and `canSave`
   * required neither - so Save on a pin with no name sent no venue at all and
   * the server refused the post for having no city.
   */
  const hasUsableLocation = Boolean(
    venueId || (picked && (venue.name.trim() || (!cityOnly && picked.label?.trim()))),
  )

  // A location is needed even to save a draft. `experiences.city_id` is NOT
  // NULL and the city is derived from the pin, so a post with no location is a
  // row the database will not accept - and letting Save try anyway produced a
  // refusal naming a city field the composer deliberately does not have.
  const canSave =
    form.title.trim().length >= 4 &&
    form.description.trim().length > 0 &&
    hasUsableLocation

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
    if (!form.categorySlug) missing.push('Choose a category so people can find it.')
    if (!venueId && !picked) missing.push('Add a location.')
    // A pin with nothing to call it cannot become a venue, and a post with no
    // venue has no city - which the server refuses. Asked for here, where the
    // field is, rather than surfaced later as a refusal about a city.
    else if (!hasUsableLocation) missing.push('Give the place a name.')
    if (form.type === 'event' && dates.length === 0)
      missing.push('Add at least one date and time.')
    if (form.priceType !== 'free' && form.priceAmount == null)
      missing.push('Set a price, or mark it as free.')
    return missing
  }, [form, venueId, picked, hasUsableLocation, dates])

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
          // No city. The server works it out from these coordinates and
          // materialises the row if it has never seen that city before, which
          // is what lets somebody post from a town nobody had typed in.
          latitude: picked.latitude,
          longitude: picked.longitude,
          // Present only when the pin came from a search result and was not
          // moved afterwards. Recorded so the same address added twice can be
          // recognised as the same address; nothing reads it back.
          placeId: picked.placeId,
        })
        venue = created.id
        setVenueId(created.id)
      }

      // 2. The listing. `suitability` is always sent, including empty, so
      //    unticking the last claim actually withdraws it - omitting the key
      //    would leave the old set standing and there would be no way to take
      //    back a promise the kitchen can no longer keep.
      const payload = {
        ...form,
        venueId: venue,
        suitability,
        // Only on create. `publisherId` is not an editable field server-side,
        // and sending it on an update would be asking for something the API
        // correctly ignores.
        ...(draftId || !publisherId ? {} : { publisherId }),
      }
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

      // What the card says has to be what somebody can actually pay. The
      // ticketing view already derives its price from the tiers; the listing's
      // own fields feed search and the free-only filter, so leaving them
      // behind would file a paid event as free.
      if (hasDates && tickets.length > 0) {
        const prices = tickets.map((ticket) => ticket.priceMinor).sort((a, b) => a - b)
        const low = prices[0]
        const high = prices[prices.length - 1]
        const shape = high === 0 ? 'free' : low === high ? 'fixed' : 'range'
        // Through the same table the rest of the money handling uses, rather
        // than dividing by a hundred - yen has no minor unit, and nor do four
        // other currencies the catalogue already contains.
        const amount = high === 0 ? null : Number(toMajorInput(low, effectiveCurrency))
        if (shape !== form.priceType || amount !== (form.priceAmount ?? null)) {
          await api.updatePost(saved.id, { priceType: shape, priceAmount: amount })
          setForm((current) => ({ ...current, priceType: shape, priceAmount: amount }))
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

  /**
   * The price a publisher types *is* a ticket - the ordinary one.
   *
   * It was not, and the result was quietly wrong in two directions: a listing
   * priced at 300 with a VIP tier added showed 800 on the card, because the
   * displayed price is derived from the tiers, and offered no way at all to buy
   * the 300 one. The publisher had typed a price that nobody could pay.
   *
   * So setting a price on an event puts a general admission ticket in the list,
   * where it is visible and editable like any other. Seeded once rather than
   * kept in sync in both directions: after it exists the list is the truth, and
   * a price field quietly overwriting a ticket somebody had adjusted would be
   * the same class of bug pointing the other way.
   */
  useEffect(() => {
    if (form.type !== 'event') return
    if (form.priceType === 'free' || form.priceAmount == null) return
    if (tickets.length > 0) return
    setTickets([
      {
        name: ORDINARY_TICKET,
        description: '',
        priceMinor: toMinor(String(form.priceAmount), effectiveCurrency),
        quantity: null,
      },
    ])
  }, [form.type, form.priceType, form.priceAmount, tickets.length, effectiveCurrency])

  // Read at commit time rather than closed over, so a name typed a moment ago
  // is the one that gets used.
  //
  // Falls back to whatever the pin already knows itself as. A location chosen
  // from the place search arrives with a label - "Piassa Roasters" - and making
  // somebody retype that is friction for no gain. It also closes a hole: the
  // readiness check accepted a bare pin as a location while this required a
  // typed name, so a pin with an empty name field sent no venue at all and the
  // server refused the post for having no city, naming a field the composer
  // deliberately does not have.
  /**
   * A city chosen from the place list becomes the pin.
   *
   * Deliberately the same state a tap on the map produces, rather than a second
   * field holding a city name of its own. A client-named city would let two
   * venues on the same street file under different ones, so the server works the
   * city out by reverse-geocoding whatever coordinates arrive - and the way to
   * tell it a city is therefore to put the pin in that city. Moving the pin
   * afterwards refines the answer; the city follows it.
   */
  function chooseCity(place: Place) {
    setPicked({
      latitude: place.latitude,
      longitude: place.longitude,
      label: place.label,
      // The identifier of a city, which is not the identifier of the venue this
      // is about to become. Saving it would record the wrong place against the
      // row and make two venues in the same city look like the same address.
      placeId: null,
      area: place.area || place.label,
      currency: place.currency ?? null,
      kind: place.kind ?? null,
      attributions: place.attributions ?? [],
    })
  }

  function venue_name() {
    if (venue.name.trim()) return venue.name.trim()
    // Never for a city: "Nairobi" is where the venue is, not what it is called,
    // and a venue row named after its own city is useless to everybody who
    // reads it afterwards.
    return cityOnly ? '' : picked?.label?.trim() || ''
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

      {/* Who this is published by. Only shown when there is a choice to make -
          most people post as themselves and should never see a control asking
          them to confirm it.

          Fixed once the post exists: moving a listing between publishers would
          move it between the people accountable for it, and reviews, bookings
          and moderation history all point at the original. */}
      {publisherOptions.length > 1 && (
        <div className="mt-4">
          <label htmlFor="publisher" className="mb-1 block text-sm text-sand-700">
            Posting as
          </label>
          <select
            id="publisher"
            value={publisherId ?? ''}
            onChange={(e) => setPublisherId(e.target.value || null)}
            disabled={isEditing}
            className="w-full max-w-sm rounded-lg border border-sand-300 bg-sand-100 px-3 py-2 text-sm text-sand-900 disabled:bg-sand-100"
          >
            {publisherOptions.map((option) => (
              <option key={option.id ?? 'me'} value={option.id ?? ''}>
                {option.name}
              </option>
            ))}
          </select>
          {isEditing && (
            <p className="mt-1 text-xs text-sand-500">
              A post stays with whoever published it.
            </p>
          )}
        </div>
      )}

      {error && (
        <p role="alert" className="mt-4 rounded-lg bg-red-950 px-3 py-2 text-sm text-red-300">
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
                      ? 'border-brand-500 bg-brand-900/40'
                      : 'border-sand-300 hover:bg-sand-200',
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
              className="w-full rounded-lg border border-sand-300 bg-sand-100 px-3.5 py-2.5 text-sm placeholder:text-sand-400 focus:border-brand-600 focus:outline-none focus:ring-2 focus:ring-brand-600/20"
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

          {/* The city is asked under "Where is it?", next to the map that
              answers it, rather than up here among the words. It used to be a
              dropdown of the ten cities somebody had typed into a table, which
              was a ceiling on where the platform could be used; it is now the
              place provider's list, so every city on earth is in it. */}
          <div>
            <div>
              <label htmlFor="category" className="mb-1.5 block text-sm font-medium text-sand-700">
                Category
              </label>
              <select
                id="category"
                value={form.categorySlug ?? ''}
                onChange={(e) => setForm({ ...form, categorySlug: e.target.value || null })}
                className="h-11 w-full rounded-lg border border-sand-300 bg-sand-100 px-3 text-sm focus:border-brand-600 focus:outline-none focus:ring-2 focus:ring-brand-600/20"
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
            <p className="flex items-center gap-2 rounded-lg bg-brand-900/30 px-3 py-2 text-sm text-brand-300">
              <Check className="size-4" aria-hidden />
              {venue.name || post?.venue?.name || 'Location added'}
            </p>
          ) : (
            <>
              {/* Above the map, because it is the coarse answer somebody
                  already knows and the map is the fine one they are about to
                  give. Choosing a city moves the pin there, so the map opens on
                  the right place instead of wherever the explorer happens to
                  be - which is what a publisher adding a venue in another city
                  had to pan across a continent to fix. */}
              <CityPicker
                value={picked?.area ?? null}
                centre={mapCentre}
                onChange={chooseCity}
              />

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
              {/* Always rendered now. It used to be withheld until a city had
                  been chosen or location shared, which meant the one control
                  that can answer "where is it" was hidden behind answering
                  "where is it". The map opens somewhere sensible and search
                  moves it anywhere on earth. */}
              <Suspense
                fallback={<div className="h-72 w-full animate-pulse rounded-xl bg-sand-200" />}
              >
                <LocationPicker centre={mapCentre} value={picked} onChange={setPicked} />
              </Suspense>

              {picked?.area && (
                <p className="text-sm text-sand-600">
                  Filed under <span className="text-sand-900">{picked.area}</span>.
                </p>
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

        {/* Collapsed by default and never required. These are what let somebody
            search for a vegan kitchen or step-free access and get an answer
            rather than a hopeful list - but a publisher who skips the whole
            section has done nothing wrong, because saying nothing is exactly
            what an unticked box means. Making any of it mandatory would trade a
            true "unknown" for a guessed answer. */}
        <Card className="space-y-3 p-5">
          <details className="group">
            <summary className="flex cursor-pointer items-center gap-2 text-sm font-medium text-sand-700">
              <Check className="size-4" aria-hidden />
              What is it good for?{' '}
              <span className="font-normal text-sand-400">(optional)</span>
              {suitability.length > 0 && (
                <span className="ml-auto rounded-full bg-brand-100 px-2 py-0.5 text-xs text-brand-800">
                  {suitability.length}
                </span>
              )}
            </summary>

            <p className="mt-3 text-xs text-sand-500">
              Only tick what is true every time you are open. Leaving something
              unticked means “not said”, never “no” — so an empty box costs you
              nothing, and a wrong tick sends somebody who cannot use stairs up a
              flight of them.
            </p>

            <div className="mt-4 space-y-5">
              {SUITABILITY_GROUPS.map((group) => (
                <fieldset key={group.key} className="space-y-2">
                  <legend className="text-xs font-medium uppercase tracking-wide text-sand-500">
                    {group.label}
                  </legend>
                  {group.hint && <p className="text-xs text-sand-500">{group.hint}</p>}
                  <div className="flex flex-wrap gap-2">
                    {group.slugs.map((slug) => {
                      const on = suitability.includes(slug)
                      return (
                        <button
                          key={slug}
                          type="button"
                          aria-pressed={on}
                          onClick={() => toggleSuitability(slug)}
                          className={
                            on
                              ? 'rounded-full border border-brand-600 bg-brand-600 px-3 py-1 text-xs text-white'
                              : 'rounded-full border border-sand-300 px-3 py-1 text-xs text-sand-700 hover:bg-sand-200'
                          }
                        >
                          {suitabilityLabel(slug)}
                        </button>
                      )
                    })}
                  </div>
                </fieldset>
              ))}
            </div>
          </details>
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
            <label className="flex cursor-pointer items-center justify-center gap-2 rounded-lg border border-dashed border-sand-300 px-4 py-6 text-sm text-sand-600 hover:bg-sand-200">
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
                  className="h-11 rounded-lg border border-sand-300 bg-sand-100 px-3 text-sm focus:border-brand-600 focus:outline-none focus:ring-2 focus:ring-brand-600/20"
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
                      className="h-11 rounded-lg border border-sand-300 bg-sand-100 px-3 text-sm focus:border-brand-600 focus:outline-none focus:ring-2 focus:ring-brand-600/20"
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
                  {form.type === 'event'
                    ? 'This becomes your ordinary ticket, below. Add more for VIP or anything else you offer.'
                    : 'What people pay when they arrive. Only an event sells tickets in advance, because a ticket is for a date.'}
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
          <Card className="border-accent-300/40 bg-accent-100/40 p-4">
            <p className="flex items-center gap-2 text-sm font-medium text-accent-300">
              <AlertTriangle className="size-4" aria-hidden />
              Before this can go live
            </p>
            <ul className="mt-2 space-y-1 pl-6 text-sm text-accent-300">
              {outstanding.map((problem) => (
                <li key={problem} className="list-disc">
                  {problem}
                </li>
              ))}
            </ul>
          </Card>
        )}

        {post?.moderationStatus === 'pending' && (
          <Card className="border-accent-300/40 bg-accent-100/40 p-4 text-sm text-accent-300">
            This post is waiting for a quick review before it appears in discovery.
            {post.moderationNotes && <p className="mt-1 text-xs">{post.moderationNotes}</p>}
          </Card>
        )}
        </div>
      </div>

      {/* Sticky action bar: saving and publishing stay reachable however long the
          form gets. */}
      <div className="fixed inset-x-0 bottom-0 z-30 border-t border-sand-200 bg-sand-100/95 backdrop-blur sm:bottom-0">
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
