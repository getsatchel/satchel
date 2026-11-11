select
    customer_id,
    first_name,
    last_name,
    email,
    signup_date,
    first_order_date,
    number_of_orders
from {{ source('raw', 'customers') }}
