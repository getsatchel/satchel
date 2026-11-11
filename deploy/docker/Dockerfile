# syntax=docker/dockerfile:1.7
# satchel: one image for every role (web, worker, db migrate/bootstrap, workspace create).
# Build context is the repo root:  docker build -f deploy/docker/Dockerfile -t satchel .
#   --build-arg BAKE_EMBEDDINGS=false   skip the baked fastembed model (smaller image, the
#                                        model is downloaded on first use instead)
#   --build-arg INSTALL_DBT=false       leave out dbt (dbt sources can then only read a
#                                        committed or uploaded manifest.json; smaller image)
# Base images are pinned by digest; bump them deliberately (Dependabot / `docker buildx
# imagetools inspect <tag>`).

ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337
ARG NODE_IMAGE=node:22-bookworm-slim@sha256:2f0e17f8955e766e776ae16d3ac47519ca3edf47c07e5c1423527926fdd5d568
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.5@sha256:e85be844203885286c60ffad8a858d48afb6c5a5c237ca0e67f12e74b8f174b1

FROM ${UV_IMAGE} AS uv_bin

# ---- curator UI -----------------------------------------------------------------------------
FROM ${NODE_IMAGE} AS ui
WORKDIR /src/ui
COPY ui/package.json ui/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci --no-audit --no-fund
COPY ui/ ./
RUN npm run build

# ---- third-party dependencies (locked) ------------------------------------------------------
# Only the lock files are copied, so this layer (the slow one) is reused until a dependency
# changes; editing satchel's own code rebuilds just the wheels below. Hashes are verified.
FROM ${PYTHON_IMAGE} AS deps
COPY --from=uv_bin /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never
WORKDIR /src/server
COPY server/pyproject.toml server/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv export --frozen --no-dev --no-emit-project --no-emit-package satchel-adapter-sdk \
        --extra gcp --extra aws --extra bigquery --extra snowflake --no-header \
        -o /tmp/requirements.txt \
 && uv venv /opt/venv \
 && uv pip install --python /opt/venv/bin/python --require-hashes --no-deps \
        -r /tmp/requirements.txt

# ---- dbt for `dbt parse` (optional) --------------------------------------------------------
# dbt sources from a repository can build manifest.json on the server (satchel.sources.
# dbt_build). dbt lives in its own virtualenv: its strict pins never meet the server's, and
# satchel runs /opt/dbt/bin/dbt as a separate process. The experimental Rust parser binary
# (~130 MB) is opt-in and unused, so it is removed. INSTALL_DBT=false leaves /opt/dbt empty.
FROM ${PYTHON_IMAGE} AS dbt
ARG INSTALL_DBT=true
COPY --from=uv_bin /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never
COPY deploy/docker/dbt-requirements.txt /tmp/dbt-requirements.txt
RUN --mount=type=cache,target=/root/.cache/uv \
    mkdir -p /opt/dbt \
 && if [ "$INSTALL_DBT" = "true" ]; then \
        uv venv /opt/dbt \
     && uv pip install --python /opt/dbt/bin/python --require-hashes --no-deps \
            -r /tmp/dbt-requirements.txt \
     && rm -f /opt/dbt/bin/dbt-core-experimental-parser \
     && /opt/dbt/bin/dbt --version; \
    fi

# ---- project wheels -------------------------------------------------------------------------
FROM deps AS build
WORKDIR /src
# Layout the server build expects: hatch bundles ../ui/dist and the Jaffle example.
COPY sdk/ sdk/
COPY examples/jaffle-shop/ examples/jaffle-shop/
COPY server/ server/
COPY --from=ui /src/ui/dist ui/dist
RUN uv build --wheel --out-dir /wheels sdk \
 && uv build --wheel --out-dir /wheels server \
 && uv pip install --python /opt/venv/bin/python --no-deps /wheels/*.whl

# ---- embedding model (optional) -------------------------------------------------------------
FROM build AS model
ARG BAKE_EMBEDDINGS=true
ENV SATCHEL_FASTEMBED_CACHE_DIR=/opt/satchel/fastembed
RUN mkdir -p /opt/satchel/fastembed \
 && if [ "$BAKE_EMBEDDINGS" = "true" ]; then \
        /opt/venv/bin/python -c "from satchel.settings import Settings; from fastembed import TextEmbedding; s = Settings(); TextEmbedding(model_name=s.fastembed_model, cache_dir=s.fastembed_cache_dir)"; \
    fi

# ---- runtime --------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime
ARG BAKE_EMBEDDINGS=true
LABEL org.opencontainers.image.title="satchel" \
      org.opencontainers.image.source="https://github.com/getsatchel/satchel" \
      org.opencontainers.image.licenses="Apache-2.0"
# git: sources can read Cube / SQLMesh / dbt files from a repository (satchel.sources.git).
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN groupadd --gid 10001 satchel \
 && useradd --uid 10001 --gid 10001 --home-dir /home/satchel --create-home --shell /usr/sbin/nologin satchel \
 && mkdir -p /var/lib/satchel/results \
 && chown -R 10001:10001 /var/lib/satchel
COPY --from=build /opt/venv /opt/venv
# Not on PATH on purpose: satchel finds it at /opt/dbt/bin/dbt (SATCHEL_DBT_BIN overrides).
COPY --from=dbt /opt/dbt /opt/dbt
COPY --from=model --chown=10001:10001 /opt/satchel /opt/satchel
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080 \
    SATCHEL_FASTEMBED_CACHE_DIR=/opt/satchel/fastembed \
    SATCHEL_RESULT_DIR=/var/lib/satchel/results \
    HF_HUB_DISABLE_TELEMETRY=1
USER 10001:10001
WORKDIR /home/satchel
EXPOSE 8080
ENTRYPOINT ["satchel"]
CMD ["serve", "api", "--host", "0.0.0.0"]
