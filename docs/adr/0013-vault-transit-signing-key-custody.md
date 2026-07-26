# ADR-0013: KMS-backed receipt signing via HashiCorp Vault Transit

## Status
Accepted

## Context
Every prior assessment of this project (roadmap.md, ADR-0003, ADR-0009) names
the same open item: `ReceiptSigner`'s Ed25519 key is generated in-process by
default. That is the single biggest gap between "the receipt is
cryptographically signed" and "the receipt is compliance-grade evidence" —
non-repudiation is only as strong as the custody of the key doing the
signing, and an in-process key an operator can read out of memory, or that
regenerates silently on every restart (breaking the ability to verify old
receipts against the *new* running process's public key), does not clear that
bar.

**Ed25519 rules out the two most obvious KMS choices.** AWS KMS's asymmetric
signing API supports RSA and ECC (NIST P-256/P-384/P-521, secp256k1) — no
EdDSA. Azure Key Vault's signing API is the same shape: RSA and the NIST
curves, no Ed25519. Adopting either would mean reopening ADR-0003's algorithm
choice project-wide (chain hashing, receipt verification, every test that
signs and independently verifies a receipt), not just swapping where the key
lives.

**HashiCorp Vault's Transit secrets engine signs Ed25519 natively** — verified
directly, not assumed: a local Vault dev server was used to create a
`type=ed25519` transit key, sign real bytes through Vault's HTTP API, and
independently verify the returned signature with the `cryptography` library
against Vault's returned public key, before any adapter code was written.

Vault is also open-source and self-hostable, consistent with this project's
self-hosted posture (the enterprise-scenario and Keycloak conformance work
took the same stance), and it has first-class OIDC/JWT auth methods, so it
composes with the same enterprise IdP federation (Okta/Entra/Keycloak) this
project already targets for workload identity — an operator's Vault access
policy can itself be gated by the same IdP.

## Decision
Introduce a `Signer` Protocol (`adapters/ledger/signing.py`) narrower than
"holds a private key": `sign(bytes) -> bytes` and `public_key() ->
Ed25519PublicKey`. `ReceiptSigner` (the existing in-process implementation)
and the new `VaultTransitSigner` both satisfy it structurally, so
`Ed25519ChainLedger` and `PostgresLedger` don't know or care which one they
were given — this is ADR-0006's port pattern applied one level below the
`Ledger` port itself, at the piece of a ledger adapter's construction that
actually varies.

`VaultTransitSigner` (`adapters/ledger/vault_signer.py`) talks to Vault's
plain HTTP API via `httpx` (already a core dependency) rather than pulling in
the `hvac` SDK for the two endpoints actually needed
(`GET /v1/transit/keys/{name}`, `POST /v1/transit/sign/{name}`). The private
key material never enters this process: `sign()` posts the payload and Vault
returns a signature; there is no code path in this adapter capable of
exporting the key.

`Settings.signing_key_mode: Literal["in-process", "vault-transit"]`, wired at
container-build time (`_signer(settings)` in `gateway/dependencies.py`) and
orthogonal to `database_url` — you can run Vault-backed signing with the file
registry and in-memory ledger, or in-process signing with Postgres. Fetches
the public key from Vault at construction, same startup-fail-fast posture as
`OidcJwksIdentityProvider` (ADR-0010): an unreachable Vault or a missing
transit key is a startup failure, not a per-request one — and it's what
`openagent-control doctor` / `GET /readyz` check before declaring the gateway
ready.

## Consequences
- Signature format and chain-hashing are unchanged — `Signer.sign()` returns
  the same 64-byte Ed25519 signature either way, so `Ed25519ChainLedger`,
  `PostgresLedger`, and every existing receipt-verification test needed zero
  changes beyond the constructor signature.
- Vault availability becomes a hard startup dependency when
  `signing_key_mode="vault-transit"`: if Vault is down, the gateway does not
  start (fail-fast), rather than serving requests it can't sign receipts for.
- Key rotation, sealing/unsealing, and Vault's own access-policy configuration
  are Vault's responsibility, not this project's — `VaultTransitSigner`
  always reads the *latest* key version (`max(int(v) for v in keys)`), so a
  `vault write -f transit/keys/{name}/rotate` takes effect on the next
  gateway restart without an application-level migration.
- `openagent-control doctor` and `GET /readyz` report a public-key fingerprint
  when Vault-backed, so an operator can confirm — without any Vault CLI access
  of their own — which key is actually signing.
- Not addressed by this ADR: **Vault's own high-availability, unsealing, and
  backup story** is out of scope here; this project treats Vault as an
  external dependency to be operated correctly, the same way it treats
  Postgres. A single `vault server -dev` instance (as used to verify this
  adapter) is not production Vault.
- AWS KMS / Azure Key Vault adapters remain plausible future work behind the
  same `Signer` Protocol, but only by changing the receipt signature algorithm
  to ECDSA for that code path — a larger, cross-cutting change this ADR
  explicitly does not make.
