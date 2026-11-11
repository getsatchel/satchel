"""Create the DuckDB file the dbt project builds from: the Jaffle Shop tables in a `raw` schema.

    python prepare_raw.py [PATH]        # default: ./jaffle.duckdb (gitignored)

Then:  DBT_DUCKDB_PATH=$PWD/jaffle.duckdb dbt build --profiles-dir .   (models land in `analytics`)
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

import duckdb

SEED = Path(__file__).resolve().parent.parent / "seed.py"


def main(target: Path) -> None:
    spec = importlib.util.spec_from_file_location("jaffle_seed", SEED)
    assert spec and spec.loader
    seed = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(seed)
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "seed.duckdb"
        seed.build_database(src)
        target.unlink(missing_ok=True)
        con = duckdb.connect(str(target))
        con.execute(f"ATTACH '{src}' AS seed (READ_ONLY)")
        con.execute("CREATE SCHEMA raw")
        for t in ("customers", "orders", "payments"):
            con.execute(f"CREATE TABLE raw.{t} AS SELECT * FROM seed.analytics.{t}")
        con.close()
    print(f"wrote {target} (raw.customers, raw.orders, raw.payments)")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path("jaffle.duckdb"))
