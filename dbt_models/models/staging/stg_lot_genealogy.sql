-- Staging: as-built genealogy — which component revision and supplier batch
-- each wafer lot actually consumed. The heart of BOM-version-aware
-- correlation: every yield number can be traced to exact revisions.

select
    g.wafer_lot_id,
    g.component_id,
    g.component_name,
    g.revision,
    g.supplier_id,
    g.supplier_lot_id,
    r.ecn,
    cast(r.effective_date as date)      as revision_effective_date
from {{ source('raw', 'WAFER_LOT_CONFIG') }} g
left join {{ source('raw', 'COMPONENT_REVISIONS') }} r
       on  r.component_id = g.component_id
       and r.revision     = g.revision
