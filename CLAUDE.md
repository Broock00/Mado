# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

`README.md` covers what Mado is, the domain taxonomy, and first-time setup. Read it
first. This file covers what is easy to get wrong afterwards.

**The README is partly out of date.** It says payments and the developer portal are
not started (both are built), quotes 414 tests (there are 1300), and describes Addis
Ababa as *the* pilot city. That last one is now wrong in a way that matters — see
"Geography is not our data" below.

## Commands

Everything below assumes Windows paths, because this is where it is developed. On
Linux use `.venv/bin/python` and run `uvicorn app.main:app` directly.

```bash
# Backend, from backend/
.venv/Scripts/python.exe run.py                 # serve on 127.0.0.1:8000
.venv/Scripts/python.exe -m pytest tests/ -q    # whole suite
.venv/Scripts/python.exe -m pytest tests/test_ranking.py::TestProximityBeatsPopularity -q
.venv/Scripts/python.exe -m ruff check app tests --fix
.venv/Scripts/python.exe -m alembic upgrade head
.venv/Scripts/python.exe -m alembic revision --autogenerate -m "what changed"

# Seeding
.venv/Scripts/python.exe -m app.seed             # Addis Ababa + reindex + embeddings
.venv/Scripts/python.exe -m app.seed --reindex   # search index only
.venv/Scripts/python.exe -m app.seed --demo-cities   # invented listings in NY/London/Nairobi
.venv/Scripts/python.exe -m app.seed --remove-demo
.venv/Scripts/python.exe -m tests.cleanup        # purge leftover test data by hand

# Frontend, from frontend/web/
npm run dev
npx tsc -b            # typecheck — NOT `tsc --noEmit`, see below
npx oxlint src
npx vitest run
npx vitest run src/lib/money.test.ts
```

### `tsc --noEmit` checks nothing here

The root `tsconfig.json` is solution-style (`"files": []` plus references), so plain
`tsc --noEmit` compiles zero files and exits 0 with any number of type errors in the
tree. Use **`npx tsc -b`**.

CI's dedicated "Typecheck" step runs the no-op form; the `Build` step
(`tsc -b && vite build`) is what actually catches type errors there.

### Most of the suite needs a running API

The integration tests talk to `127.0.0.1:8000` and **fail rather than skip** when it
is unreachable — deliberately, because a skip reads as a pass. Start the API first,
or opt out with `MADO_SKIP_API_TESTS=1`.

Run the suite with `MADO_AI_PROVIDER=stub` **on the server**, not just on pytest.
Two concierge tests assert on wording and on whether the assistant asks a clarifying
question; against a live model those are a coin toss, and they skip themselves when
the server reports `ai: unknown` on `/health/ready` rather than `ai: disabled`.

Set `MADO_WEATHER_PROVIDER=none` too. With a real provider, a ranking assertion
depends on the actual weather in whatever city the fixture invented.

Test data cleans itself up: a session teardown in `conftest.py` deletes everything
owned by an account at `mado-qa.example.org`. Do not remove it. Before it existed
the database reached 3,038 accounts and 1,626 listings against 30 real ones, and the
concierge was recommending "An Evening of Spoken Word at Test Venue 0bf7" to the
person developing it.

## Architecture

A modular monolith. Domains own a Postgres schema and do not read each other's
tables; a foreign key into `identity.users` is the accepted exception.

| Schema | Domain | Holds |
|---|---|---|
| `identity` | identity, trust/audit | users, sessions, tokens, audit log |
| `publisher` | publisher, developer | publishers, API keys, webhooks |
| `catalog` | catalog | cities, venues, experiences, events, media |
| `explorer` | explorer | saved items, reviews, reservations, itineraries, notifications |
| `commerce` | commerce | ticket types, orders, tickets, payment events |
| `ai` | ai | conversations, messages, memories |

`app/models.py` imports every model. Alembic autogenerate and the test fixtures both
depend on it — a domain no route happens to import would silently vanish from
migrations otherwise.

### Vendors are held behind interfaces in `app/integrations/`

`ai_provider`, `places`, `geocoding`, `routing`, `search`, `payments`, `email`,
`embeddings`, `media_storage`, `timezones`, `weather`. Each is a Protocol with two or
three implementations and a `get_provider()` chosen by config. Swapping vendors is a
settings change, and no vendor type reaches a caller.

Every one ships a stub, and **the stubs never invent a result**. The AI stub composes
replies only from tool output; the places stub resolves nowhere; the payments stub
holds a payment at `pending` until something explicitly settles it. A stub that
returns something plausible is indistinguishable from a working integration until it
reaches production — that rule has already caught two bugs here and is worth keeping.

### Tools run before generation

`app/domains/ai/gateway.py` classifies, plans, executes tools, then asks the model to
phrase what came back (spec 56.01 §3.1). The model never decides a fact.

**The gateway swallows tool failures on purpose** (§3.8, safe failure) — so a tool
raising `TypeError` produces a fluent reply built from a fallback rather than an
error. That is how a broken search hid for several commits, answering every question
with the same generic list. `tests/test_concierge_tools.py` exists for this and
asserts that two unrelated questions get two different answers; a "does it return
results" test passes throughout the failure.

### Geography is not our data

The platform stores its own events, venues, explorers and ranking intelligence.
Countries, cities, districts, streets and landmarks are resolved at request time
through `app/integrations/places.py` and never curated into tables. **Never add a
table of places, and never make a curated row a precondition for a venue or a
search.**

**Google Places API (New) is the primary provider**; OpenStreetMap/Nominatim is the
keyless fallback and is genuinely worse, not equivalent — it matches whole names and
predicts nothing. A location search is two calls, and the split is deliberate:
`autocomplete` while somebody types (names and ids, no coordinates), then `details`
for the one they choose. A `sessionToken` passed through both bills the whole search
as one lookup instead of one per keystroke, so never drop it, never reuse one, and
never cache predictions across sessions.

Requests carry an `X-Goog-FieldMask` naming only what `Place` needs — the API refuses
a request without one and bills by the most expensive field named. Adding a field
there is a billing change; opening hours, ratings, photos and reviews are all things
Mado holds its own opinion of and must never appear in it.

`place_id` is the one field Google's terms allow to be stored indefinitely, which is
why it is the only one stored (`catalog.venues.place_id`, written at publishing time
and read by nothing). Everything else is cached for an hour against the thirty days
permitted. `Place.attributions` must be displayed wherever the place is.

**Reverse geocoding is not part of the Places API.** `describe()` talks to the
Geocoding API instead — a different host, a different product, and two traps that
both fail silently:

- It does **not** read `X-Goog-Api-Key`. The key must go in the `key` query
  parameter; sent as a header it replies "You must use an API key" as though none
  were sent. (`redact()` scrubs `key=` from logs, which is what makes that safe.)
- It reports refusals with **HTTP 200** and `status: REQUEST_DENIED`, so
  `raise_for_status` sees nothing wrong. Always check `status` — otherwise a bad key
  is indistinguishable from the middle of the sea.

Discovery is scoped by an `Area` (`app/domains/catalog/repository.py`), which is one
of three things, because one shape does not fit all:

- a **point and radius** — a street, a neighbourhood; widens through
  `RADIUS_STEPS_KM` (2, 5, 15, 40 km) until there are enough candidates
- a **bounding box** — a borough or a city, intersected against `venues.geo`
- a **country code** — exact, and the only thing that works for a country: a box
  around Kenya covers four neighbours, and the one around the United States spans
  360° of longitude because of the Pacific territories

A `City` row still exists as a label and as the target of a venue's foreign key
(`catalog.venues.city_id` and `catalog.experiences.city_id` are both NOT NULL, and
the concierge reads `city.timezone` to know when "tonight" is). It is not the
scoping key, and anything that reintroduces it as one is a regression.

**A city is a consequence, never a precondition.** `app/domains/catalog/cities.py`
materialises the row from wherever a pin was dropped: the venue's coordinates are
reverse-geocoded server-side, and the city is found-or-created from the result.
`city_slug` is therefore optional on venue and experience creation.

The composer's city field is a **place search, not a dropdown**. The dropdown it
replaced listed the ten cities somebody had typed into a table, which was a
ceiling on where the platform could be used at all; the field asks
`/places/autocomplete?citiesOnly=true`, so every inhabited place on earth is in
it and none of them needs a row first. `cities_only` is the only caller that
narrows those predictions — everywhere else a restriction is a category somebody
cannot find, which is why it is a parameter and not the default. Google gets
`includedPrimaryTypes: ["(cities)"]`, its own collection rather than a
hand-written list of types, because what a city is called differs by country;
Nominatim gets `featureType=settlement` and its results filtered again, since
that parameter is a hint and a county comes back through it.

Choosing a city **sets the pin to it** rather than sending a city name — the
coordinates still decide, and the map moves there so the publisher can refine.
A pin standing for a city is marked as such (`isAreaPick` in
`features/map/types.ts`) and cannot name the venue: falling back to its label
would create a venue called "Nairobi".

Three rules that path depends on:

- **The coordinates decide, not the client.** A client-named city lets two venues
  on the same street file under different cities.
- **`is_live` is never set from here.** It means the city passed the launch
  checklist in spec BUSINESS-08 — an editorial judgement, not a side effect of
  somebody adding a venue.
- **Rows are found before they are created**, matched on lower(name) plus country
  code, so the eleventh venue in Nairobi joins the Nairobi row.

Filling a new row needs a currency and a time zone for a country nobody
anticipated. Both come from CLDR via Babel rather than a hand-kept table
(`catalog/localisation.py`, `integrations/timezones.py`) — currencies get
redenominated and zones get split, and a hand-written table is wrong within a year
in exactly the places nobody is looking. Time zones prefer Google's Time Zone API,
because only a coordinate lookup can be right about a country with more than one
zone; the keyless fallback answers only where a country has exactly one and
re
turns None rather than guessing, since a city quietly given a neighbour's zone
shows every event at the wrong time and looks entirely normal doing it.

The Time Zone API has the same two traps as the Geocoding API — key in the query
string, refusals as HTTP 200 — and spells the message `errorMessage` where
Geocoding says `error_message`.

Test runs create city rows that the ownership-based teardown cannot see, because
a city has no owner. `tests/cleanup.py` reclaims them by emptiness instead: not
live, and nothing left pointing at it.

`ST_MakeEnvelope` takes west, south, east, north — a different order from the
south, west, north, east that geocoders report.

### Ranking belongs to the platform

Meilisearch returns candidates; `app/domains/discovery/ranking.py` decides order and
produces the explanation shown on each card. Search filters by city slug only — the
index cannot do geometry, so an `Area` that is a box or a country goes to the
database instead.

### What a listing is suitable for is a claim, not a fact

`catalog/suitability.py` holds one controlled vocabulary — dietary, family,
access, comfort, practical — stored on `catalog.experiences.suitability` and
`catalog.venues.facilities`, both string arrays with GIN indexes. The effective
set for a listing is the **union of the two**: the play area belongs to the
building, the fasting menu to the kitchen, and the same room hosts a children's
matinee at 11am and an over-18 club night at 9pm.

**Presence is a claim; absence is not a denial.** A listing that has not said
`vegan` has not said it lacks vegan food — nobody has said anything. Never store
a false, and never render an absent slug as "does not have". Cards carry
`unverified`, naming what the explorer asked about that this listing has not
answered, and the concierge is instructed to hedge on those rather than promise
or deny.

A **requirement** hard-filters, in `repository.require_suitability` (SQL) and
again in `DiscoveryService.summarize` (Python, because Meilisearch and pgvector
return ids chosen with no knowledge of these columns). Unknown is excluded along
with contradicted, deliberately: "we do not know whether this kitchen can do
nut-free" is not a maybe worth showing somebody with a nut allergy. A
**preference** only sorts, through the `fit` signal, and never excludes.

Requirements arrive from three places and are unioned: what the explorer just
said, what `ai/memory.py` remembers about them (`requirements_from`, restriction
and allergy only — a `dislikes` memory must never remove options), and their
stored profile. `IMPLIES` encodes one-directional satisfaction: a vegan kitchen
answers a request for vegetarian food, never the reverse.

The vocabulary is duplicated in `frontend/web/src/lib/suitability.ts` because the
composer must render checkboxes before it has asked the server anything. A test
in `tests/test_trip_planning.py` asserts the list in the comprehension prompt
matches the module, so drift fails a build.

### An account is a person or a business, never both

`identity.users.account_type` says which. Registration is unchanged; the account
answers the question afterwards, and `account_type_chosen_at` distinguishes
"answered individual" from "never asked" so the interface prompts once instead
of reading a default as a decision.

Choosing business converts the account. From then on it **is** the business:
its profile, the name on everything it posts, what other explorers see. There is
no personal profile alongside it and no second login. `publisher.publishers`
with `type = organization` is that profile, and `business_type` (hotel, museum,
cafe — `publisher/business.py`) is a label on it. Location and opening hours stay
on `catalog.venues`, content stays in `catalog.experiences`, and suitability
stays in its own vocabulary — so there is no `HotelService` and adding one would
duplicate all three.

Conversion is one way. Turning back would leave published listings, reviews of
them and any tickets sold attributed to something that no longer exists. A
personal publisher that already existed is **not** deleted: what they posted as
themselves stays theirs, because rewriting it to claim the business wrote it
would misstate who was accountable at the time.

**`publisher_for(user)` is the single answer to "whose name goes on this".** A
business account posts as the business, an individual as themselves, and the
composer therefore offers no choice. `POST /posts` resolves the publisher itself
for its per-publisher rate limit and passes the id down — so it must call
`publisher_for` too. It called `personal_publisher`, and a business account's
posts silently came out under the owner's own name while every check passed.

A choice appears in exactly one case: somebody **invited to another business**.
`GET /me/publishing-identities` returns it, filtered to roles that can actually
create something, so the composer never offers a button that returns 403.

**One gate.** `PublishingService.assert_can_manage(user, publisher_id,
permission=...)` is the only authorization check for a business, and
`permissions_for` is the only thing that computes what somebody holds. Owners
(`publishers.owner_user_id`) implicitly hold everything; everyone else holds
whatever their `publisher_members` row's role grants. `assert_can_publish_as` and
`_load_owned` both route through it, so a business's editor can open its drafts
and a stranger still cannot.

The owner is deliberately **not** a membership row — that would be a second
answer to "who owns this" that could disagree with the first. `owner` is
therefore not an assignable role, and nobody may change their own role, or an
administrator would simply promote themselves.

Permissions follow the `resource:action` convention from `developer/keys.py`,
including its rule: *a scope nobody enforces is worse than no scope at all*. That
is why there is no `offers:manage` or `reviews:respond` — Mado has neither
feature, and a checkbox governing nothing is a lie to whoever ticks it.

One invariant is easy to get wrong and is tested directly: **any role that can
publish must also hold `events:manage`**, because publishing an event requires a
date. An editor with `content:publish` and no `events:manage` passes every check
individually and cannot complete the job; only the end-to-end run found it.

The kind is chosen **once, at `/welcome`**, between registering and reaching
anything else — the only moment somebody expects to be set up. Signing in never
asks, and anyone who has already answered is redirected away. Converting later
lives in settings only, because the overwhelming majority of accounts will never
be a business and putting it in the navigation asks a question nobody had.

A business is tagged next to its name on its profile, on every card it posts, and
on the post detail page — where the tag is a link to everything else it has
posted. An individual publisher is not linked: there is no profile page to go to,
and a link that opens nothing is worse than plain text.

On the client, `/account-type/business` converts;
`/businesses/:id/manage` edits the business and its team; `/businesses/:slug` is
the public page and needs no account. The dashboard renders sections from
`GET /businesses/{id}/permissions` — a convenience for hiding what the caller
cannot use, never the enforcement.

`AppShell` names the account from `/me/account-type`, not from
`profile.displayName`: a business account showing the signed-in person's name is
the dual identity this model exists to avoid. It is fetched rather than read off
`/me` because the business lives in the publisher domain and identity does not
reference it.

The composer's publisher selector appears only when there is genuinely a choice,
and is **disabled while editing**: moving a listing between publishers would move
it between the people accountable for it, and its reviews, bookings and
moderation history all point at the first one.

### Reposts, and why there are no likes or comments

`explorer.reposts` is the only social table. **Likes and comments were built and
then removed**: reviews already carry a rating and a written opinion, one per
person per listing, weighted by verified attendance and feeding the listing's
average. A like is a weaker version of the rating and a comment is a weaker
version of the opinion, so keeping both meant two places to say the same thing,
two things to moderate, and a reader looking in two places to learn what people
thought. Migration `2b52eb5c077a` drops them.

A repost is not a weaker review. It says "other people should see this", which a
review does not say and cannot — so it stays. `test_social.py` asserts the removed
endpoints actually 404 rather than merely being unlinked from the interface.

**`repost_count` on `catalog.experiences` is written by `explorer/social.py` and
nowhere else.** It is denormalised because a feed renders dozens of cards each
showing it, and **recomputed from the rows** rather than incremented — an
increment is one lost request away from being permanently wrong with nothing to
notice. `is_reposted` is filled by one bulk lookup per request, the same shape as
`saved_experience_ids`.

A repost is not a save either: a save is private and means "find this again".
Nothing can be attached to a draft — otherwise an id is enough to circulate
somebody's unpublished work.

### The concierge answers about wherever the sentence names

`catalog/locate.py` is the one place a named place becomes an `Area` — the
discovery route's `place` parameter and the concierge both go through it, because
the rules (a country scopes by code, a region by its box, a radius by the kind of
place) were learned the hard way and two copies would drift.

Comprehension extracts a `destination` from the message, and the gateway's
`_locate` decides what wins: **a place named this turn**, then **a place picked in
the interface**, then **a place named earlier in the conversation** (persisted on
`conversation.state["place"]`), then the client's own area. Without the last two,
"any music nights?" after "we're going to New York" goes home, which reads as the
assistant having forgotten the only thing that mattered.

A resolved destination moves the whole context: area, timezone, currency and the
coordinates the forecast is fetched for. It also sets
`RankingContext.located_remotely`, which suppresses distances on cards and in
reasons and tells the prompt the explorer is **not there** — otherwise the reply
offers somebody in Addis a nine-minute walk to a bar in Brooklyn.

`near` is a bias and never a restriction, which is what lets "Bole" find the
neighbourhood in the explorer's own city while "New York" still reaches another
continent.

A question must never create a city row. `_locate` looks one up for planning and
accepts None; a city is a consequence of somebody publishing there, never of
somebody asking.

### Weather is a forecast, never a climate average

`integrations/weather.py` — Open-Meteo keyless (16 days), Google Weather where a
key is set (10 days), and a stub that answers nothing at all. Beyond the
provider's horizon it returns **None**, and every caller keeps that distinguishable
from "fine" all the way to the reply. A seasonal normal dressed up as a
prediction is the exact failure the stub rule exists to prevent: indistinguishable
from a working integration until somebody packs for it.

Weather scores under the ranker's `fit` signal, not `personalization` — it is a
fact about the day, not something about this explorer. `_shelter_for` matches the
mitigation to the problem, because shade does nothing about rain and a heater
does nothing about heat.

Run the test suite with `MADO_WEATHER_PROVIDER=none`. Against a real provider an
assertion about which listing ranks first depends on the actual weather in the
city a fixture invented, and starts failing when it rains there.

The Google provider has the same key-in-the-query-string trap as Geocoding and
Time Zone.

### A trip is not a long outing

`plan_outing` builds one ordered sequence inside one window. `plan_trip`
(`planning.build_trip`) builds a stay: a day plan per local calendar day, and the
three things that only exist at that level — a stop is never repeated across
days, one budget is spent down across the whole stay, and **each day is planned
against its own forecast**, which is the mechanism that puts the outdoor day on
the dry one. Routing a five-day request into `plan_outing` produced a single
itinerary from Monday morning to Friday night with a museum at 3am.

The gateway chooses between them on whether the window spans more than one
**local** calendar day — an evening in Addis crosses midnight UTC and would
otherwise become a two-day trip.

Money in a plan comes from `RankingContext.currency`, which comes from the city.
Every plan used to quote ETB in every city.

### Maps are drawn by whichever vendor is configured

`frontend/web/src/features/map/` holds two implementations of each map — Google
(`Google*.tsx`) when `VITE_GOOGLE_MAPS_API_KEY` is set, MapLibre over OpenStreetMap
tiles (`MapLibre*.tsx`) when it is not — behind three selectors, `ExperienceMap`,
`RouteMap` and `PinMap`, which are the only names a page should import. The props in
`features/map/types.ts` are the whole contract, and no vendor type appears in them.

A change to one implementation that is not made to the other is a bug: which vendor
is billing should never change how a map behaves. Neither ever asks its vendor what
is nearby — the catalogue and the ranking are the platform's (spec 82.01 §10).

The browser key is public by nature and **must be a different key** from
`MADO_GOOGLE_MAPS_API_KEY`, restricted by HTTP referrer with only the Maps JavaScript
API enabled. The server's key can spend on Places and Geocoding; a leaked one is a
bill.

### Money

Integers in minor units end to end (`price_minor`, `amount_minor`), never floats,
never `Decimal` in transit. `MINOR_UNITS` in `payments.py` carries the zero-decimal
currencies — ¥500 is 500, not 50000. On the client, `lib/money.ts` parses by reading
the digits either side of the decimal point, because `Math.round(19.99 * 100)`
happens to work while `1.005` does not.

Payments are hosted-checkout only and there is deliberately no code path that accepts
a card. Settlement always verifies with the provider that holds the money
(`provider_named(order.provider)`); a browser returning with `?status=success` proves
nothing. Stripe is fully built and disabled behind `MADO_STRIPE_ENABLED`.

## Environment

`.env` is gitignored; `.env.example` documents every setting. Nothing enforces that
the two stay in step, so after adding a setting check it by hand — every
`MADO_*` in the example should match a field on `Settings`. Worth knowing:

- `MADO_AI_PROVIDER` — `stub` by default; `gemini` needs `MADO_GEMINI_API_KEY`
- `MADO_PAYMENT_PROVIDER` — `stub` by default, so setting Chapa keys alone changes nothing
- `MADO_PAYMENT_WEBHOOK_SECRET` / `MADO_STRIPE_WEBHOOK_SECRET` — empty refuses every
  callback, which is the safe direction: an unsigned callback issues tickets
- `MADO_TELEMETRY_TOKEN` — unset means `/metrics` and `/health/ready` answer only
  outside production
- `MADO_WEBHOOK_ALLOW_PRIVATE_ENDPOINTS` — development only; it disables the SSRF guard
- The Gemini key travels in the `x-goog-api-key` header. Never a query string — this
  project leaked one that way once, and `redact()` in `app/core/logging.py` scrubs
  credential-shaped strings from logs because of it

## Platform traps

- **psycopg cannot drive Windows' default ProactorEventLoop.** `run.py` and
  `app/core/asyncio_compat.py` start a selector loop. Any standalone async script
  touching the database needs `asyncio.run(..., loop_factory=asyncio.SelectorEventLoop)`.
- **Another application of the user's listens on port 8000 over IPv6.** Never kill a
  process on that port without checking its command line contains `Mado`.
- **Vite binds IPv6 only** — `localhost:5173` works, `127.0.0.1:5173` does not.
- **Servers started in the background get reaped**, sometimes minutes later, with no
  traceback. Fine for a one-off check; ask the user to start the API themselves if it
  needs to stay up.
- **FastAPI 0.141 includes routers lazily**, so `app.routes` holds wrappers rather
  than `APIRoute`s. Walk with `fastapi.routing.iter_route_contexts(app.routes)` —
  the same enumeration OpenAPI generation uses, so paths match.
- **Alembic autogenerate proposes dropping the raw-SQL indexes** (HNSW, partial)
  every run; `alembic/env.py` excludes them by name. It also omits the
  `pgvector`/`geoalchemy2` imports, which `script.py.mako` adds unconditionally.

## House style

Comments explain *why*, at the density of the surrounding code — which is high, and
deliberately so. Say what the obvious alternative was and how it fails. Do not
restate what the line does.

Commit messages are prose, not bullet lists: what changed, what it fixes, what was
considered and rejected, and how it was verified. **Never add a `Co-Authored-By`
trailer or any AI attribution.**

Verify against a running system, not only unit tests. Several bugs here passed the
suite and failed the moment something real was driven through them — a tool signature
mismatch, a `MissingGreenlet` from serialising an unloaded relationship, an
instrumentation call to functions that did not exist. When a check passes, confirm it
was not vacuous: a source-scanning test that finds no files passes silently.
