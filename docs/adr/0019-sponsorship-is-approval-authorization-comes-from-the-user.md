# ADR-0019: Sponsorship is approval; authorization comes from the user

## Status
Accepted

## Context
`AgentIdentity.human_sponsor` was doing two jobs at once, and only one of them
well. It recorded *who a call was on behalf of* — and it was also, implicitly,
the entire human side of the authorization story. Three consequences, each a
real defect:

**The sponsor could be asserted, not proved.** `OidcJwksIdentityProvider` ended
with `human_sponsor=human_sponsor or headers.get("x-human-sponsor")`. When the
agent's token was app-only, the gateway fell back to trusting a header — so an
autonomous agent could claim to act for any human by setting one. That is a
dev-stub property (ADR-0005) that had quietly landed in the production identity
path, and ADR-0010 never mentioned it.

**Nothing tied the subject token to the sponsor.** `_broker_credential` read
`X-Subject-Token` and handed it to the IdP without looking inside. The IdP
rejects a *forged* token, so that was never a forgery hole — but an agent
holding some other user's **valid** token could present it while claiming a
different sponsor. The IdP would mint a real credential for that user, and the
receipt would attribute the call to someone else.

**Policy could not see the user at all.** The OPA input carried `method`,
`spiffe_id`, registry facts and `params`. A rule could say "this agent may call
this tool"; it could not say "…and only if the acting human is entitled to it".
The intersection that delegation is *for* existed only at the IdP, as whatever
scopes it would mint.

Framing that fixes all three: **sponsorship is an approval — a record that a
human signed off on the agent acting. Authorization is a separate question,
answered by that user's own verified identity, roles and permissions.** An
approval is not an entitlement.

## Spec grounding, checked rather than assumed
Reading the specs before building changed the design three times:

- **`sub` alone is not an identifier.** OIDC Core §5.7: the only guaranteed
  unique identifier for an end-user is the `iss`/`sub` pair, because `sub` is
  only locally unique within an issuer. The existing code used a bare `sub` for
  `human_sponsor` while already using an issuer-scoped `spiffe_id` for the
  workload — human identity held to a weaker standard than machine identity.
  Both are issuer-scoped now.
- **Pairwise subject identifiers would have broken the obvious fix.** The
  planned binding was "require `subject.sub == human_sponsor`". With pairwise
  subjects the value is derived per client, so the *same human* legitimately
  has a different `sub` in the agent's token than in the subject token. Strict
  equality would have rejected valid delegated calls on any tenant configured
  that way. Found before shipping, not after.
- **RFC 8693 already has the right primitive.** `may_act` (§4.4) is the
  standard way a subject token names the party authorized to act for that user
  — a stronger and more portable binding than comparing identifiers. RFC 8693
  also confirms `subject_token` is a *token-endpoint request parameter* with no
  defined header form, so `X-Subject-Token` is this project's own convention
  and is documented as such rather than implied to be interoperable.
- `preferred_username`/`email` MUST NOT be used as identifiers (§5.7) — kept,
  but display-only. Roles/groups are not standard OIDC claims, so the claim
  name stays configurable, as `OidcOperatorAuth` already established.

The model this lands on is RFC 8693 **delegation**, not impersonation: "A still
has its own identity separate from B… any actions taken are being taken by A
representing B."

## Decision

### `SubjectIdentity`, verified — the authorization principal
`OidcSubjectVerifier` validates the subject token exactly as the workload's
token is validated (JWKS, signature, `iss`, `aud`, `exp`) and projects it to
`subject_id` (`{issuer}#{sub}`), `username` (display only), `roles`, `scopes`,
and `authorized_actor` (`may_act.sub`).

Validating `aud` against the *resource* audience is also what rejects an ID
token being passed as a subject token — an ID token's `aud` is the client id.
No special case is needed, and there is a test asserting it so the property
stays deliberate rather than incidental.

Off by default (`OAC_SUBJECT_VERIFICATION_MODE=off`): enabling it changes what
a delegated call requires, which is a deployment decision.

### Binding: `may_act` first, issuer-scoped equality as fallback
`OAC_SUBJECT_BINDING=strict` (default) honours `may_act` when the IdP issues
it, and otherwise requires the subject's issuer-scoped id to equal the claimed
sponsor. `may-act-only` enforces `may_act` alone — **the correct setting for a
pairwise-subject tenant**, and the mismatch error names it, because an operator
hitting this legitimately needs to be told what to do, not just refused.
`off` verifies the token but checks no relationship to the caller.

### The user reaches policy, curated
OPA input gains `subject: {id, roles, scopes}`, or `null` for an autonomous
call. Curated rather than the raw claim set on purpose: policy input ends up in
OPA decision logs, and a user's whole token does not belong there.

`may_act` is deliberately **not** exposed — it is an input to the gateway's own
binding check, not a user entitlement, and putting it in policy input would
invite rules that re-decide binding inconsistently.

### The unverified sponsor header is gone from the production path
`OidcJwksIdentityProvider` no longer falls back to `X-Human-Sponsor`.
`HeaderIdentityProvider` still honours it, where the entire identity is already
a documented stub.

## Consequences
- The intersection is now expressible and enforced *at the gateway*: agent
  grant AND user entitlement. Verified against a real `opa` process
  (`tests/integration/test_subject_authorization.py`) in both directions — an
  entitled user is allowed a tool the agent holds, and an unentitled user is
  refused the same tool.
- **The absent-subject trap is tested, because it is genuinely dangerous.** In
  Rego, `not "x" in input.subject.roles` against a null subject is *undefined*,
  not true — so an entitlement rule written the obvious way silently fails to
  fire and an autonomous agent sails past a check meant to constrain it. The
  shipped policy's commented example spells out the null case, and a test
  asserts the deny actually happens.
- Entitlement rules are shipped commented-out. Enabling them by default would
  change the decision for every existing deployment; the registry grant remains
  the allowlist and guardrails narrow it (ADR-0016).
- **Breaking change**: with `identity_mode=oidc-jwks`, `human_sponsor` is now
  `{issuer}#{sub}` rather than a bare `sub`. Anything matching on the old
  value — a Rego rule, a receipt query — needs updating. Correctness was worth
  it: the old value could collide across federated issuers.
- **Not addressed**: the receipt still records `spiffe_id` and not the acting
  user, so "which human was this done for" is not answerable from the ledger
  alone. That is the natural next step, and it needs care — adding a field
  changes the signed payload shape, so old receipts must stay verifiable
  (the `enforced` column in migration 0002 is the precedent to follow).
- Verification costs one JWKS-cached signature check per delegated call, in a
  worker thread so a JWKS cache miss cannot stall the event loop — the same
  treatment `OidcJwksIdentityProvider` already applies.
