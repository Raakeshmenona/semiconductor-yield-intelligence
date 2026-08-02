"""
Synthetic Fab Dataset Generator
===============================
Creates a realistic multi-level semiconductor BOM (~8,000 nodes) plus 18 months
of wafer-lot production history with three *injected* root causes that the
analytics layer must rediscover blindly:

  1. Bad supplier batch : DUV photoresist lot ``SL-APX-4471``  -> -8 pp yield
  2. Bad design revision: electrostatic chuck rev ``C``        -> -4 pp yield
  3. Process drift      : etch chamber temp +3 sigma           -> -5 pp yield

Outputs (written to ``data/raw/``):
  bom_nodes.csv           one row per BOM node (part master)
  bom_edges.csv           parent/child edges with qty-per (multi-level DAG)
  component_revisions.csv revision history for critical components (ECN log)
  supplier_lots.csv       incoming supplier material batches
  wafer_lots.csv          production lots with yield outcome
  wafer_lot_config.csv    as-built genealogy: lot -> component rev + supplier lot
  process_parameters.csv  per-lot process telemetry (10 parameters)
  ground_truth.json       the injected causes (used only to validate the demo)

Usage:
    python data/generate_synthetic_data.py [--nodes 8000] [--lots 600]
"""
from __future__ import annotations

import argparse
import json
import random
from collections import deque
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

SEED = 42
RAW_DIR = Path(__file__).resolve().parent / "raw"

PRODUCT_ID = "IX-9000"
PRODUCT_DESC = "IX-9000 Wafer Inspection & Metrology Module"

LEVEL_CATEGORY = ["PRODUCT", "SUBSYSTEM", "MODULE", "ASSEMBLY",
                  "SUBASSEMBLY", "COMPONENT", "PART"]

ADJECTIVES = ["Precision", "Vacuum", "Ceramic", "Quartz", "Anodized", "RF",
              "Cryo", "Optical", "Pneumatic", "Servo", "Thermal", "Coated",
              "Dual-Stage", "High-Purity", "Shielded", "Calibrated"]
NOUNS = ["Valve", "Manifold", "Bracket", "Housing", "Sensor", "Harness",
         "Actuator", "Bellows", "Flange", "Seal", "Stage", "Mirror", "Lens",
         "Chamber Liner", "Feedthrough", "Controller", "Gasket", "Nozzle",
         "Baffle", "Heater", "Coupler", "Regulator", "Fitting", "Shutter"]

# The 15 "critical components" whose revision / supplier batch is tracked
# per wafer lot (as-built genealogy).  Names chosen from a real fab BOM.
CRITICAL_COMPONENTS = [
    ("CC-01", "DUV Photoresist (193nm)"),
    ("CC-02", "Electrostatic Chuck"),
    ("CC-03", "RF Generator Module"),
    ("CC-04", "Slit Valve Assembly"),
    ("CC-05", "Quartz Focus Ring"),
    ("CC-06", "Gas Distribution Showerhead"),
    ("CC-07", "Perfluoro O-Ring Seal Kit"),
    ("CC-08", "Pellicle Frame"),
    ("CC-09", "CMP Polishing Pad"),
    ("CC-10", "Slurry Feed Unit"),
    ("CC-11", "Silicon Edge Ring"),
    ("CC-12", "Faraday Shield"),
    ("CC-13", "Wafer Lift Pin Set"),
    ("CC-14", "Turbomolecular Pump"),
    ("CC-15", "Mass Flow Controller"),
]

SUPPLIERS = ["ApexChem", "Kyodo Materials", "VacTech GmbH", "Siltron Precision",
             "NovaSeal", "Fujikura Advanced", "Cryogenix", "Elemental Coatings",
             "Pacific Quartz", "MicroFlow Systems", "Titan Vacuum", "HelioOptics"]

PROCESS_PARAMS = {
    "litho_exposure_dose_mj":   (25.0, 0.60),
    "litho_focus_offset_nm":    (0.0, 4.0),
    "etch_chamber_temp_c":      (65.0, 1.10),
    "etch_rf_power_w":          (1500.0, 22.0),
    "etch_pressure_mtorr":      (80.0, 2.5),
    "dep_film_thickness_a":     (2000.0, 18.0),
    "cmp_downforce_psi":        (4.2, 0.15),
    "cmp_slurry_flow_mlmin":    (180.0, 6.0),
    "implant_dose_e15":         (3.0, 0.08),
    "anneal_peak_temp_c":       (1015.0, 5.0),
}

# ----- injected root causes (ground truth) ---------------------------------
BAD_SUPPLIER_LOT = "SL-APX-4471"          # a photoresist batch (CC-01)
BAD_SUPPLIER_LOT_EFFECT = -8.0
BAD_REVISION_COMPONENT = "CC-02"          # electrostatic chuck
BAD_REVISION = "C"
BAD_REVISION_EFFECT = -4.0
DRIFT_PARAM = "etch_chamber_temp_c"
DRIFT_EFFECT = -5.0

START_DATE = date(2025, 1, 1)
END_DATE = date(2026, 6, 30)


# ---------------------------------------------------------------------------
# BOM structure
# ---------------------------------------------------------------------------
def build_bom(target_nodes: int, max_depth: int = 6, rng=None):
    """Breadth-first grow a BOM tree to ``target_nodes``, then add cross-links
    (shared components used by several parents) so the result is a true DAG,
    exactly like production SAP/Teamcenter structures."""
    rng = rng or np.random.default_rng(SEED)
    nodes = {PRODUCT_ID: {"node_id": PRODUCT_ID, "part_number": PRODUCT_ID,
                          "description": PRODUCT_DESC, "level_hint": 0,
                          "category": "PRODUCT"}}
    edges = []
    frontier = deque([(PRODUCT_ID, 0)])
    counter = 1

    while frontier and len(nodes) < target_nodes:
        parent, lvl = frontier.popleft()
        if lvl >= max_depth:
            continue
        for seq in range(int(rng.integers(3, 9))):
            if len(nodes) >= target_nodes:
                break
            nid = f"PN-{100000 + counter}"
            counter += 1
            child_lvl = lvl + 1
            desc = (f"{random.choice(ADJECTIVES)} "
                    f"{random.choice(NOUNS)} {nid[-4:]}")
            nodes[nid] = {"node_id": nid, "part_number": nid,
                          "description": desc, "level_hint": child_lvl,
                          "category": LEVEL_CATEGORY[min(child_lvl, 6)]}
            edges.append({"parent_id": parent, "child_id": nid,
                          "qty_per": int(rng.integers(1, 7)),
                          "seq": (seq + 1) * 10})
            frontier.append((nid, child_lvl))

    # cross-links: a deeper node re-used under an additional shallower parent.
    # Linking strictly downward in level_hint guarantees the graph stays acyclic.
    node_list = list(nodes.values())
    existing = {(e["parent_id"], e["child_id"]) for e in edges}
    added = 0
    while added < max(50, target_nodes // 55):
        p = random.choice(node_list)
        c = random.choice(node_list)
        if (p["level_hint"] < c["level_hint"]
                and (p["node_id"], c["node_id"]) not in existing
                and p["node_id"] != PRODUCT_ID):
            edges.append({"parent_id": p["node_id"], "child_id": c["node_id"],
                          "qty_per": int(rng.integers(1, 5)), "seq": 990})
            existing.add((p["node_id"], c["node_id"]))
            added += 1

    # promote 15 nodes (level >= 3) to the named critical components
    candidates = [n for n in node_list if n["level_hint"] >= 3]
    chosen = random.sample(candidates, len(CRITICAL_COMPONENTS))
    crit_map = {}
    for (cc_id, cc_name), node in zip(CRITICAL_COMPONENTS, chosen):
        node["description"] = cc_name
        node["critical_component_id"] = cc_id
        crit_map[cc_id] = {"node_id": node["node_id"], "name": cc_name}

    nodes_df = pd.DataFrame(node_list)
    nodes_df["critical_component_id"] = nodes_df.get("critical_component_id")
    edges_df = pd.DataFrame(edges)
    return nodes_df, edges_df, crit_map


# ---------------------------------------------------------------------------
# Revisions, suppliers, supplier lots
# ---------------------------------------------------------------------------
def build_revisions(crit_map, rng):
    """Every critical component starts at rev A; some get B; CC-02 gets the
    (bad) rev C effective 2026-03-01."""
    rows = []
    for cc_id, info in crit_map.items():
        rows.append({"component_id": cc_id, "node_id": info["node_id"],
                     "component_name": info["name"], "revision": "A",
                     "effective_date": START_DATE.isoformat(),
                     "ecn": f"ECN-{rng.integers(10000, 99999)}",
                     "change_summary": "Initial release"})
        if rng.random() < 0.55 or cc_id == BAD_REVISION_COMPONENT:
            eff = START_DATE + timedelta(days=int(rng.integers(120, 330)))
            rows.append({"component_id": cc_id, "node_id": info["node_id"],
                         "component_name": info["name"], "revision": "B",
                         "effective_date": eff.isoformat(),
                         "ecn": f"ECN-{rng.integers(10000, 99999)}",
                         "change_summary": "Tolerance / material update"})
    # the injected bad revision
    rows.append({"component_id": BAD_REVISION_COMPONENT,
                 "node_id": crit_map[BAD_REVISION_COMPONENT]["node_id"],
                 "component_name": crit_map[BAD_REVISION_COMPONENT]["name"],
                 "revision": BAD_REVISION,
                 "effective_date": date(2026, 3, 1).isoformat(),
                 "ecn": "ECN-77410",
                 "change_summary": "Cost-down: alternate ceramic binder"})
    return pd.DataFrame(rows)


def build_supplier_lots(crit_map, rng):
    """One incoming batch per critical component roughly every 6 weeks."""
    rows = []
    comp_suppliers = {}
    for cc_id, info in crit_map.items():
        n_sup = int(rng.integers(1, 3))
        comp_suppliers[cc_id] = random.sample(SUPPLIERS, n_sup)

    seq = 4400
    for cc_id, info in crit_map.items():
        d = START_DATE - timedelta(days=30)
        while d < END_DATE:
            supplier = random.choice(comp_suppliers[cc_id])
            code = supplier[:3].upper().replace(" ", "")
            lot_id = f"SL-{code}-{seq}"
            seq += 1
            rows.append({"supplier_lot_id": lot_id, "component_id": cc_id,
                         "component_name": info["name"],
                         "supplier_id": supplier,
                         "received_date": d.isoformat(),
                         "qty_received": int(rng.integers(500, 5000)),
                         "coa_passed": True})
            d += timedelta(days=int(rng.integers(35, 55)))

    df = pd.DataFrame(rows)
    # force the injected bad batch: rename one mid-2026 photoresist (CC-01)
    # lot from ApexChem so it is consumed by a compact window of wafer lots
    cc01 = df[(df.component_id == "CC-01")].sort_values("received_date")
    target_idx = cc01[cc01.received_date.between("2026-03-15", "2026-05-01")]
    if target_idx.empty:
        target_idx = cc01.tail(3)
    idx = target_idx.index[0]
    df.loc[idx, "supplier_lot_id"] = BAD_SUPPLIER_LOT
    df.loc[idx, "supplier_id"] = "ApexChem"
    return df


# ---------------------------------------------------------------------------
# Wafer lots, as-built genealogy, process telemetry, yield
# ---------------------------------------------------------------------------
def effective_revision(revs: pd.DataFrame, cc_id: str, on: date) -> str:
    r = revs[(revs.component_id == cc_id)
             & (revs.effective_date <= on.isoformat())]
    if r.empty:
        return "A"
    return r.sort_values("effective_date").iloc[-1]["revision"]


def pick_supplier_lot(slots: pd.DataFrame, cc_id: str, on: date, rng) -> pd.Series:
    """Fab consumes the most recently received batch (with a little overlap)."""
    avail = slots[(slots.component_id == cc_id)
                  & (slots.received_date <= on.isoformat())]
    avail = avail.sort_values("received_date").tail(2)
    if avail.empty:
        avail = slots[slots.component_id == cc_id].head(1)
    return avail.iloc[int(rng.integers(0, len(avail)))]


def build_production(n_lots, revs, slots, rng):
    span = (END_DATE - START_DATE).days
    lots, config, params = [], [], []
    drift_lots = set(rng.choice(n_lots, size=max(8, n_lots // 15),
                                replace=False).tolist())

    for i in range(n_lots):
        lot_id = f"WL-{2025 + (i * span // n_lots) // 365}-{i + 1:04d}"
        start = START_DATE + timedelta(days=int(rng.integers(0, span)))
        y = 92.0 + float(rng.normal(0, 1.2))

        # --- as-built genealogy for the 15 critical components
        for cc_id, cc_name in CRITICAL_COMPONENTS:
            rev = effective_revision(revs, cc_id, start)
            sl = pick_supplier_lot(slots, cc_id, start, rng)
            config.append({"wafer_lot_id": lot_id, "component_id": cc_id,
                           "component_name": cc_name, "revision": rev,
                           "supplier_id": sl["supplier_id"],
                           "supplier_lot_id": sl["supplier_lot_id"]})
            if sl["supplier_lot_id"] == BAD_SUPPLIER_LOT:
                y += BAD_SUPPLIER_LOT_EFFECT + float(rng.normal(0, 0.8))
            if cc_id == BAD_REVISION_COMPONENT and rev == BAD_REVISION:
                y += BAD_REVISION_EFFECT + float(rng.normal(0, 0.5))

        # --- process telemetry
        row = {"wafer_lot_id": lot_id}
        for p, (mu, sd) in PROCESS_PARAMS.items():
            row[p] = float(rng.normal(mu, sd))
        if i in drift_lots:
            mu, sd = PROCESS_PARAMS[DRIFT_PARAM]
            row[DRIFT_PARAM] = float(rng.normal(mu + 3.2 * sd, 0.6 * sd))
            y += DRIFT_EFFECT + float(rng.normal(0, 0.7))
        params.append(row)

        # small genuine process sensitivities (keeps the RF honest)
        y -= 0.35 * abs(row["litho_focus_offset_nm"]) / 4.0
        y -= 0.25 * abs(row["cmp_downforce_psi"] - 4.2) / 0.15

        y = float(np.clip(y, 40.0, 99.5))
        wafers, dpw = 25, 1200
        good = int(round(wafers * dpw * y / 100.0))
        lots.append({"wafer_lot_id": lot_id, "product_id": PRODUCT_ID,
                     "fab_start_date": start.isoformat(),
                     "fab_finish_date": (start + timedelta(days=3)).isoformat(),
                     "wafers": wafers, "die_per_wafer": dpw,
                     "yield_pct": round(y, 2),
                     "good_die": good,
                     "defect_die": wafers * dpw - good})

    drift_lot_ids = [lots[i]["wafer_lot_id"] for i in sorted(drift_lots)]
    return (pd.DataFrame(lots), pd.DataFrame(config),
            pd.DataFrame(params), drift_lot_ids)


# ---------------------------------------------------------------------------
def main(target_nodes: int = 8000, n_lots: int = 600) -> None:
    rng = np.random.default_rng(SEED)
    random.seed(SEED)
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Building {target_nodes:,}-node BOM ...")
    nodes, edges, crit_map = build_bom(target_nodes, rng=rng)
    revs = build_revisions(crit_map, rng)
    slots = build_supplier_lots(crit_map, rng)
    print(f"Simulating {n_lots} wafer lots ...")
    lots, config, params, drift_ids = build_production(n_lots, revs, slots, rng)

    out = {
        "bom_nodes.csv": nodes, "bom_edges.csv": edges,
        "component_revisions.csv": revs, "supplier_lots.csv": slots,
        "wafer_lots.csv": lots, "wafer_lot_config.csv": config,
        "process_parameters.csv": params,
    }
    for name, df in out.items():
        df.to_csv(RAW_DIR / name, index=False)
        print(f"  wrote {name:28s} {len(df):>7,} rows")

    truth = {
        "bad_supplier_lot": {"supplier_lot_id": BAD_SUPPLIER_LOT,
                             "component_id": "CC-01",
                             "effect_pp": BAD_SUPPLIER_LOT_EFFECT},
        "bad_revision": {"component_id": BAD_REVISION_COMPONENT,
                         "revision": BAD_REVISION,
                         "effect_pp": BAD_REVISION_EFFECT},
        "process_drift": {"parameter": DRIFT_PARAM,
                          "effect_pp": DRIFT_EFFECT,
                          "wafer_lots": drift_ids},
    }
    (RAW_DIR / "ground_truth.json").write_text(json.dumps(truth, indent=2))
    print("  wrote ground_truth.json (validation only - the pipeline never reads it)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--nodes", type=int, default=8000)
    ap.add_argument("--lots", type=int, default=600)
    a = ap.parse_args()
    main(a.nodes, a.lots)
