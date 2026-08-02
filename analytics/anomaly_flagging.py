"""
Process Parameter Anomaly Flagging  (IsolationForest)
=====================================================
Screens per-lot process telemetry for multivariate anomalies *before* any
yield data exists — catching chamber drifts, mis-set recipes and sensor
excursions while wafers are still in the line.

For every flagged lot the module also explains WHY: the per-parameter
robust z-scores, sorted, so an engineer sees e.g.
``etch_chamber_temp_c z=+3.4`` immediately.

Usage:
    python analytics/anomaly_flagging.py --raw data/raw --out data/outputs
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Tuple

import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest


def robust_z(df: pd.DataFrame) -> pd.DataFrame:
    """Median/MAD z-scores — immune to the anomalies we're hunting."""
    med = df.median()
    mad = 1.4826 * (df - med).abs().median()
    return (df - med) / mad.replace(0, np.nan)


def flag_anomalies(process_params: pd.DataFrame,
                   contamination: float = 0.08,
                   random_state: int = 7
                   ) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Returns (flags per lot, long-form parameter deviations for flagged lots)."""
    X = process_params.set_index("wafer_lot_id")
    z = robust_z(X)

    iso = IsolationForest(n_estimators=300, contamination=contamination,
                          random_state=random_state)
    Xz = z.fillna(0).to_numpy()
    pred = iso.fit_predict(Xz)
    score = -iso.score_samples(Xz)              # higher = more anomalous

    flags = pd.DataFrame({
        "wafer_lot_id": X.index,
        "anomaly_score": np.round(score, 4),
        "is_anomaly": pred == -1,
    })
    # attach the single worst-deviating parameter per lot (headline)
    worst_param = z.abs().idxmax(axis=1)
    worst_z = z.abs().max(axis=1)
    flags["top_deviating_param"] = worst_param.values
    flags["top_deviation_z"] = np.round(
        [z.loc[i, p] for i, p in worst_param.items()], 2)
    flags["abs_top_z"] = np.round(worst_z.values, 2)

    detail = (z.loc[flags.loc[flags.is_anomaly, "wafer_lot_id"]]
                .reset_index()
                .melt(id_vars="wafer_lot_id", var_name="parameter",
                      value_name="robust_z")
                .dropna())
    detail = detail[detail["robust_z"].abs() >= 2.0]
    detail["robust_z"] = detail["robust_z"].round(2)
    detail = detail.sort_values(["wafer_lot_id", "robust_z"],
                                key=lambda s: (s.abs() if s.name == "robust_z"
                                               else s),
                                ascending=[True, False])
    return flags.sort_values("anomaly_score", ascending=False), detail


def run(raw_dir: Path, out_dir: Path) -> pd.DataFrame:
    params = pd.read_csv(raw_dir / "process_parameters.csv")
    flags, detail = flag_anomalies(params)

    out_dir.mkdir(parents=True, exist_ok=True)
    flags.to_csv(out_dir / "process_anomalies.csv", index=False)
    detail.to_csv(out_dir / "process_anomaly_detail.csv", index=False)

    n = int(flags["is_anomaly"].sum())
    print(f"PROCESS ANOMALY SCREEN: {n}/{len(flags)} lots flagged")
    print("=" * 70)
    print(flags[flags.is_anomaly]
          .head(10)[["wafer_lot_id", "anomaly_score",
                     "top_deviating_param", "top_deviation_z"]]
          .to_string(index=False))
    return flags


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--raw", type=Path, default=Path("data/raw"))
    ap.add_argument("--out", type=Path, default=Path("data/outputs"))
    a = ap.parse_args()
    run(a.raw, a.out)
