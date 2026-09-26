{{
    config(
        materialized='incremental',
        unique_key='order_id',
        incremental_strategy='delete+insert'
    )
}}

select
    o.order_id,
    o.event_id,
    e.event_name,
    e.city,
    e.starts_at,
    o.ordered_at,
    o.total_eur,
    o.status,
    o.updated_at
from {{ ref('stg_orders') }} o
join {{ ref('stg_events') }} e using (event_id)

{% if is_incremental() %}
  -- only pick up new orders
  where o.ordered_at > (select max(ordered_at) from {{ this }})
{% endif %}
