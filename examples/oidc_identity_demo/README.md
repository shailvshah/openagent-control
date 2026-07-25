# OIDC identity demo — Okta / Entra ID token validation

Proves `OidcJwksIdentityProvider` ([ADR-0010](../../docs/adr/0010-oidc-jwks-identity-for-okta-and-entra.md))
against real, cryptographically signed access tokens shaped like what Okta and
Microsoft Entra ID actually issue — fully offline, no real tenant needed.

## What it shows

1. **A registered agent authenticates with an Entra-style delegated token**
   (`azp` claim identifies the calling service) → identity resolves to
   `oidc://{issuer}/finance-invoice-svc`, which is in the Agent Registry with
   `read_query` granted → **ALLOWED**, forwarded, receipted.
2. **An unregistered client presents an Okta-style client-credentials token**
   (`cid` claim) → identity resolves to a client ID with no registry record →
   **DENIED as an orphan** (ADR-0008's zero-orphaned-agents guarantee) — and
   the denial is still receipted, chained to the previous decision's hash.
3. **A validly signed token for the wrong audience** → rejected at the
   identity layer itself (HTTP 401), before policy ever runs — the
   confused-deputy check the `enterprise-idp-integration` skill calls out as
   the #1 real-world integration bug.

## Run it

```bash
poetry run python -m examples.oidc_identity_demo.demo
```

No external services required — `mock_idp.py` starts a local HTTP server that
serves an OIDC discovery document and JWKS and mints real RS256 tokens against
its own generated key pair.

## Files

- `mock_idp.py` — the offline stand-in for Okta/Entra: discovery + JWKS
  endpoints, `mint_token()` for building test tokens with arbitrary claims
- `demo.py` — wires `OidcJwksIdentityProvider` into the gateway and runs the
  three scenarios above

## Pointing this at a real tenant

Swap `mock_idp.discovery_url` for a real one and this is production config,
not demo code:

```bash
export OAC_IDENTITY_MODE=oidc-jwks
export OAC_OIDC_DISCOVERY_URL="https://{yourOktaDomain}/oauth2/default/.well-known/oauth-authorization-server"
# or: https://login.microsoftonline.com/{tenant}/v2.0/.well-known/openid-configuration
export OAC_OIDC_AUDIENCE="<your registered application's client ID / App ID URI>"
```

See the `enterprise-idp-integration` skill for exact endpoint shapes per
provider, and ADR-0010 for why workload identity comes from the client/app-id
claim (`azp`/`appid`/`cid`) rather than `sub`.
