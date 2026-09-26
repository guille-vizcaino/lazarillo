select
    event_id,
    artist,
    venue,
    city,
    artist || ' @ ' || venue as event_name,
    starts_at
from {{ source('landing', 'events') }}
