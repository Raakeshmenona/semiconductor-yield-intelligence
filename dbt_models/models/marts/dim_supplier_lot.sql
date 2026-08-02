-- Mart: supplier-lot dimension enriched with defect-contribution analytics.
-- Feeds the Power BI "Supplier Pareto" page and the hierarchy slicer's
-- supplier drill path (Supplier -> Component -> Batch).

with supplier_lots as (

    select
        supplier_lot_id,
        component_id,
        component_name,
        supplier_id,
        cast(received_date as date)     as received_date,
        cast(qty_received as integer)   as qty_received,
        coa_passed
    from {{ source('raw', 'SUPPLIER_LOTS') }}

),

usage as (

    select
        supplier_lot_id,
        count(distinct wafer_lot_id)    as wafer_lots_using,
        min(wafer_lot_id)               as first_wafer_lot,
        max(wafer_lot_id)               as last_wafer_lot
    from {{ ref('stg_lot_genealogy') }}
    group by supplier_lot_id

),

ranking as (

    select
        supplier_lot_id,
        yield_delta_pp,
        shrunk_delta_pp,
        p_value,
        est_cost_impact_usd,
        defect_contribution_score,
        rank                            as defect_rank
    from {{ source('analytics', 'SUPPLIER_LOT_RANKING') }}

)

select
    s.supplier_lot_id,
    s.component_id,
    s.component_name,
    s.supplier_id,
    s.received_date,
    s.qty_received,
    s.coa_passed,
    coalesce(u.wafer_lots_using, 0)     as wafer_lots_using,
    u.first_wafer_lot,
    u.last_wafer_lot,
    r.yield_delta_pp,
    r.shrunk_delta_pp,
    r.p_value,
    r.est_cost_impact_usd,
    r.defect_contribution_score,
    r.defect_rank,
    r.defect_rank <= 5
        and r.yield_delta_pp < -1.0     as is_suspect_batch
from supplier_lots s
left join usage   u on u.supplier_lot_id = s.supplier_lot_id
left join ranking r on r.supplier_lot_id = s.supplier_lot_id
