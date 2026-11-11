#!/usr/bin/env bash
# Give an emulated Cloud SQL Postgres instance the Cloud SQL semantics satchel relies on.
#
# On Cloud SQL, users created through the Admin API (google_sql_user) are members of
# cloudsqlsuperuser: they have CREATEROLE and CREATEDB, databases created through the API belong
# to cloudsqlsuperuser, and members may CREATE EXTENSION vector. floci-gcp creates plain LOGIN
# roles in a vanilla postgres container instead, so `satchel db bootstrap` (as the owner) could
# neither create the extension nor the satchel_app / satchel_worker roles. This reproduces the Cloud
# SQL setup with the built-in postgres superuser; everything else then runs as in production.
#
# It also creates the database through the Admin API when it is missing (the floci examples set
# foundation create_database = false, see docs/deploy/floci.md).
#
#   cloudsql-shim.sh <instance> [owner=satchel] [database=satchel]
# Env: FLOCI_ENDPOINT (default http://localhost:4588), FLOCI_PROJECT (floci-local),
#      FLOCI_NAMESPACE (satchel).
set -euo pipefail

instance="$1"
owner="${2:-satchel}"
db="${3:-satchel}"
endpoint="${FLOCI_ENDPOINT:-http://localhost:4588}"
project="${FLOCI_PROJECT:-floci-local}"
ns="${FLOCI_NAMESPACE:-satchel}"

base="$endpoint/sql/v1beta4/projects/$project/instances/$instance/databases"
if ! curl -fsS "$base/$db" >/dev/null 2>&1; then
  curl -fsS -X POST "$base" -H 'content-type: application/json' \
    -d "{\"name\":\"$db\",\"instance\":\"$instance\",\"project\":\"$project\"}" >/dev/null
  echo "cloudsql-shim: created database $db on $instance"
fi

cid=""
for _ in $(seq 1 30); do
  cid="$(docker ps -q --filter label=io.floci.service=cloudsql \
    --filter "label=io.floci.resource-id=$instance" --filter "label=floci_namespace=$ns" | head -n1)"
  [ -n "$cid" ] && docker exec "$cid" pg_isready -U postgres -q && break
  sleep 2
done
if [ -z "$cid" ]; then
  echo "no floci Cloud SQL container for instance $instance" >&2
  exit 1
fi

docker exec -i "$cid" psql -v ON_ERROR_STOP=1 -U postgres -d postgres -q <<SQL
DO \$\$BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cloudsqlsuperuser') THEN
    CREATE ROLE cloudsqlsuperuser NOLOGIN CREATEROLE CREATEDB;
  END IF;
END\$\$;
ALTER ROLE "$owner" CREATEROLE CREATEDB;
GRANT cloudsqlsuperuser TO "$owner";
ALTER DATABASE "$db" OWNER TO cloudsqlsuperuser;
\c "$db"
CREATE EXTENSION IF NOT EXISTS vector;
SQL
echo "cloudsql-shim: $instance: $owner is a cloudsqlsuperuser member, $db belongs to cloudsqlsuperuser, extension vector created"
