"""
Yield <-> Defect Correlator  (Random Forest feature importance)
===============================================================
Answers the question fab engineers spend 2-3 days on manually:

    "Yield dropped — WHICH BOM revision, supplier batch or process
     parameter is responsible?"

Approach
--------
1. Build a per-wafer-lot feature matrix from the as-built genealogy:
   one-hot columns for every (component, revision) and (component,
   supplier lot) actually consumed, plus the numeric process telemetry.
2. Fit a RandomForestRegressor on yield %, score on a holdout split.
3. Rank factors by *permutation importance* (robust against one-hot
   cardinality bias), then attach the observed yield delta for each
   factor (mean yield with factor vs without) so every candidate in the
   shortlist comes with magnitude AND direction.

Output: a ranked root-cause shortlist — the "automated 10-minute answer".

Usage:
    python analytics/yield_defect_correlator.py \
        --raw data/raw --out data/outputs
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Optional, Tuple

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestRegressor
from sklearn.inspection import permutation_importance
from sklearn.metrics import r2_score
from sklearn.model_selection import train_test_split


class YieldDefectCorrelator:
    def __init__(self, wafer_lots: pd.DataFrame, lot_config: pd.DataFrame,
                 process_params: pd.DataFrame, random_state: int = 7) -> None:
        self.wafer_lots = wafer_lots
        self.lot_config = lot_config
        self.process_params = process_params
        self.random_state = random_state
        self.model: Optional[RandomForestRegressor] = None
        self.features: Optional[pd.DataFrame] = None
        self.target: Optional[pd.Series] = None
        self.holdout_r2: float = float("nan")

    # ------------------------------------------------------------------ #
    def build_feature_matrix(self) -> Tuple[pd.DataFrame, pd.Series]:
        """lot genealogy + process telemetry -> one row per wafer lot."""
        cfg = self.lot_config.copy()
        cfg["rev_feat"] = ("REV|" + cfg["component_id"] + "|"
                           + cfg["component_name"] + "|rev " + cfg["revision"])
        cfg["lot_feat"] = ("SUPLOT|" + cfg["component_id"] + "|"
                           + cfg["component_name"] + "|" + cfg["supplier_lot_id"])

        rev_oh = pd.crosstab(cfg["wafer_lot_id"], cfg["rev_feat"])
        lot_oh = pd.crosstab(cfg["wafer_lot_id"], cfg["lot_feat"])

        proc = self.process_params.set_index("wafer_lot_id")
        proc.columns = ["PROC|" + c for c in proc.columns]

        one_hot = rev_oh.join(lot_oh, how="inner").clip(upper=1)
        X = one_hot.join(proc, how="inner")

        y = (self.wafer_lots.set_index("wafer_lot_id")
                            .loc[X.index, "yield_pct"])
        self.features, self.target = X, y
        return X, y

    # ------------------------------------------------------------------ #
    def detect_excursions(self, sigma: float = 3.0) -> pd.DataFrame:
        """Flag lots whose yield falls below (median - sigma * robust-sigma)."""
        y = self.wafer_lots["yield_pct"]
        med = y.median()
        robust_sigma = 1.4826 * (y - med).abs().median()   # MAD estimator
        threshold = med - sigma * robust_sigma
        out = self.wafer_lots.copy()
        out["baseline_yield"] = round(med, 2)
        out["excursion_threshold"] = round(threshold, 2)
        out["is_excursion"] = out["yield_pct"] < threshold
        return out

    # ------------------------------------------------------------------ #
    def fit(self) -> "YieldDefectCorrelator":
        if self.features is None:
            self.build_feature_matrix()
        X, y = self.features, self.target
        X_tr, X_te, y_tr, y_te = train_test_split(
            X, y, test_size=0.25, random_state=self.random_state)
        self.model = RandomForestRegressor(
            n_estimators=400, min_samples_leaf=2, n_jobs=-1,
            random_state=self.random_state)
        self.model.fit(X_tr, y_tr)
        self.holdout_r2 = r2_score(y_te, self.model.predict(X_te))
        self._X_te, self._y_te = X_te, y_te
        return self

    # ------------------------------------------------------------------ #
    def root_cause_shortlist(self, top_n: int = 12) -> pd.DataFrame:
        """Permutation importance + observed yield delta per factor."""
        if self.model is None:
            self.fit()
        perm = permutation_importance(
            self.model, self._X_te, self._y_te, n_repeats=8,
            random_state=self.random_state, n_jobs=-1)

        rows = []
        for col, imp in zip(self.features.columns, perm.importances_mean):
            kind, *_ = col.split("|")
            if kind == "PROC":
                x = self.features[col]
                hi = self.target[x > x.quantile(0.9)].mean()
                lo = self.target[(x >= x.quantile(0.1))
                                 & (x <= x.quantile(0.9))].mean()
                delta, n_aff = hi - lo, int((x > x.quantile(0.9)).sum())
                label = col.split("|", 1)[1]
                detail = "high decile vs mid-range"
            else:
                mask = self.features[col] == 1
                if mask.sum() < 3:
                    continue
                delta = self.target[mask].mean() - self.target[~mask].mean()
                n_aff = int(mask.sum())
                _, comp_id, comp_name, value = col.split("|", 3)
                label = f"{comp_name} [{comp_id}] {value}"
                detail = ("BOM revision" if kind == "REV"
                          else "supplier batch")
            rows.append({"factor": label, "factor_type": detail,
                         "permutation_importance": round(float(imp), 4),
                         "yield_delta_pp": round(float(delta), 2),
                         "lots_affected": n_aff})

        out = (pd.DataFrame(rows)
               .sort_values("permutation_importance", ascending=False)
               .head(top_n)
               .reset_index(drop=True))
        out.index += 1
        out.index.name = "rank"
        return out

    # ------------------------------------------------------------------ #
    def report(self, shortlist: pd.DataFrame, elapsed_s: float) -> str:
        exc = self.detect_excursions()
        n_exc = int(exc["is_excursion"].sum())
        lines = [
            "ROOT-CAUSE SHORTLIST (automated)",
            "=" * 70,
            f"Lots analysed        : {len(self.wafer_lots):,}",
            f"Excursion lots       : {n_exc} "
            f"(below {exc['excursion_threshold'].iloc[0]}%)",
            f"Model holdout R^2    : {self.holdout_r2:.3f}",
            f"Time to shortlist    : {elapsed_s:.1f}s   "
            "(KPI target < 10 min; manual baseline 2-3 days)",
            "",
            shortlist.to_string(),
        ]
        return "\n".join(lines)


# ---------------------------------------------------------------------- #
def run(raw_dir: Path, out_dir: Path) -> pd.DataFrame:
    t0 = time.perf_counter()
    corr = YieldDefectCorrelator(
        pd.read_csv(raw_dir / "wafer_lots.csv"),
        pd.read_csv(raw_dir / "wafer_lot_config.csv"),
        pd.read_csv(raw_dir / "process_parameters.csv"))
    corr.fit()
    shortlist = corr.root_cause_shortlist()
    elapsed = time.perf_counter() - t0

    out_dir.mkdir(parents=True, exist_ok=True)
    shortlist.to_csv(out_dir / "root_cause_shortlist.csv")
    corr.detect_excursions().to_csv(out_dir / "yield_excursions.csv",
                                    index=False)
    print(corr.report(shortlist, elapsed))
    return shortlist


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--raw", type=Path, default=Path("data/raw"))
    ap.add_argument("--out", type=Path, default=Path("data/outputs"))
    a = ap.parse_args()
    run(a.raw, a.out)
