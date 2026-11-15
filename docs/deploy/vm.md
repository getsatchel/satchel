# One VM with Docker Compose

The smallest real deployment: one Linux machine running Postgres, the Satchel web server, the
worker, and [Caddy](https://caddyserver.com/) for HTTPS. It suits a trial or a small team. The files
are in [`deploy/vm/`](../../deploy/vm/).

```
Claude / browser ──https──▶ Caddy (Let's Encrypt) ──▶ web :8080 ──▶ Postgres (pgvector)
                                                       worker ──────▶
```

The example uses AWS EC2 and Amazon Cognito, but any x86-64 Linux host and any OIDC provider work
the same way. The image is built for `linux/amd64` only, so choose an x86 instance, not ARM
(Graviton).

## 1. Prerequisites

- A domain or subdomain you control, for example `satchel.example.com`.
- An OIDC provider. Section 3 sets up Cognito.
- A billing alert on your cloud account before you create anything (AWS: Billing → Budgets).

## 2. Create the machine

On EC2 (Ubuntu 24.04 LTS, x86-64):

- Instance type `t3.medium` (2 vCPU, 4 GiB). The web server and worker each load the local
  embedding model (about 1.3 GiB). A `t3.small` (2 GiB) works if you set
  `SATCHEL_EMBEDDING_PROVIDER` to `openai_compat` or `hash` (section 4).
- A 30 GiB gp3 root volume.
- A security group allowing 22 from your IP only, and 80 and 443 from anywhere. Caddy needs 80 to
  get the certificate.
- An Elastic IP, so the address survives a stop and start.

Point the domain at the Elastic IP with an `A` record, and wait until `dig +short <domain>` returns it.

Install Docker on the machine:

```bash
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2 git
sudo usermod -aG docker "$USER"     # log out and back in to use docker without sudo
```

Add 2 GiB of swap as a margin against running out of memory:

```bash
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

## 3. Create the OIDC client (Cognito)

1. In Amazon Cognito, create a user pool. Choose a **traditional web application** app client,
   with email as the sign-in identifier.
2. On the app client, set the allowed callback URL to `https://<domain>/auth/callback` and the
   scopes to `openid`, `email` and `profile`.
3. Note the user pool ID, the client ID and the client secret.
4. Create a user with the email you will use as the workspace admin.

The issuer is `https://cognito-idp.<region>.amazonaws.com/<user-pool-id>`.

**Inviting people.** An invite under **Settings > Members** lets someone into the workspace, but
they also need a Cognito user. Either:

- set `SATCHEL_OIDC_CREATE_USERS=true` in `.env` and give the instance an IAM role allowed
  `cognito-idp:AdminCreateUser` on the pool. Inviting someone then creates their Cognito user and
  Cognito emails them a temporary password. Edit the pool's invitation message to include your
  Satchel URL; or
- turn on self sign-up in the pool and send people the invite link from the Members tab. Anyone
  can create a Cognito user, but only people you invited get into Satchel (unless the Sign-in
  setting lets others join as explorers).

## 4. Configure and start

```bash
git clone --depth 1 https://github.com/getsatchel/satchel.git
cd satchel/deploy/vm
cp .env.example .env
chmod 600 .env
for v in POSTGRES_PASSWORD SATCHEL_APP_DB_PASSWORD SATCHEL_WORKER_DB_PASSWORD SATCHEL_ENCRYPTION_KEY; do
  sed -i "s/^$v=$/$v=$(openssl rand -hex 32)/" .env
done
nano .env       # domain, OIDC issuer / client id / secret, workspace and admin email
docker compose up -d
```

`up` runs three one-shot steps in order, then starts the long-running services:

| Service | Does |
|---|---|
| `db-bootstrap` | `satchel db bootstrap`: creates `satchel_app` and `satchel_worker` with your passwords ([Database](database.md)). |
| `db-upgrade` | `satchel db upgrade`: runs the migrations as the owner. |
| `first-workspace` | `satchel workspace create`: the workspace and its admin. |
| `web`, `worker`, `caddy` | The server, the background worker, and HTTPS. |

All three steps are safe to run again, so every `up` repeats them. `SATCHEL_ENV=production` is set,
so the server refuses to start without OIDC or with an `http://` public URL
([Container: production mode](container.md#production-mode)).

**Embeddings.** The default, `fastembed`, runs a local model and needs the 4 GiB machine. Otherwise
set `SATCHEL_EMBEDDING_PROVIDER=openai_compat` with `SATCHEL_EMBEDDING_API_KEY` (OpenAI
`text-embedding-3-large`, asked for 1024 dimensions; any compatible endpoint works through
`SATCHEL_EMBEDDING_BASE_URL` and `SATCHEL_EMBEDDING_MODEL`), or `hash`, which needs no model but
searches poorly. After changing the provider, run
`docker compose run --rm web sync reembed -w <workspace>`.

Back up `SATCHEL_ENCRYPTION_KEY` somewhere other than this machine. Without it, the secrets saved in
Settings must be entered again ([Configuration](configuration.md)).

## 5. Check it

```bash
docker compose ps                       # the setup steps "Exited (0)"; web "healthy"
curl -s https://<domain>/readyz         # {"status":"ok"}
docker compose logs -f web worker
```

Open `https://<domain>` and sign in with the admin email. Then connect Claude Code:

```bash
claude mcp add --transport http satchel https://<domain>/mcp    # then /mcp to sign in
```

## 6. Operate

- **Upgrade:** set `SATCHEL_IMAGE_TAG` in `.env` to the new release, then
  `docker compose pull && docker compose up -d`. The migration step runs before the server restarts.
- **Back up the database:**
  `docker compose exec -T postgres pg_dump -U satchel -Fc satchel > satchel-$(date +%F).dump`.
  Copy the dump off the machine (for example to S3). An EBS snapshot schedule is a simple second layer.
- **Logs:** `docker compose logs`. Each line is JSON (`SATCHEL_LOG_FORMAT=json`).
- **Grow out of it:** for several replicas, a managed database or high availability, move to
  [EKS](eks.md), [GKE](gke.md) or [Cloud Run](cloud-run.md). Restore the `pg_dump` into the new database.

## 7. Costs

At on-demand list prices in `us-east-1`, a `t3.medium` with a 30 GiB gp3 volume and an Elastic IP is
roughly USD 35 to 40 a month; a `t3.small` about USD 20 to 25. New AWS accounts' free-tier credits
cover a while of either. Cognito's free tier covers a small team. Check the current pricing
pages before relying on these numbers.

## 8. Teardown

`docker compose down` stops everything and keeps the data. `docker compose down -v` also deletes
the database, the result cache and the certificates. Then terminate the instance and release the
Elastic IP: AWS bills every public IPv4 address by the hour, attached or not.
