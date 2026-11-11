# Jaffle Shop dbt project

A small dbt project over the same data as the Jaffle Shop Satchel context: models `orders`, `customers`
and `payments` with descriptions and tests (including `accepted_values` and `relationships`), a
MetricFlow semantic model with `revenue`, `order_count` and `average_order_value`, one saved query,
and an exposure. Satchel reads its artifacts (`docs/integrations/dbt.md`); no dbt is needed to use Satchel.

```bash
pip install dbt-duckdb
python prepare_raw.py                                  # ./jaffle.duckdb with a `raw` schema
export DBT_DUCKDB_PATH=$PWD/jaffle.duckdb
dbt build --profiles-dir . && dbt docs generate --profiles-dir .
satchel source validate dbt --manifest target/manifest.json \
  --semantic-manifest target/semantic_manifest.json --catalog target/catalog.json
```

The models build into the `analytics` schema, the relations the hand-written context documents.
`server/tests/fixtures/dbt/` holds trimmed copies of the artifacts (`python trim_artifacts.py target
../../../server/tests/fixtures/dbt`), used by the tests.
