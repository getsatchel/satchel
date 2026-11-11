# Jaffle Shop Cube data model

A Cube data model over the same data as the Jaffle Shop Satchel context: cubes `orders`, `customers`
and `payments` (YAML, `model/cubes/`), a view (`model/views/`), and the measures `revenue`,
`order_count` and `average_order_value` with the same definitions as the dbt semantic model and
the hand-written MDL. Satchel reads the model files (`docs/integrations/cube.md`); no Cube server is
needed to use Satchel.

```bash
satchel source validate cube --models model            # what satchel would import (--json for all)
satchel source add cube jaffle-cube --models model --connection warehouse --dialect duckdb
satchel source sync jaffle-cube
```

The model builds on `analytics.orders|customers|payments`, the relations the hand-written context
and the dbt example document (`python ../dbt/prepare_raw.py` plus `dbt build`, or the seed in
`../seed.py`).

## Checked against a real Cube

The model loads in Cube 1.8.18 and answers March 2025 revenue with 1716.15 over 25 orders (the
number `query_metrics` returns over the satchel-derived MDL), with the server's session in UTC:

```bash
npm i @cubejs-backend/server @cubejs-backend/duckdb-driver jsonwebtoken
CUBEJS_DB_TYPE=duckdb CUBEJS_DB_DUCKDB_DATABASE_PATH=$PWD/jaffle.duckdb \
  CUBEJS_API_SECRET=secret CUBEJS_DEV_MODE=false CUBEJS_CACHE_AND_QUEUE_DRIVER=memory TZ=UTC \
  npx cubejs-server        # reads ./model; Cube's DuckDB driver needs a DuckDB file it can open
```

`server/tests/fixtures/cube/meta.json` is the trimmed `/cubejs-api/v1/meta` response of that
server with one extra JavaScript cube (`server/tests/fixtures/cube/edge/legacy.js`), used by the
tests; `satchel source add cube ... --meta-url http://localhost:4000 --meta-token-env CUBE_TOKEN`
reads the live one.
