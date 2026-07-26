# Keycloak conformance — validating against a real, third-party IdP

The scenario's own [authorization server](../authorization_server.py) is code in
this repo. That makes it fast and dependency-free, but it also means a bug in it
could be mirrored by a matching bug in the adapters that call it, and both would
still pass their tests. **Keycloak cannot share our bugs.**

This is the check that justifies claiming the OIDC and RFC 8693 adapters
actually interoperate with enterprise identity infrastructure — and it earned
its keep immediately: it caught a real defect on first run (see below).

Keycloak is the open-source IdP closest in role to PingOne, Okta, or Entra ID.
Standard token exchange (RFC 8693) has been **officially supported since
Keycloak 26.2** — no preview feature flag required.

## Run it

```bash
# 1. Get Keycloak 26.2+ (needs Java 17+; brew install openjdk@17)
curl -sLO https://github.com/keycloak/keycloak/releases/download/26.4.2/keycloak-26.4.2.tar.gz
tar xzf keycloak-26.4.2.tar.gz

# 2. Start it
JAVA_HOME=$(brew --prefix openjdk@17) \
KC_BOOTSTRAP_ADMIN_USERNAME=admin KC_BOOTSTRAP_ADMIN_PASSWORD=admin \
  keycloak-26.4.2/bin/kc.sh start-dev --http-port=8380

# 3. Provision the realm
KC_HOME=$PWD/keycloak-26.4.2 ./examples/enterprise_scenario/keycloak/provision.sh

# 4. Run the conformance tests
OAC_TEST_KEYCLOAK_URL=http://localhost:8380/realms/oac \
  poetry run pytest tests/integration/test_keycloak_conformance.py -v
```

They skip silently without `OAC_TEST_KEYCLOAK_URL`, so the default `make check`
stays fast and dependency-free.

## What the realm encodes

`provision.sh` is not boilerplate — each step is a decision a platform team
makes for real:

| Client | Role | Why it matters |
|---|---|---|
| `finance-invoice-svc` | The agent workload | Gets tokens audienced **to the gateway** via an audience mapper |
| `openagent-control-gateway` | The control plane | The **only** client with `standard.token.exchange.enabled` |
| `finance-mcp-api` | The downstream API | Its client id *is* the audience the MCP server validates |

The asymmetry is the whole security model: the agent's token is audienced to the
gateway, so it is useless at the downstream API; only the gateway may exchange
it for one that isn't. An agent permitted to exchange tokens could mint its own
downstream credentials, which is precisely what the control plane exists to
prevent — `provision.sh` grants that permission to exactly one client.

## The defect this caught

Keycloak issues client-credentials tokens with `sub` set to the **service
account's UUID**, distinct from the client id:

```json
{ "azp": "finance-invoice-svc",
  "sub": "1de70397-df2a-4b59-9679-fd51438bf04e",
  "preferred_username": "service-account-finance-invoice-svc" }
```

The identity adapter originally inferred "a `sub` different from the client id
means a human is being acted for". Against Okta that heuristic holds; against
Keycloak it does not. The gateway therefore classified an autonomous machine
call as *delegated*, and rejected it with `401 — requires an X-Subject-Token
header`. Every agent would have been broken, and no in-repo test could have
found it, because our own authorization server shared the same assumption.

The fix ([`oidc_jwks.py`](../../../src/openagent_control/adapters/identity/oidc_jwks.py))
checks the documented app-only markers for each provider — Okta's `sub == client
id`, Entra's `idtyp: app`, and Keycloak's `service-account-` username prefix —
rather than a single cross-vendor guess.

## Known limits

- The realm is provisioned by script rather than a committed realm export, so it
  is readable as a sequence of decisions rather than a 900-line JSON blob.
- Tokens here are obtained via `client_credentials`. The **delegated** path (a
  real human's token exchanged on their behalf) is exercised against the
  in-repo authorization server, not Keycloak, because it needs an interactive
  login to produce a genuine user token.
- Not wired into CI. Running Keycloak on every `make check` would add a JVM and
  ~30s of startup to a suite that currently takes 3 seconds.
