# Mado

**An AI urban intelligence platform for discovering, planning and experiencing cities.**

Mado answers one question — *"What should I do, right here, right now?"* — by
collecting, ranking and explaining what is actually happening in a city.

It is not an events app, a map, or a travel guide. The core asset is the **City
Digital Twin**: a continuously-updated model of a city's venues, experiences,
events, publishers and their relationships.

> Mado was specified end to end under the earlier name **CityPulse**. The 227
> specification documents live outside this repository; every non-obvious decision
> in the code cites the spec section it came from.

Pilot city: **Addis Ababa, Ethiopia**.

---

## What is built

This repository currently implements the **Explorer MVP vertical slice** — a
working path from database to interface, rather than a broad but hollow scaffold.

| Area | Status |
|---|---|
| Discovery Canvas — context-ranked modules with explanations | ✅ |
| Search — Meilisearch retrieval + platform-owned ranking | ✅ |
| Experience & event detail, upcoming dates, accessibility, provenance | ✅ |
| AI Concierge — intent classification, tool execution, grounded replies, SSE streaming | ✅ |
| Identity — anonymous browsing, register/login, rotating refresh tokens | ✅ |
| Saved items, preferences, privacy controls | ✅ |
| **Publishing — anyone with an account can post** | ✅ |
| **Trust & safety — screening, reporting, moderation queue** | ✅ |
| External feed ingestion (city open data, ticketing, university calendars) | ⬜ Next |
| Bookings, ticketing, payments (Stripe / Chapa) | ⬜ Not started |
| Partner & Developer portals | ⬜ Not started |
| Mobile client | ⬜ Out of scope — web only |

## Publishing: a publisher is a person

**Anyone with an account can post**, the way anyone can post on a social platform.
There is no organization form, no verification gate and no approval queue standing
between an explorer and sharing something. The first time someone publishes, a
*personal publisher* is created automatically from their profile.

Organizations still exist — a hotel or a museum genuinely is one, and needs
several people posting under one identity — but they are the second case, not the
entry requirement. Verification is a badge earned on top, never a gate in front.

This stays aligned with the specs rather than departing from them: spec
BUSINESS-07's Level 0 is already the *"Community Publisher — basic account,
suitable for small community events and informal groups"*. What changed is that
Level 0 became the default path.

**Open publishing makes trust & safety load-bearing**, so it ships alongside:

| Layer | What it does |
|---|---|
| Accountability | Every post has an owner, including personal publishers |
| Automated screening | Cheap, explainable heuristics at publish time — scores, never blocks |
| Community reporting | Readers flag what screening missed; 3 reports withhold pending review, a scam report acts immediately |
| Human oversight | Moderators decide; a ruling locks and cannot be overturned by automation |

Two rules hold throughout, from spec BUSINESS-07: **automated systems detect,
humans decide**, and nothing is ever deleted automatically. The strongest
automatic action is withholding an item from discovery — reversible, and the
author keeps their copy and is told why.

Moderator rights are granted out of band, never through the API:

```bash
python -m app.seed --make-moderator someone@example.org
```

`is_moderator` is a column on the user, deliberately not a key in the
explorer-writable preferences blob — otherwise one careless schema change becomes
privilege escalation.

## Domain language

The taxonomy is binding and slightly counter-intuitive, so it is worth stating:

- **Experience** is the primary object — anything discoverable.
- **Event** is a *time-bound* Experience. Every Event is an Experience; not every
  Experience is an Event. A concert with three showings is **one** experience row
  and **three** event instances.
- **Venue** is where an experience happens; **Place** is any geographic location.
- **Collection** → **Itinerary** → **Journey** are increasing scopes of planning.
- Actors: **Explorer**, **Publisher**, **Contributor**, **Partner**.

## Architecture

A **modular monolith**: one deployable, with domain modules that own their own
Postgres schema and never read each other's tables. The service boundaries the
specs draw are preserved as module boundaries, so splitting into separate services
later is a packaging change rather than a redesign.

```
Explorer web (React)
        │
        ▼
   FastAPI app ──────────── AI Gateway ──── model provider
        │                        │              (Gemini, or the offline
        │                        │               deterministic provider)
        │                        └──── tool registry ──┐
        │                                              │
        ├── identity ── publisher ── catalog ── explorer ── ai   (Postgres schemas)
        │
        ├── Meilisearch   (candidate retrieval only — ranking stays in the platform)
        └── Redis         (cache, sessions)
```

Two rules matter most:

1. **Ranking is owned by the platform, never the vendor.** Meilisearch returns
   candidates; `app/domains/discovery/ranking.py` decides what the explorer sees
   and why.
2. **Tools run before generation.** The AI Gateway retrieves verified platform
   data first and asks the model only to phrase it, so a provider outage degrades
   fluency rather than correctness.

## Stack

Python 3.11+ / FastAPI / SQLAlchemy 2 · PostgreSQL 16 + PostGIS + pgvector ·
Redis · Meilisearch · React 19 + TypeScript + Vite + Tailwind + React Query ·
Gemini behind an internal gateway.

---

## Running it

**Prerequisites:** Docker, Python 3.11+, Node 20+.

```bash
# 1. Infrastructure (Postgres+PostGIS+pgvector, Redis, Meilisearch)
cd infrastructure
docker compose up -d --build

# 2. Configuration
cd ..
cp .env.example .env

# 3. Backend
cd backend
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"     # Windows
# source .venv/bin/activate && pip install -e ".[dev]"  # macOS / Linux

.venv/Scripts/python.exe -m alembic upgrade head
.venv/Scripts/python.exe -m app.seed                    # Addis Ababa + search index
.venv/Scripts/python.exe run.py --reload                # http://127.0.0.1:8000

# 4. Web client
cd ../frontend/web
npm install
npm run dev                                             # http://localhost:5173
```

API documentation: <http://127.0.0.1:8000/docs>

> **Windows note:** `run.py` exists because psycopg's async driver cannot use the
> default `ProactorEventLoop`. It starts uvicorn on a selector loop. On Linux, run
> `uvicorn app.main:app` directly.

> **Vite note:** the dev server binds IPv6 only, so use `localhost:5173` rather
> than `127.0.0.1:5173`.

### AI provider

The concierge runs **without an API key by default**. `MADO_AI_PROVIDER=stub` uses
a deterministic local provider that composes replies from the tool results the
platform already retrieved — every fact it states is real, it is simply less
fluent than a model. To use Gemini:

```bash
MADO_AI_PROVIDER=gemini
MADO_GEMINI_API_KEY=your-key
```

### Tests

```bash
cd backend
.venv/Scripts/python.exe -m pytest tests/ -q
```

92 tests: ranking and explanation logic, intent classification and temporal
resolution, spam screening, and API contract tests covering the envelope,
anonymous access, publishing ownership and lifecycle, and moderation. They run
against the live stack; the API tests skip
themselves automatically when the server is not running.

```bash
cd frontend/web
npm run build          # typecheck + production build
```

---

## Repository layout

```
backend/
  app/
    core/            config, database, errors, envelopes, security, logging
    api/routes/      auth · me · catalog · discovery · concierge · publishing · trust
    domains/
      identity/      users, auth identities, sessions
      publisher/     personal + organization publishers, publishing service
      catalog/       cities, venues, experiences, events, media
      explorer/      saved items, reviews, reports, interaction signals
      discovery/     ranking, feed modules, search orchestration, indexer
      ai/            gateway, intents, tools, prompts
      trust/         screening, reporting, moderation queue
    integrations/    search (Meilisearch), ai_provider (Gemini / offline)
    seed/            Addis Ababa pilot data
  alembic/           migrations
  tests/
frontend/web/
  src/
    app/             shell, routing, store, hooks
    design-system/   tokens and primitives
    features/        discover · search · experiences · saved · concierge · auth
                     publishing (compose, your posts) · trust (report)
    lib/             api client, types, formatting
infrastructure/      docker compose, Postgres image with PostGIS + pgvector
docs/adr/            architecture decision records
```

## Decisions worth knowing

- **`venues.geo` is a generated column.** PostGIS geography derived from
  latitude/longitude by Postgres, so the point and the coordinates cannot drift
  apart, and `Computed` keeps the ORM from trying to write it.
- **Handlers commit explicitly.** FastAPI runs `yield`-dependency teardown *after
  the response is sent*, so committing there let clients act on data that was not
  yet durable. See `app/core/database.py`.
- **Meilisearch typo thresholds are lowered to 3/7.** Only the final query term
  gets prefix matching, and transliterated Amharic names are often short.
- **`eager_defaults` is on for every model.** `updated_at` is server-generated by
  `onupdate=func.now()`, so without it the ORM leaves the column expired after a
  flush and the next read emits a lazy `SELECT` — which raises `MissingGreenlet`
  under an async session, at whatever unrelated line happened to touch it first.
- **Discovery has two independent gates.** `published_experiences()` requires both
  that the author published it *and* that moderation has not withheld it, so a
  flagged post disappears from the feed, search, nearby and concierge answers at
  once. There is no code path that can forget to check.
- **react-router-dom is pinned to 7.18.2** despite an open advisory — see
  [ADR-0001](docs/adr/0001-react-router-version.md).
- **Search is Meilisearch, not OpenSearch.** Specs 80.01/80.04 say OpenSearch;
  spec 82.01, which declares itself the source of truth for external services,
  says Meilisearch with OpenSearch as the alternative. 82.01 wins.
