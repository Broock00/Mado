# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

`README.md` covers what Mado is, the domain taxonomy, and first-time setup. Read it
first. This file covers what is easy to get wrong afterwards.

**The README is partly out of date.** It says payments and the developer portal are
not started (both are built), quotes 414 tests (there are 1111), and describes Addis
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
`embeddings`, `media_storage`. Each is a Protocol with two or three implementations
and a `get_provider()` chosen by config. Swapping vendors is a settings change, and
no vendor type reaches a caller.

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
Countries, cities, districts, streets and landmarks are resolved at request time from
OpenStreetMap (keyless, default) or Google, through `app/integrations/places.py`, and
never curated into tables. **Never add a table of places, and never make a curated
row a precondition for a venue or a search.**

Discovery is scoped by an `Area` (`app/domains/catalog/repository.py`), which is one
of three things, because one shape does not fit all:

- a **point and radius** — a street, a neighbourhood; widens through
  `RADIUS_STEPS_KM` (2, 5, 15, 40 km) until there are enough candidates
- a **bounding box** — a borough or a city, intersected against `venues.geo`
- a **country code** — exact, and the only thing that works for a country: a box
  around Kenya covers four neighbours, and the one around the United States spans
  360° of longitude because of the Pacific territories

A `City` row still exists as a label and as the target of a venue's foreign key. It
is not the scoping key, and anything that reintroduces it as one is a regression.

`ST_MakeEnvelope` takes west, south, east, north — a different order from the
south, west, north, east that geocoders report.

### Ranking belongs to the platform

Meilisearch returns candidates; `app/domains/discovery/ranking.py` decides order and
produces the explanation shown on each card. Search filters by city slug only — the
index cannot do geometry, so an `Area` that is a box or a country goes to the
database instead.

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
