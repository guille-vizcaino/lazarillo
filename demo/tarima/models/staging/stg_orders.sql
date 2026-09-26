select
    order_id,
    event_id,
    lower(buyer_email) as buyer_email,
    ordered_at,
    price_eur,
    fees_eur,
    price_eur + fees_eur as total_eur,
    status,
    updated_at
from {{ source('landing', 'orders') }}
