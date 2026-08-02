-- Staging: typed wafer lots with derived defect metrics.

select
    wafer_lot_id,
    product_id,
    cast(fab_start_date as date)        as fab_start_date,
    cast(fab_finish_date as date)       as fab_finish_date,
    cast(wafers as integer)             as wafers,
    cast(die_per_wafer as integer)      as die_per_wafer,
    cast(yield_pct as float)            as yield_pct,
    cast(good_die as integer)           as good_die,
    cast(defect_die as integer)         as defect_die,
    defect_die / nullif(wafers * die_per_wafer, 0) * 1e6
                                        as defect_ppm
from {{ source('raw', 'WAFER_LOTS') }}
