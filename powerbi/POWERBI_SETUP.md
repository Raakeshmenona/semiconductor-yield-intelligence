# Power BI Report — Setup Guide

Three-page report over the Snowflake `YIELD_INTEL` database. Import the
measures from [dax_measures.dax](dax_measures.dax) (Tabular Editor:
*Model > Import script*, or paste per-measure).

## 1. Connect

1. **Get Data → Snowflake**: server `<account>.snowflakecomputing.com`,
   warehouse `COMPUTE_WH`. Import mode for `BOM_EXPLODED` (fast slicer),
   DirectQuery is fine for the facts.
2. Load: `MARTS.FCT_YIELD_EXCURSION`, `MARTS.DIM_SUPPLIER_LOT`,
   `STAGING.STG_LOT_GENEALOGY`, `ANALYTICS.BOM_EXPLODED`,
   `ANALYTICS.ROOT_CAUSE_SHORTLIST`.
3. Create `DimDate` (*Modeling → New table*):
   `DimDate = CALENDAR(DATE(2025,1,1), TODAY())` and **Mark as date table**.

## 2. Model relationships

| From | To | Cardinality |
|---|---|---|
| FctYieldExcursion[wafer_lot_id] | LotGenealogy[wafer_lot_id] | 1 → * |
| LotGenealogy[supplier_lot_id] | DimSupplierLot[supplier_lot_id] | * → 1 |
| FctYieldExcursion[fab_start_date] | DimDate[Date] | * → 1 |
| LotGenealogy[component_id] | BomExploded[critical_component_id] | * → * (single direction) |

## 3. Pages

### Page 1 — "Fab Yield Overview" (executive)
- KPI cards: `Yield %`, `Yield Delta pp`, `Excursion Lots`,
  `Yield Loss Cost $`, `Cost Avoidance per Run $`.
- Line chart: `Yield %` and `Yield 30d MA %` by `DimDate[Date]`;
  add an anomaly marker series on `is_excursion`.
- **BOM hierarchy slicer**: Hierarchy slicer visual (or decomposition tree)
  on `BOM Level 1..5` calculated columns — clicking any assembly filters
  every visual to lots whose genealogy touches that subtree.

### Page 2 — "Supplier Pareto"
- Pareto: column `Supplier Defect Cost $` by `DimSupplierLot[supplier_lot_id]`
  (sorted by `defect_contribution_score`), line `Supplier Defect Cost
  Cumulative %` on the secondary axis.
- Table: rank, batch, component, supplier, `yield_delta_pp`, `p_value`,
  `est_cost_impact_usd`; conditional format `is_suspect_batch` red.

### Page 3 — "Excursion Drill-Through" (target page)
- **Drill-through field**: `FctYieldExcursion[wafer_lot_id]` — right-click any
  lot anywhere → *Drill through → Excursion Detail*.
- Cards: `Selected Lot Yield %`, `Root Cause Verdict`,
  `Selected Lot Suspect Components`.
- Matrix: the lot's full genealogy (component → revision → supplier batch)
  with `defect_contribution_score` as data bars.
- Decomposition tree: `yield_loss_cost_usd` by BOM Level 1→5 for the
  affected components.

## 4. Refresh

The Airflow task `refresh_powerbi_dataset` POSTs to the Power BI REST API
after each dbt build; alternatively schedule dataset refresh in the service
right after the 02:00 pipeline window.
