"""Analytics unit tests on a small hand-built dataset with one obvious
bad supplier batch, so the correlator/ranker must find it."""
import numpy as np
import pandas as pd
import pytest

from analytics.supplier_lot_ranker import rank_supplier_lots
from analytics.anomaly_flagging import flag_anomalies
from analytics.yield_defect_correlator import YieldDefectCorrelator

RNG = np.random.default_rng(0)
N = 120
BAD_LOT = "SL-BAD-1"


@pytest.fixture(scope="module")
def dataset():
    lot_ids = [f"WL-{i:03d}" for i in range(N)]
    bad_users = set(lot_ids[:20])

    cfg, params, lots = [], [], []
    for i, wl in enumerate(lot_ids):
        supplier_lot = BAD_LOT if wl in bad_users else f"SL-OK-{i % 4}"
        cfg.append({"wafer_lot_id": wl, "component_id": "CC-01",
                    "component_name": "Photoresist", "revision": "A",
                    "supplier_id": "ApexChem",
                    "supplier_lot_id": supplier_lot})
        y = 92 + RNG.normal(0, 0.8) - (7 if wl in bad_users else 0)
        lots.append({"wafer_lot_id": wl, "product_id": "P", "wafers": 25,
                     "die_per_wafer": 1000, "yield_pct": y,
                     "good_die": int(25000 * y / 100),
                     "defect_die": 25000 - int(25000 * y / 100),
                     "fab_start_date": "2026-01-01",
                     "fab_finish_date": "2026-01-04"})
        params.append({"wafer_lot_id": wl,
                       "temp_c": float(RNG.normal(65, 1)),
                       "power_w": float(RNG.normal(1500, 20))})

    supplier_lots = pd.DataFrame([
        {"supplier_lot_id": sl, "component_id": "CC-01",
         "component_name": "Photoresist", "supplier_id": "ApexChem",
         "received_date": "2025-12-01", "qty_received": 100,
         "coa_passed": True}
        for sl in {c["supplier_lot_id"] for c in cfg}])
    return (pd.DataFrame(lots), pd.DataFrame(cfg),
            pd.DataFrame(params), supplier_lots)


def test_ranker_finds_bad_batch(dataset):
    lots, cfg, _params, slots = dataset
    ranked = rank_supplier_lots(lots, cfg, slots)
    assert ranked.iloc[0]["supplier_lot_id"] == BAD_LOT
    assert ranked.iloc[0]["yield_delta_pp"] < -5
    assert ranked.iloc[0]["p_value"] < 0.01


def test_correlator_shortlists_bad_batch(dataset):
    lots, cfg, params, _slots = dataset
    corr = YieldDefectCorrelator(lots, cfg, params).fit()
    assert corr.holdout_r2 > 0.5
    shortlist = corr.root_cause_shortlist(top_n=3)
    assert any(BAD_LOT in f for f in shortlist["factor"])


def test_excursion_detection(dataset):
    lots, cfg, params, _slots = dataset
    corr = YieldDefectCorrelator(lots, cfg, params)
    exc = corr.detect_excursions()
    flagged = set(exc.loc[exc.is_excursion, "wafer_lot_id"])
    assert flagged  # the -7pp lots must trip the robust threshold
    assert all(wl.startswith("WL-0") for wl in flagged)


def test_anomaly_flagging_explains_deviation():
    n = 100
    params = pd.DataFrame({
        "wafer_lot_id": [f"WL-{i}" for i in range(n)],
        "temp_c": np.r_[np.random.default_rng(1).normal(65, 1, n - 3),
                        [72.0, 71.5, 73.0]],
        "power_w": np.random.default_rng(2).normal(1500, 20, n),
    })
    flags, detail = flag_anomalies(params, contamination=0.05)
    hot = flags[flags.wafer_lot_id.isin({"WL-97", "WL-98", "WL-99"})]
    assert hot["is_anomaly"].all()
    assert (hot["top_deviating_param"] == "temp_c").all()
    assert set(detail.columns) == {"wafer_lot_id", "parameter", "robust_z"}
