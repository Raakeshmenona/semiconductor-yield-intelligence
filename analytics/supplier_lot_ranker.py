"""
Supplier Lot Ranker  (defect contribution scoring)
==================================================
Ranks every incoming supplier batch by how much yield loss it explains:

  * yield delta      : mean yield of wafer lots consuming the batch minus
                       mean yield of all other lots of the same period
  * Welch t / p      : significance of that delta (unequal variances;
                       normal approximation — no SciPy dependency)
  * shrunk delta     : empirical-Bayes shrinkage ``delta * n / (n + k)`` so a
                       batch used by 2 lots can't top the table on noise
  * est. cost impact : defect die attributable to the batch x cost per die

The output feeds ``dim_supplier_lot`` in the warehouse and the Power BI
"Supplier Pareto" page.

Usage:
    python analytics/supplier_lot_ranker.py --raw data/raw --out data/outputs
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path

import numpy as np
import pandas as pd

SHRINKAGE_K = 8            # pseudo-lot count for empirical-Bayes shrinkage
COST_PER_GOOD_DIE = 12.50  # USD, used for cost-avoidance quantification


def _welch_p_value(a: np.ndarray, b: np.ndarray) -> float:
    """Two-sided Welch t-test p-value via normal approximation."""
    n1, n2 = len(a), len(b)
    if n1 < 2 or n2 < 2:
        return 1.0
    v1, v2 = a.var(ddof=1), b.var(ddof=1)
    se = math.sqrt(v1 / n1 + v2 / n2)
    if se == 0:
        return 1.0
    t = (a.mean() - b.mean()) / se
    return math.erfc(abs(t) / math.sqrt(2))


def rank_supplier_lots(wafer_lots: pd.DataFrame, lot_config: pd.DataFrame,
                       supplier_lots: pd.DataFrame) -> pd.DataFrame:
    yields = wafer_lots.set_index("wafer_lot_id")["yield_pct"]
    die_per_lot = (wafer_lots["wafers"] * wafer_lots["die_per_wafer"]).iloc[0]
    overall_mean = float(yields.mean())

    rows = []
    for (sl_id, comp_id), grp in lot_config.groupby(
            ["supplier_lot_id", "component_id"]):
        users = grp["wafer_lot_id"].unique()
        y_in = yields.loc[yields.index.intersection(users)].to_numpy()
        y_out = yields.loc[~yields.index.isin(users)].to_numpy()
        if len(y_in) == 0:
            continue
        delta = float(y_in.mean() - y_out.mean())
        shrunk = delta * len(y_in) / (len(y_in) + SHRINKAGE_K)
        p = _welch_p_value(y_in, y_out)
        # defect die attributable to this batch (only when it hurts yield)
        attributable = max(0.0, -delta) / 100.0 * die_per_lot * len(y_in)
        rows.append({
            "supplier_lot_id": sl_id,
            "component_id": comp_id,
            "wafer_lots_using": len(y_in),
            "mean_yield_with": round(float(y_in.mean()), 2),
            "mean_yield_without": round(float(y_out.mean()), 2),
            "yield_delta_pp": round(delta, 2),
            "shrunk_delta_pp": round(shrunk, 2),
            "p_value": round(p, 6),
            "attributable_defect_die": int(attributable),
            "est_cost_impact_usd": round(attributable * COST_PER_GOOD_DIE, 0),
        })

    df = pd.DataFrame(rows)
    df["defect_contribution_score"] = (
        -df["shrunk_delta_pp"] * -np.log10(df["p_value"].clip(lower=1e-12)))
    df = df.merge(
        supplier_lots[["supplier_lot_id", "component_id", "supplier_id",
                       "component_name", "received_date"]],
        on=["supplier_lot_id", "component_id"], how="left")
    df = (df.sort_values("defect_contribution_score", ascending=False)
            .reset_index(drop=True))
    df.index += 1
    df.index.name = "rank"
    df.attrs["overall_mean_yield"] = overall_mean
    return df


def run(raw_dir: Path, out_dir: Path) -> pd.DataFrame:
    ranked = rank_supplier_lots(
        pd.read_csv(raw_dir / "wafer_lots.csv"),
        pd.read_csv(raw_dir / "wafer_lot_config.csv"),
        pd.read_csv(raw_dir / "supplier_lots.csv"))
    out_dir.mkdir(parents=True, exist_ok=True)
    ranked.to_csv(out_dir / "supplier_lot_ranking.csv")

    top = ranked.head(10)[["supplier_lot_id", "component_name", "supplier_id",
                           "wafer_lots_using", "yield_delta_pp", "p_value",
                           "est_cost_impact_usd",
                           "defect_contribution_score"]]
    print("SUPPLIER LOT DEFECT-CONTRIBUTION RANKING (top 10)")
    print("=" * 70)
    print(top.to_string())
    return ranked


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--raw", type=Path, default=Path("data/raw"))
    ap.add_argument("--out", type=Path, default=Path("data/outputs"))
    a = ap.parse_args()
    run(a.raw, a.out)
