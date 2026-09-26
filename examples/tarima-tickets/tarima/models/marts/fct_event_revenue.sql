select
    event_id,
    event_name,
    city,
    starts_at,
    count(*) filter (where status = 'paid') as tickets_sold,
    sum(total_eur) filter (where status = 'paid') as revenue_eur
from {{ ref('fct_orders') }}
group by 1, 2, 3, 4
