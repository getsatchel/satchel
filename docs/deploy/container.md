# Running Satchel from the container image

One image, `ghcr.io/getsatchel/satchel`, runs every role; the command picks the role. Tags: for each `v*` release, the version (`0.1.1`), the minor line (`0.1`) and `latest`; for every push to `main`, `main` and `sha-<short>`. Pin a release version in production; `main` moves with every merge. It runs as uid/gid 10001 and needs no writable root filesystem when `SATCHEL_RESULT_DIR` points at `gs://`, `s3://` or a mounted volume. It includes the `gcp` and `aws` extras (google-cloud-storage, boto3).

## Build

```bash
docker build -f deploy/docker/Dockerfile -t satchel .                          # default: model baked in
docker build -f deploy/docker/Dockerfile --build-arg BAKE_EMBEDDINGS=false -t satchel:slim .
```

`BAKE_EMBEDDINGS=true` (default) downloads the fastembed model (`mixedbread-ai/mxbai-embed-large-v1`, about 1.3 GB) into `/opt/satchel/fastembed` so the container never needs the network to embed. Use `false` for a much smaller image if you use `hash` or `openai_compat` embeddings. The image installs the locked dependencies (hash-verified, from `server/uv.lock`) plus the `gcp` extra; base images are pinned by digest.

## Commands

| Role | Command | Notes |
|---|---|---|
| web | `satchel serve api --host 0.0.0.0` (the image default) | Port from `PORT` (default 8080); `--port` wins. |
| db roles | `satchel db bootstrap` | Run with `SATCHEL_BOOTSTRAP_DATABASE_URL` (an admin / cloudsqlsuperuser URL). Idempotent. |
| migrate | `satchel db upgrade` | Uses `SATCHEL_MIGRATION_DATABASE_URL` (owner). Run before web/worker roll out. |
| first workspace | `satchel workspace create <slug> --name <n> --admin-email <e>` | Idempotent; OIDC login with that email maps to admin. |
| worker | `satchel worker` (loop) or `satchel worker --once` | `--once` for a CronJob or Scheduler-triggered Cloud Run Job. SIGTERM ends the loop after the current pass. |
| audit export | `satchel audit export --since <iso> [--to gs://bucket/prefix \| s3://bucket/prefix \| file]` | JSONL. |

Example, local:

```bash
docker run --rm -e SATCHEL_DATABASE_URL=... ghcr.io/getsatchel/satchel:0.1.1 db upgrade
docker run --rm -e SATCHEL_DATABASE_URL=... ghcr.io/getsatchel/satchel:0.1.1 workspace create main --name Main --admin-email you@example.com
docker run -p 8080:8080 -e SATCHEL_DATABASE_URL=... -e SATCHEL_PUBLIC_URL=http://localhost:8080 ghcr.io/getsatchel/satchel:0.1.1
```

## Probes

| Probe | Path | Meaning |
|---|---|---|
| liveness | `GET /healthz` | Process is up; never touches the database. |
| readiness / startup | `GET /readyz` | 200 when the database answers (checked on every call, 3 s timeout) and the alembic revision is at head (cached about 30 s). Otherwise 503 `{"status": "unavailable", "reason": "..."}`. |

## Production mode

`SATCHEL_ENV=production` makes `satchel serve ...` and `satchel worker` refuse to start (exit 1, every problem listed) when:

- `SATCHEL_DEV_AUTH` is set;
- OIDC is not configured (`SATCHEL_OIDC_ISSUER`, `SATCHEL_OIDC_CLIENT_ID`, `SATCHEL_OIDC_CLIENT_SECRET`);
- `SATCHEL_PUBLIC_URL` is not `https://` (unless `SATCHEL_ALLOW_INSECURE_PUBLIC_URL=1`, for TLS terminated in a way Satchel cannot see).

Admin commands (`db upgrade`, `db bootstrap`, `workspace create`, `audit export`) are not guarded.

## Logging

`SATCHEL_LOG_FORMAT=json` writes one JSON object per line from the server, the access log and the worker: `severity`, `message`, `logger`, `time`, `httpRequest` (access log) and `logging.googleapis.com/trace` when the request carries `X-Cloud-Trace-Context` or `traceparent` (`projects/<id>/traces/<trace>` when `GOOGLE_CLOUD_PROJECT` is set, else the bare trace id).

## Environment

The environment table in the M8 deployment contract is the source of truth. Copied here:

| Var | Meaning |
|---|---|
| `SATCHEL_ENV` | `development` (default) or `production`. Production refuses to start if `SATCHEL_DEV_AUTH` is set, if OIDC is not configured, or if `SATCHEL_PUBLIC_URL` is not https (unless `SATCHEL_ALLOW_INSECURE_PUBLIC_URL=1`). |
| `SATCHEL_DATABASE_URL` | app role (`satchel_app`); RLS applies. |
| `SATCHEL_ADMIN_DATABASE_URL` | worker role (`satchel_worker`) used by server-internal jobs (`admin_session`). Falls back to `SATCHEL_DATABASE_URL` only in development. |
| `SATCHEL_MIGRATION_DATABASE_URL` | owner role, migrations only. |
| `SATCHEL_BOOTSTRAP_DATABASE_URL` | admin role, `satchel db bootstrap` only. |
| `SATCHEL_APP_DB_PASSWORD`, `SATCHEL_WORKER_DB_PASSWORD` | bootstrap only. |
| `SATCHEL_PUBLIC_URL` | external https URL (OAuth issuer; MCP resource is `<public_url>/mcp`). |
| `SATCHEL_OIDC_ISSUER`, `SATCHEL_OIDC_CLIENT_ID`, `SATCHEL_OIDC_CLIENT_SECRET` | SSO. |
| `SATCHEL_EMBEDDING_PROVIDER` (+ `SATCHEL_EMBEDDING_*`) | fastembed default. |
| `SATCHEL_CHAT_PROVIDER`, `SATCHEL_CHAT_MODEL`, `SATCHEL_CHAT_API_KEY` | curation LLM (optional). |
| `SATCHEL_RESULT_DIR` | local path, `gs://bucket/prefix` or `s3://bucket/prefix`. Multi-replica deployments MUST use an object store. The image defaults to `/var/lib/satchel/results`. |
| `AWS_REGION`, `AWS_ENDPOINT_URL_S3` | S3 only: the bucket's region, and an optional S3-compatible endpoint (MinIO, an emulator), which switches to path-style addressing. Credentials come from the AWS default chain (IRSA, Pod Identity, task role, instance profile). |
| `SATCHEL_LOG_FORMAT` | `text` (default) or `json`. |
| `PORT` | listen port (Cloud Run sets it). Image default 8080. |
| `SATCHEL_FASTEMBED_CACHE_DIR` | set to `/opt/satchel/fastembed` in the image. |

Cloud SQL URLs use the unix socket form: `postgresql+psycopg://USER:PASS@/satchel?host=/cloudsql/PROJECT:REGION:INSTANCE`.

## Ingress

Single host: `/` UI, `/v1` REST, `/mcp` MCP, `/oauth`, `/auth`, `/.well-known`. No response buffering on `/mcp` (SSE); request timeout of at least 300 s.
