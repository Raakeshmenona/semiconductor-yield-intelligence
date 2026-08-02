"""
End-to-End Local Pipeline Runner
================================
Executes the full pipeline on your laptop — no SAP, Teamcenter or Snowflake
required (the SAP gateway is mocked, the Snowflake load runs --dry-run):

  1. generate synthetic fab data      (skipped if data/raw already exists)
  2. SAP RFC extraction (mock)        -> JSON BOM tree
  3. Teamcenter PLMXML round-trip     -> normalized tables
  4. networkx BOM explosion           -> KPI: 8,000 nodes < 2 min
  5. ML root-cause correlation        -> KPI: shortlist < 10 min
  6. supplier lot ranking + anomaly screen
  7. Snowflake load plan (dry-run)
  8. root-cause report + ground-truth validation

Usage:
    python scripts/run_pipeline_local.py [--regen] [--validate]

``--validate`` exits non-zero if the ML shortlist misses any injected root
cause (used as the CI smoke gate).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

RAW = PROJECT_ROOT / "data" / "raw"
OUT = PROJECT_ROOT / "data" / "outputs"
REPORTS = PROJECT_ROOT / "reports"


def banner(step: str) -> None:
    print(f"\n{'=' * 74}\n  {step}\n{'=' * 74}")


def main(regen: bool, validate: bool) -> int:
    import pandas as pd
    t_pipeline = time.perf_counter()

    # ---- 1. synthetic data -------------------------------------------- #
    banner("STEP 1/8  Synthetic fab dataset")
    if regen or not (RAW / "wafer_lots.csv").exists():
        from data.generate_synthetic_data import main as gen
        gen()
    else:
        print(f"  reusing existing dataset in {RAW}")

    # ---- 2. SAP extraction (mock RFC) ---------------------------------- #
    banner("STEP 2/8  SAP BOM extraction (CS_BOM_EXPL_MAT_V2 via mock RFC)")
    from bom.sap_bom_extractor import (MockSAPGateway, SAPBOMExtractor,
                                       flatten_tree)
    gw = MockSAPGateway(RAW / "bom_edges.csv", RAW / "bom_nodes.csv")
    extractor = SAPBOMExtractor(gw)
    tree = extractor.extract_tree("IX-9000", "1000")
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "sap_bom_tree.json").write_text(json.dumps(tree))
    sap_rows = len(flatten_tree(tree))
    print(f"  {sap_rows:,} usage rows via {extractor.calls:,} RFC calls")

    # ---- 3. Teamcenter PLMXML ------------------------------------------ #
    banner("STEP 3/8  Teamcenter PLMXML parse")
    from bom.teamcenter_xml_parser import parse_plmxml, write_sample_plmxml
    xml = write_sample_plmxml(pd.read_csv(RAW / "bom_edges.csv"),
                              pd.read_csv(RAW / "bom_nodes.csv"),
                              OUT / "sample_plmxml.xml")
    tables = parse_plmxml(xml)
    for name, df in tables.items():
        df.to_csv(OUT / f"teamcenter_{name}.csv", index=False)
        print(f"  {name:12s} {len(df):>6,} rows")

    # ---- 4. BOM explosion (KPI) ---------------------------------------- #
    banner("STEP 4/8  networkx BOM explosion  [KPI: < 120 s]")
    from bom.bom_graph_builder import run_explosion
    kpi = run_explosion(RAW / "bom_edges.csv", RAW / "bom_nodes.csv",
                        "IX-9000", OUT)

    # ---- 5. ML root-cause ---------------------------------------------- #
    banner("STEP 5/8  Random Forest defect correlation  [KPI: < 600 s]")
    t0 = time.perf_counter()
    from analytics.yield_defect_correlator import run as run_corr
    shortlist = run_corr(RAW, OUT)
    rca_seconds = time.perf_counter() - t0

    # ---- 6. supplier ranking + anomaly screen -------------------------- #
    banner("STEP 6/8  Supplier lot ranking")
    from analytics.supplier_lot_ranker import run as run_rank
    ranking = run_rank(RAW, OUT)

    banner("STEP 6b   IsolationForest process anomaly screen")
    from analytics.anomaly_flagging import run as run_anom
    anomalies = run_anom(RAW, OUT)

    # ---- 7. Snowflake load plan ---------------------------------------- #
    banner("STEP 7/8  Snowflake load (dry-run)")
    sys.path.insert(0, str(PROJECT_ROOT / "snowflake"))
    from yield_mart_loader import load
    load(RAW, OUT, dry_run=True)

    # ---- 8. report + validation ---------------------------------------- #
    banner("STEP 8/8  Root-cause report & ground-truth validation")
    truth = json.loads((RAW / "ground_truth.json").read_text())
    factors = " | ".join(shortlist["factor"].astype(str))
    top_lot = str(ranking.iloc[0]["supplier_lot_id"])

    checks = {
        "bad supplier batch in ML shortlist":
            truth["bad_supplier_lot"]["supplier_lot_id"] in factors,
        "bad revision in ML shortlist":
            f"[{truth['bad_revision']['component_id']}] rev "
            f"{truth['bad_revision']['revision']}" in factors,
        "process drift param in ML shortlist":
            truth["process_drift"]["parameter"] in factors,
        "bad batch ranks #1 in supplier ranking":
            top_lot == truth["bad_supplier_lot"]["supplier_lot_id"],
        "explosion KPI < 120 s": kpi["elapsed_seconds"] < 120,
        "root-cause KPI < 600 s": rca_seconds < 600,
    }

    exc = pd.read_csv(OUT / "yield_excursions.csv")
    cost = pd.read_csv(OUT / "supplier_lot_ranking.csv")
    avoided = float(cost["est_cost_impact_usd"].max())

    REPORTS.mkdir(exist_ok=True)
    report = [
        "# Yield Intelligence — Run Report", "",
        f"*Generated by scripts/run_pipeline_local.py*", "",
        "## KPIs",
        f"- BOM explosion: **{kpi['nodes']:,} nodes / "
        f"{kpi['explosion_rows']:,} usage rows in "
        f"{kpi['elapsed_seconds']} s** (target < 120 s; manual ~3 days)",
        f"- Root-cause shortlist: **{rca_seconds:.1f} s** "
        "(target < 600 s; manual 2-3 days)",
        f"- Excursion lots flagged: **{int(exc['is_excursion'].sum())}** "
        f"of {len(exc)}",
        f"- Largest single-batch cost impact: **${avoided:,.0f}** "
        "(cost avoidable by quarantining the batch)", "",
        "## Automated root-cause shortlist", "",
        shortlist.to_markdown(), "",
        "## Validation vs injected ground truth", "",
    ]
    for name, ok in checks.items():
        report.append(f"- {'PASS' if ok else 'FAIL'} — {name}")
    (REPORTS / "root_cause_report.md").write_text(
        "\n".join(report), encoding="utf-8")

    print()
    for name, ok in checks.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    print(f"\n  Report: {REPORTS / 'root_cause_report.md'}")
    print(f"  Total pipeline time: {time.perf_counter() - t_pipeline:.1f} s")

    if validate and not all(checks.values()):
        print("\nVALIDATION FAILED", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--regen", action="store_true",
                    help="force regeneration of the synthetic dataset")
    ap.add_argument("--validate", action="store_true",
                    help="exit 1 unless every ground-truth check passes")
    a = ap.parse_args()
    sys.exit(main(a.regen, a.validate))
