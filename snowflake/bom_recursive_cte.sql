-------------------------------------------------------------------------------
-- Snowflake-native multi-level BOM flattening (recursive CTE)
-------------------------------------------------------------------------------
-- Mirrors bom/bom_graph_builder.py inside the warehouse so BI tools and dbt
-- can explode the BOM without any Python round-trip.
--
-- Source table (loaded by snowflake/yield_mart_loader.py):
--   RAW.BOM_EDGES(parent_id, child_id, qty_per, seq)
--   RAW.BOM_NODES(node_id, part_number, description, category)
--
-- KPI: 8,000-node BOM flattens in seconds on an XS warehouse.
-------------------------------------------------------------------------------

USE DATABASE YIELD_INTEL;
USE SCHEMA ANALYTICS;

CREATE OR REPLACE TABLE BOM_EXPLODED AS
WITH RECURSIVE bom_explosion AS (

    -- anchor: the finished product(s) = nodes that never appear as a child
    SELECT
        n.node_id                                   AS root_id,
        n.node_id                                   AS component_id,
        CAST(NULL AS VARCHAR)                       AS parent_id,
        0                                           AS bom_level,
        1.0::FLOAT                                  AS qty_per,
        1.0::FLOAT                                  AS extended_qty,
        n.node_id::VARCHAR                          AS path
    FROM RAW.BOM_NODES n
    WHERE NOT EXISTS (
        SELECT 1 FROM RAW.BOM_EDGES e WHERE e.child_id = n.node_id
    )

    UNION ALL

    -- recursive member: walk one level down, multiplying quantities
    SELECT
        x.root_id,
        e.child_id                                  AS component_id,
        e.parent_id,
        x.bom_level + 1                             AS bom_level,
        e.qty_per::FLOAT                            AS qty_per,
        x.extended_qty * e.qty_per                  AS extended_qty,
        x.path || ' > ' || e.child_id               AS path
    FROM bom_explosion x
    JOIN RAW.BOM_EDGES e
      ON e.parent_id = x.component_id
    -- cycle guard: never revisit a node already on this path
    WHERE NOT CONTAINS(x.path, e.child_id)
      AND x.bom_level < 20
)
SELECT
    x.root_id,
    x.component_id,
    x.parent_id,
    x.bom_level,
    x.qty_per,
    x.extended_qty,
    x.path,
    n.description,
    n.category
FROM bom_explosion x
LEFT JOIN RAW.BOM_NODES n
       ON n.node_id = x.component_id;

ALTER TABLE BOM_EXPLODED CLUSTER BY (root_id, bom_level);

-------------------------------------------------------------------------------
-- Where-used (implosion) view: every top-level product a component rolls into,
-- with the cumulative quantity per finished unit.
-------------------------------------------------------------------------------
CREATE OR REPLACE VIEW BOM_WHERE_USED AS
SELECT
    component_id,
    root_id                                         AS used_in_product,
    COUNT(*)                                        AS usage_paths,
    SUM(extended_qty)                               AS qty_per_finished_unit,
    MIN(bom_level)                                  AS shallowest_level,
    MAX(bom_level)                                  AS deepest_level
FROM BOM_EXPLODED
WHERE bom_level > 0
GROUP BY component_id, root_id;

-------------------------------------------------------------------------------
-- Genealogy bridge: wafer lot -> every BOM path of the components it consumed.
-- This is THE join that makes defect correlation "BOM-version-aware":
-- Power BI drills from a yield excursion straight into the exploded hierarchy.
-------------------------------------------------------------------------------
CREATE OR REPLACE VIEW LOT_TO_BOM_PATH AS
SELECT
    c.wafer_lot_id,
    c.component_id      AS critical_component_id,
    c.revision,
    c.supplier_lot_id,
    n.node_id           AS bom_node_id,
    b.path,
    b.bom_level,
    b.extended_qty
FROM RAW.WAFER_LOT_CONFIG c
JOIN RAW.BOM_NODES n
  ON n.critical_component_id = c.component_id
JOIN BOM_EXPLODED b
  ON b.component_id = n.node_id;
