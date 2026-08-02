-- Mart: yield excursion fact — one row per wafer lot with excursion flag,
-- rolling baseline, and the dollar impact of the excursion. Grain matches
-- the Power BI drill-through page ("Excursion Detail").

with lots as (

    select * from {{ ref('stg_wafer_lots') }}

),

baseline as (

    -- 60-day trailing robust baseline per product
    select
        l.wafer_lot_id,
        median(b.yield_pct)             as baseline_yield,
        stddev(b.yield_pct)             as baseline_sigma
    from lots l
    join lots b
      on  b.product_id = l.product_id
      and b.fab_start_date between dateadd(day, -60, l.fab_start_date)
                               and l.fab_start_date
    group by l.wafer_lot_id

),

anomalies as (

    select
        wafer_lot_id,
        anomaly_score,
        is_anomaly,
        top_deviating_param
    from {{ source('analytics', 'PROCESS_ANOMALIES') }}

)

select
    l.wafer_lot_id,
    l.product_id,
    l.fab_start_date,
    l.fab_finish_date,
    l.wafers,
    l.die_per_wafer,
    l.yield_pct,
    l.good_die,
    l.defect_die,
    l.defect_ppm,
    b.baseline_yield,
    b.baseline_sigma,
    l.yield_pct < b.baseline_yield
                  - {{ var('excursion_sigma') }} * b.baseline_sigma
                                        as is_excursion,
    greatest(b.baseline_yield - l.yield_pct, 0) / 100.0
        * l.wafers * l.die_per_wafer    as lost_die_vs_baseline,
    greatest(b.baseline_yield - l.yield_pct, 0) / 100.0
        * l.wafers * l.die_per_wafer
        * {{ var('cost_per_good_die_usd') }}
                                        as yield_loss_cost_usd,
    a.anomaly_score                     as process_anomaly_score,
    coalesce(a.is_anomaly, false)       as has_process_anomaly,
    a.top_deviating_param
from lots l
left join baseline b  on b.wafer_lot_id = l.wafer_lot_id
left join anomalies a on a.wafer_lot_id = l.wafer_lot_id
