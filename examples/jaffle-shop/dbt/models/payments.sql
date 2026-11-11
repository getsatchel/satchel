select
    payment_id,
    order_id,
    payment_method,
    status,
    amount,
    created_at
from {{ source('raw', 'payments') }}
