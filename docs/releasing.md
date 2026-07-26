# Releasing

Releases publish to PyPI from GitHub Actions using **Trusted Publishing** — an
OIDC identity GitHub mints per run and PyPI verifies against a publisher it has
on file. No long-lived API token exists, so there is none to leak. That is the
same argument this project makes about brokered credentials, applied to itself.

## One-time setup

**1. Configure the trusted publisher on PyPI.** This cannot be automated; it is
the step that establishes trust. Go to
<https://pypi.org/manage/project/openagent-control/settings/publishing/> and add
a GitHub publisher with **exactly** these values:

| Field | Value |
|---|---|
| Owner | `shailvshah` |
| Repository name | `openagent-control` |
| Workflow name | `release.yml` |
| Environment name | `pypi` |

All four are matched exactly. A mismatch — including the environment name — is
rejected at publish time, which is the point: it is what stops a different repo
or workflow from publishing as this project.

**2. Revoke any API tokens.** Once trusted publishing works, an API token is
only a liability. Delete them at
<https://pypi.org/manage/account/token/>.

The `pypi` GitHub environment is created automatically the first time the
workflow references it. Adding required reviewers to it (Settings →
Environments → pypi) makes every publish need a human approval — worth doing
for a package that sits in an authorization path.

## Cutting a release

```bash
# 1. Bump the version. The tag and pyproject must agree; CI enforces it.
poetry version patch          # or minor / major / 0.2.0

# 2. Verify locally exactly as CI will.
make check
make test-packaging

# 3. Commit, tag, push.
git commit -am "Release v$(poetry version --short)"
git tag -a "v$(poetry version --short)" -m "v$(poetry version --short)"
git push origin main --follow-tags
```

Pushing the tag triggers `.github/workflows/release.yml`, which:

1. runs the **entire CI workflow** again — lint, types, tests on 3.11 and 3.12
   against real Postgres/Redis/OPA, packaging conformance, and the end-to-end
   scenario. A tag is not evidence that main was green when it was cut;
2. checks the **tag matches the package version**, because a PyPI version
   number cannot be reused once burned, so a mismatch has to fail *before*
   the upload;
3. builds and publishes.

To rehearse without publishing, run the workflow manually from the Actions tab
with `dry_run` left checked — everything runs except the publish step.

## What CI enforces

| Check | Job | Fails on |
|---|---|---|
| `black --check`, `ruff`, `mypy --strict` | `quality` | any finding |
| Tests + coverage | `test` (3.11, 3.12) | coverage below **95%** (`--cov-fail-under=95`) |
| Wheel actually works | `packaging` | a wheel missing policies/migrations, or one that fails from a foreign working directory |
| Real end-to-end path | `scenario` | breakage in the gateway → OPA → token exchange → MCP chain |

The `test` job installs a real `opa` binary and asserts it is present before
running. The integration tests skip themselves when OPA is missing rather than
substituting a fake policy engine — correct locally, dangerous in CI, where a
green run over a silently-skipped suite is precisely the failure mode this
project keeps finding. `pytest -rs` prints every skip so the log shows what did
not run.

**Not run in CI**, because they need credentials or a long-lived service —
run them by hand before a significant release:

```bash
# Real third-party IdP (see examples/enterprise_scenario/keycloak/README.md)
OAC_TEST_KEYCLOAK_URL=http://localhost:8380/realms/oac poetry run pytest tests/integration/

# GitHub's production MCP server (read-only)
OAC_TEST_GITHUB_TOKEN=$(gh auth token) poetry run pytest tests/integration/
```

## Versioning

Semantic versioning. While the package is `Development Status :: 3 - Alpha`,
treat the `Settings` field names and the port `Protocol`s as the public API —
those are what an embedding integration depends on.
