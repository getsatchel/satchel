# Jaffle Shop SQLMesh project

A SQLMesh project over the same data as the Jaffle Shop Satchel context: models `analytics.orders`,
`analytics.customers` and `analytics.payments` (kind `FULL`, with descriptions, declared columns,
column descriptions, `grain`, `references` and audits) and a `VIEW` over them,
`analytics.monthly_revenue`. Satchel reads the project files (`docs/integrations/sqlmesh.md`); no
SQLMesh is needed to use Satchel.

```bash
satchel source validate sqlmesh --project .          # what satchel would import (--json for all)
satchel source add sqlmesh jaffle-sqlmesh --project . --connection warehouse
satchel source sync jaffle-sqlmesh
```

To build the models with SQLMesh itself (optional; this is how the project was checked):

```bash
pip install sqlmesh duckdb
python ../dbt/prepare_raw.py jaffle_sqlmesh.duckdb     # the `raw` schema
sqlmesh plan --auto-apply                              # builds analytics.*
```

`server/tests/fixtures/sqlmesh/` holds a larger project with the edge cases the tests cover
(macros, Python models, kinds, audits).
