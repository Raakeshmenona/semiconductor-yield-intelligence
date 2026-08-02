"""
Yield Mart Loader  (Python -> Snowflake via snowflake-connector-python)
=======================================================================
Loads the raw layer (BOM structure, genealogy, wafer lots, telemetry) and
the analytics outputs (exploded BOM, root-cause shortlist, supplier
ranking, anomaly flags) into Snowflake with ``write_pandas`` bulk loading.

Connection comes from environment variables:
    SNOWFLAKE_ACCOUNT, SNOWFLAKE_USER, SNOWFLAKE_PASSWORD,
    SNOWFLAKE_ROLE (opt), SNOWFLAKE_WAREHOUSE (default COMPUTE_WH)

``--dry-run`` prints the load plan (tables, row counts, DDL) without
connecting — used by CI and by the local demo.

Usage:
    python snowflake/yield_mart_loader.py --raw data/raw \
        --outputs data/outputs [--dry-run]
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import pandas as pd

DATABASE = "YIELD_INTEL"

# (csv file, target schema, target table)
LOAD_PLAN = [
    ("raw",     "bom_nodes.csv",              "RAW",       "BOM_NODES"),
    ("raw",     "bom_edges.csv",              "RAW",       "BOM_EDGES"),
    ("raw",     "component_revisions.csv",    "RAW",       "COMPONENT_REVISIONS"),
    ("raw",     "supplier_lots.csv",          "RAW",       "SUPPLIER_LOTS"),
    ("raw",     "wafer_lots.csv",             "RAW",       "WAFER_LOTS"),
    ("raw",     "wafer_lot_config.csv",       "RAW",       "WAFER_LOT_CONFIG"),
    ("raw",     "process_parameters.csv",     "RAW",       "PROCESS_PARAMETERS"),
    ("outputs", "bom_exploded.csv",           "ANALYTICS", "BOM_EXPLODED_PY"),
    ("outputs", "root_cause_shortlist.csv",   "ANALYTICS", "ROOT_CAUSE_SHORTLIST"),
    ("outputs", "supplier_lot_ranking.csv",   "ANALYTICS", "SUPPLIER_LOT_RANKING"),
    ("outputs", "yield_excursions.csv",       "ANALYTICS", "YIELD_EXCURSIONS"),
    ("outputs", "process_anomalies.csv",      "ANALYTICS", "PROCESS_ANOMALIES"),
]


def _connect():
    """Late import so the rest of the pipeline runs without the connector."""
    import snowflake.connector  # noqa: WPS433  (deliberate late import)
    return snowflake.connector.connect(
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        user=os.environ["SNOWFLAKE_USER"],
        password=os.environ["SNOWFLAKE_PASSWORD"],
        role=os.environ.get("SNOWFLAKE_ROLE"),
        warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE", "COMPUTE_WH"),
        session_parameters={"QUERY_TAG": "yield_intelligence_loader"},
    )


def _bootstrap(cur) -> None:
    cur.execute(f"CREATE DATABASE IF NOT EXISTS {DATABASE}")
    for schema in ("RAW", "ANALYTICS", "STAGING", "MARTS"):
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {DATABASE}.{schema}")


def load(raw_dir: Path, outputs_dir: Path, dry_run: bool = False) -> int:
    dirs = {"raw": raw_dir, "outputs": outputs_dir}
    plan = []
    for src, fname, schema, table in LOAD_PLAN:
        path = dirs[src] / fname
        if not path.exists():
            print(f"  skip  {schema}.{table:24s} ({fname} not found)")
            continue
        df = pd.read_csv(path)
        df.columns = [c.upper() for c in df.columns]
        plan.append((df, schema, table, fname))

    total = sum(len(df) for df, *_ in plan)
    if dry_run:
        print(f"[dry-run] would load {len(plan)} tables / {total:,} rows "
              f"into {DATABASE}:")
        for df, schema, table, fname in plan:
            print(f"  {schema}.{table:24s} {len(df):>8,} rows  <- {fname}")
        return total

    from snowflake.connector.pandas_tools import write_pandas
    conn = _connect()
    try:
        cur = conn.cursor()
        _bootstrap(cur)
        for df, schema, table, fname in plan:
            ok, _chunks, nrows, _ = write_pandas(
                conn, df, table_name=table, database=DATABASE,
                schema=schema, auto_create_table=True, overwrite=True)
            status = "ok" if ok else "FAILED"
            print(f"  {status:6s} {schema}.{table:24s} {nrows:>8,} rows")
        # build the in-warehouse explosion once the raw layer is present
        sql_file = Path(__file__).with_name("bom_recursive_cte.sql")
        for stmt in sql_file.read_text().split(";"):
            if stmt.strip():
                cur.execute(stmt)
        print("  ok     ANALYTICS.BOM_EXPLODED (recursive CTE rebuilt)")
    finally:
        conn.close()
    return total


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--raw", type=Path, default=Path("data/raw"))
    ap.add_argument("--outputs", type=Path, default=Path("data/outputs"))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    try:
        load(a.raw, a.outputs, a.dry_run)
    except KeyError as exc:
        sys.exit(f"Missing environment variable: {exc}. "
                 "Set SNOWFLAKE_ACCOUNT/USER/PASSWORD or use --dry-run.")
