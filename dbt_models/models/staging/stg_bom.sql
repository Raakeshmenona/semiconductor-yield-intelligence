-- Staging: clean, typed BOM structure (nodes + edges in one usage grain).
-- One row per parent/child usage occurrence with part-master attributes
-- resolved on both sides. Downstream marts and the Power BI hierarchy
-- slicer are built from this model.

with edges as (

    select
        parent_id,
        child_id,
        cast(qty_per as float)          as qty_per,
        cast(seq as integer)            as seq
    from {{ source('raw', 'BOM_EDGES') }}

),

nodes as (

    select
        node_id,
        part_number,
        description,
        category,
        critical_component_id
    from {{ source('raw', 'BOM_NODES') }}

)

select
    {{ dbt_utils.generate_surrogate_key(['e.parent_id', 'e.child_id']) }}
                                        as bom_usage_key,
    e.parent_id,
    p.description                       as parent_description,
    p.category                          as parent_category,
    e.child_id,
    c.description                       as child_description,
    c.category                          as child_category,
    c.critical_component_id,
    e.qty_per,
    e.seq
from edges e
left join nodes p on p.node_id = e.parent_id
left join nodes c on c.node_id = e.child_id
