# ADR-0001: Pin react-router-dom to 7.18.2 despite an open advisory

Status: Accepted
Date: 2026-08-05

## Context

`npm audit` reports high-severity advisories against every published version of
`react-router-dom`. The affected ranges overlap such that no version is clean:

| Version | Advisories that apply |
|---|---|
| 6.0.0 – 7.17.0 | 14 advisories, including unauthenticated RCE via vendored turbo-stream (CVSS 8.1), SSR XSS in `ScrollRestoration` (8.2), and **open redirect via backslash in `<Link>` / `useNavigate`** |
| 7.12.0 – 8.2.0 | 1 advisory: RSC Mode CSRF Bypass — action execution before a 400 response |

`npm audit fix` recommends downgrading to 7.11.0, which silences the newer
advisory but reintroduces all fourteen older ones. Following the tool's advice
would make the application materially less safe.

## Decision

Pin `react-router-dom` to exactly **7.18.2**.

## Rationale

The single advisory that applies to 7.18.2 concerns **React Server Components
mode**: it requires a server processing router actions. The Mado Explorer client
is a pure client-side SPA built by Vite — no SSR, no RSC, no server actions, no
server-side loaders. The vulnerable code path is not reachable in this
configuration.

The advisories affecting ≤ 7.17.0 are not all server-side. The open-redirect
issues in `<Link>` and `useNavigate` apply directly to client-side routing and
would be genuinely exploitable here.

Choosing 7.18.2 therefore trades a non-reachable finding for the avoidance of
several reachable ones.

## Consequences

- `npm audit --audit-level=high` will report one finding. This is expected. CI
  should not gate on a clean audit for this package alone; it should compare
  against a reviewed allowlist entry for this advisory.
- **Revisit if the architecture changes.** If Mado ever adds SSR or React Server
  Components for the SEO-indexable city and experience pages described in spec
  BUSINESS-90.02 s16, this decision is void and the advisory becomes live. That
  change must re-open this ADR.
- Re-evaluate when a version outside both ranges ships (> 8.2.0).
