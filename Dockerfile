# The engine's container image (phase 7 row 7.21 and
# docs/decisions/2026-09-11-linux-container-product-host.md).
#
# One container, one data volume, supercronic reading the crontab that
# `engine schedule` renders from the tenant file. API keys only: the [claude]
# extra is NOT installed here, which is row 7.26's exit criterion (no Claude
# Code, no Max seat, nothing that needs a seat to log into).
#
# Build:  docker build -t engine .
# Run:    docker compose up -d        (see docs/install.md)

FROM python:3.12-slim-bookworm

# uv, pinned to the version CI installs (astral-sh/setup-uv in ci.yml). Copied
# from its own published image rather than curled: the tag is the pin and the
# registry does the verifying.
COPY --from=ghcr.io/astral-sh/uv:0.9.27 /uv /usr/local/bin/uv

# git: the ledger is a git repository and the 23:00 job pushes it.
# curl: the dead-man pings (scripts/lib/hc-ping.sh) and the download below.
# tzdata: supercronic resolves the tenant's timezone by name (CRON_TZ).
# ca-certificates: every https call in the image.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git tzdata \
 && rm -rf /var/lib/apt/lists/*

# supercronic: the one binary this image adds that the engine did not already
# need. Pinned by version AND by sha256 per architecture, verified here, so a
# retagged or replaced release fails the build instead of scheduling the
# business's month-end. Checksums computed from the published artifacts on
# 2026-09-16; aptible publishes no checksums file of its own.
ARG TARGETARCH
ARG SUPERCRONIC_VERSION=v0.2.49
ARG SUPERCRONIC_SHA256_AMD64=a53ae236602c7338aba3fbaff40bda6300eae3b9fedb8261eb06cfe3724430c1
ARG SUPERCRONIC_SHA256_ARM64=02aa0cb229ba09050cba6638059dadb9eedc2276632ea43d6a57a2f8c1629dd5
RUN set -eu; \
    case "${TARGETARCH:-amd64}" in \
      amd64) arch=amd64; sha="${SUPERCRONIC_SHA256_AMD64}" ;; \
      arm64) arch=arm64; sha="${SUPERCRONIC_SHA256_ARM64}" ;; \
      *) echo "no pinned supercronic for architecture ${TARGETARCH}" >&2; exit 2 ;; \
    esac; \
    url="https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}"; \
    curl -fsSL -o /usr/local/bin/supercronic "${url}/supercronic-linux-${arch}"; \
    echo "${sha}  /usr/local/bin/supercronic" | sha256sum -c -; \
    chmod +x /usr/local/bin/supercronic

# sops and age: the tenant's secrets (row 7.22). sops decrypts
# tenants/<slug>/tenant.secrets.enc.yaml into the environment at boot;
# age-keygen is here so the operator generates the box's identity inside the
# container and needs nothing installed on the host. Same rule as supercronic:
# pinned by version AND by sha256 per architecture, verified at build time.
# The sops checksums below match the project's own published
# sops-v3.13.3.checksums.txt; age publishes sigstore proofs rather than a
# checksums file, so its two values were computed from the release artifacts
# on 2026-09-16 and are stated in the PR.
ARG SOPS_VERSION=v3.13.3
ARG SOPS_SHA256_AMD64=e5bec3346a873ae91d871550f3e698c1aad962aff462a080e40f25fde17fef6b
ARG SOPS_SHA256_ARM64=53b0abacd38ef1b12a66d6c100956691b9cefce018d91f81e73ddf7438b94d77
RUN set -eu; \
    case "${TARGETARCH:-amd64}" in \
      amd64) arch=amd64; sha="${SOPS_SHA256_AMD64}" ;; \
      arm64) arch=arm64; sha="${SOPS_SHA256_ARM64}" ;; \
      *) echo "no pinned sops for architecture ${TARGETARCH}" >&2; exit 2 ;; \
    esac; \
    url="https://github.com/getsops/sops/releases/download/${SOPS_VERSION}"; \
    curl -fsSL -o /usr/local/bin/sops "${url}/sops-${SOPS_VERSION}.linux.${arch}"; \
    echo "${sha}  /usr/local/bin/sops" | sha256sum -c -; \
    chmod +x /usr/local/bin/sops

ARG AGE_VERSION=v1.3.2
ARG AGE_SHA256_AMD64=cbe24006683f8eb669266162894b9a522a1af52f2665fbc63a4bb032ed26ac10
ARG AGE_SHA256_ARM64=6b8dc4333c53a5a57c9e5834e3a48f92605d7154014cd07269ff3327db5d37f4
RUN set -eu; \
    case "${TARGETARCH:-amd64}" in \
      amd64) arch=amd64; sha="${AGE_SHA256_AMD64}" ;; \
      arm64) arch=arm64; sha="${AGE_SHA256_ARM64}" ;; \
      *) echo "no pinned age for architecture ${TARGETARCH}" >&2; exit 2 ;; \
    esac; \
    url="https://github.com/FiloSottile/age/releases/download/${AGE_VERSION}"; \
    curl -fsSL -o /tmp/age.tgz "${url}/age-${AGE_VERSION}-linux-${arch}.tar.gz"; \
    echo "${sha}  /tmp/age.tgz" | sha256sum -c -; \
    tar -xzf /tmp/age.tgz -C /tmp; \
    install -m 0755 /tmp/age/age /tmp/age/age-keygen /usr/local/bin/; \
    rm -rf /tmp/age /tmp/age.tgz

WORKDIR /app

# The dependency layer first, so editing engine code does not re-resolve 39
# packages. --no-dev: the test suite is CI's job, not the box's.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY . /app
RUN uv sync --locked --no-dev

# One data volume (docs/install.md): the tenant file, the ledger, the auditor
# store, the reports and the logs. /app/data is the same directory, reachable
# as a relative path from where the jobs run, because every path in a tenant
# file is relative to the directory the engine runs from and the scheduled
# scripts cd to the repo.
RUN rm -rf /app/data && ln -s /data /app/data
VOLUME ["/data"]

# UV_NO_SYNC: the environment was built above and is not re-resolved at 02:00
# on a box that may have no network. ENGINE_UV_EXTRA and ENGINE_UV_GROUP
# empty: this host installed no optional extras and no optional dependency
# groups (no SDK, no browser), so `uv run` is asked for neither
# (scripts/lib/uv-run.sh).
# ENGINE_IMAGE: there is no checkout here, and this is how the freshness guard
# is told the image digest is the review
# (docs/decisions/2026-09-16-an-image-is-a-reviewed-checkout.md). A release
# build overrides it with the real tag or digest.
#
# The state roots are set HERE, not only in the entrypoint: `docker compose
# exec engine uv run engine doctor <tenant>` does not go through the
# entrypoint, and an operator's own command must see the same volume the
# scheduled jobs do (found by running the thing, 2026-09-16).
#
# SOPS_AGE_KEY_FILE is the path the operator puts the box's age identity at
# (row 7.22). It is a PATH, never a key: nothing secret is in this file or in
# the image. It sits here for the same reason the state roots do, so an
# operator's own `docker compose exec engine uv run engine doctor <tenant>`
# looks in the same place the entrypoint does.
ENV UV_NO_SYNC=1 \
    ENGINE_UV_EXTRA= \
    ENGINE_UV_GROUP= \
    ENGINE_IMAGE=engine:dev \
    ENGINE_TENANT=demo \
    SOPS_AGE_KEY_FILE=/data/age/keys.txt \
    ENGINE_DATA_ROOT=/data \
    ENGINE_TENANTS_ROOT=/data/tenants \
    ENGINE_LEDGER_ROOT=/data/ledger \
    AUDITOR_STORE_ROOT=/data/auditor \
    AUDITOR_TENANTS_DIR=/data/tenants \
    LOG_DIR=/data/logs \
    PYTHONUNBUFFERED=1

ENTRYPOINT ["/app/host/entrypoint.sh"]
