# Contributing

## Setup

```bash
make install      # poetry install --all-extras --with examples
make check         # black --check, ruff, mypy --strict, pytest (95% coverage gate)
```

The integration suite additionally wants the `opa` binary
(`brew install opa` / see [OPA's install docs](https://www.openpolicyagent.org/docs/latest/#running-opa)).
Without it, tests that need a real policy engine skip themselves rather than
substitute a fake one — see the "Reuse existing verification, don't fake it"
principle below.

## Before opening a PR

- `make check` must pass locally — it's what CI runs, so this catches almost
  everything before the round trip.
- If you touched `examples/enterprise_scenario/`, run the scenario itself:
  `poetry run python -m examples.enterprise_scenario.scenario`.
- If you touched packaging (`pyproject.toml`'s `[tool.poetry] include`,
  `resources/`, the CLI, `Dockerfile`), run `make test-packaging` — it builds a
  wheel, installs it into a clean venv, and runs it from an unrelated working
  directory. This is the only check that catches a wheel silently missing a
  runtime file, which has happened before in this repo.

## How this codebase is organized

Hexagonal: `domain/` (pure models + `Protocol` ports, no I/O) →
`application/` (the transport-agnostic use case) → `adapters/` (one file per
external system) → `gateway/` (FastAPI, wiring only). See
[ADR-0006](docs/adr/0006-hexagonal-architecture-for-the-control-plane.md).
Swapping an adapter — a new IdP, a new policy engine — should mean adding one
file and one line in `gateway/dependencies.py`, not touching the gateway or
domain layer. If your change doesn't fit that shape, that's worth a second
look before merging.

Every non-trivial decision has an ADR in `docs/adr/`. If you're proposing one
(a new adapter, a new port, a change to the security model), write one —
they're short, and they're what stops the same debate from happening twice.

## Reuse existing verification, don't fake it

The strongest pattern in this codebase, worth following: when a component
needs to interoperate with something real (an IdP, an MCP server, a policy
engine), test it against **the real thing**, not a hand-written stand-in for
it. `tests/integration/test_keycloak_conformance.py` and
`test_github_mcp_conformance.py` exist because a mock written by the same
person who wrote the adapter tends to share the adapter's wrong assumptions —
that's exactly how two real bugs were found (Keycloak's service-account `sub`
claim, and the original MCP upstream adapter not speaking the real MCP
transport at all). If you're adding a new external integration, look for the
cheapest way to test it against a real instance before reaching for a mock.

## Commit / PR conventions

- Commits explain **why**, not what — the diff already shows what changed.
- No unrequested scope creep: a bug fix doesn't need a refactor riding along.
- Tests are not optional for new behavior. A PR that adds an adapter without a
  test against something real (see above) will get asked for one.

## Reporting a security issue

Don't open a public issue — see [SECURITY.md](SECURITY.md).
