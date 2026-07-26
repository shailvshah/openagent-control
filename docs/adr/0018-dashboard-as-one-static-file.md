# ADR-0018: The dashboard is one static file, not a SPA build

## Status
Accepted

## Context
ADR-0014 built the control-plane API and left the dashboard for later,
sketching it as "a Vite/React app served as static assets from the same
service." Meanwhile `cli.py`'s help text, `deployment.md`, and a route
docstring all described the running service as "the control-plane API +
dashboard" — a thing an operator could type a command to get and would not
receive. That was corrected first, before any of this was built, because a
promise in the CLI is worse than a gap in the roadmap.

Revisiting the SPA plan, the React shape did not survive contact with what this
service actually is:

- **It is meant to run inside the customer's trust boundary**, and may have no
  egress at all. Anything loaded from a CDN makes the page blank in exactly the
  environments this product is built for.
- **It is a security product**, so a node toolchain and a lockfile of
  transitive dependencies would be entering the release path of a service that
  holds audit evidence, to render four numbers and two tables.
- **There is not enough UI here to earn a framework.** Fleet counts, an agent
  table with suspend/activate, a receipt list, and a chain-integrity check.

## Decision
One `index.html`, packaged in `resources/dashboard/` next to the Rego policies
and served by the control plane at `/`. No build step, no CDN, no framework —
vanilla JS, inline CSS, light and dark via `prefers-color-scheme`.

**The page has no privileged back channel.** It calls the same `/api/v1`
endpoints an operator can `curl`; anything it can do, the API already allows.
That is what keeps ADR-0014's security boundary intact — the dashboard adds a
renderer, not a capability.

**The HTML itself is unauthenticated, deliberately.** It contains no data. Every
figure is fetched by the browser from `/api/v1`, which does require an operator
credential, so an unauthenticated visitor gets an empty sign-in box. Gating the
static file would imply a protection it does not provide.

**Credential handling is honest about what it is.** The operator pastes their
credential (the API key, or an OIDC access token) and it is held in that tab's
`sessionStorage`, sent as a bearer token. This is *not* the browser-appropriate
OIDC login ADR-0014 anticipated — no authorization-code redirect, no session
cookie, no refresh. It is the smallest thing that works against the operator
identity that already exists, and the page says so on its face rather than in a
footnote. A 401 signs the operator out; any other error is surfaced as an
error, because signing someone out on a 500 would hide an outage behind a login
box.

### `GET /api/v1/fleet/activity`
The existing `/fleet/summary` is a landing-page count. The dashboard also needs
"who is busiest" and "why are calls being denied", so this aggregates receipts
over a window, grouped by agent and by denial reason.

**Not grouped by tool**, which is the obvious thing to want and the one thing
the ledger cannot answer: receipts carry a payload hash rather than the payload
(ADR-0003), so *that* a call happened is provable while *what it was* is not
readable. Adding a tool-name column to receipts to power a chart would trade a
privacy property for a dashboard feature; the endpoint reports what it can and
this ADR records why it stops there.

`truncated` is part of the response, not a footnote. A count silently capped by
a scan limit is worse than one labelled a lower bound — an operator reads "3
denials" and believes it.

## Consequences
- `openagent-control serve-control-plane` now genuinely serves a dashboard, so
  the wording removed at the start of this work is true when restored.
- Packaged under `resources/`, so `test_packaging.py` — which installs a built
  wheel into a clean venv and runs it from an unrelated directory — covers it
  automatically. That test exists because this project has shipped a wheel
  missing its runtime files before.
- The browser-appropriate OIDC login remains unbuilt and is now the only
  outstanding piece of ADR-0014's dashboard plan. `sessionStorage` means the
  credential is gone when the tab closes and never leaves it, which is
  acceptable for an internal operator tool and would not be for a
  customer-facing one.
- No JS tests. The page is exercised by route tests that assert it is served,
  is self-contained (no external `src=`/`href=`), and needs no credential while
  the API does. A headless-browser suite would be more coverage than this
  amount of UI justifies; if the dashboard grows, that judgement should be
  revisited rather than inherited.
