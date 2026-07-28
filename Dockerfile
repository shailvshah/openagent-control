# Multi-stage. The builder produces a wheel; the runtime installs *that wheel*
# and nothing else — no source tree, no repo layout on disk.
#
# This is deliberate. Copying src/ into the image would let the container pass
# while the published package is broken: policies, migrations and the default
# registry all used to live outside the package and were absent from the wheel,
# so a `pip install` started, answered /healthz with 200, and failed every
# request. Building from the wheel means the image cannot succeed unless a pip
# install would too.
FROM python:3.11-slim AS builder

WORKDIR /build
RUN pip install --no-cache-dir poetry==2.3.2

COPY pyproject.toml poetry.lock README.md LICENSE ./
COPY src ./src
RUN poetry build -f wheel


FROM python:3.11-slim

# The persistence extra is installed so one image serves both modes; the code
# lazy-imports that stack, so leaving it unused costs image size, not memory.
COPY --from=builder /build/dist/*.whl /tmp/
# The extras suffix must attach to the glob's *expansion*, not the pattern
# itself: `/tmp/*.whl'[persistence]'` quotes the brackets so they stop being
# a glob character class, but that also makes them literal characters the
# filename itself would have to end with — which no wheel ever does, so the
# glob silently fails to match and pip reports "not a valid wheel filename".
RUN pip install --no-cache-dir "$(ls /tmp/*.whl)[persistence]" && rm /tmp/*.whl

# Run unprivileged: the gateway holds the token-exchange client secret and sits
# in the path to internal systems, so root in the container is not warranted.
RUN useradd --create-home --uid 10001 oac
USER oac
WORKDIR /home/oac

EXPOSE 8000

# Liveness only. Readiness (/readyz) checks OPA, the database schema and Redis;
# it belongs in the orchestrator's readiness probe, not here, or the container
# would be killed and restarted for a dependency a restart cannot fix.
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8000/healthz')"

ENTRYPOINT ["openagent-control"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]
