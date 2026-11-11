MODEL (
  name analytics.orders,
  kind FULL,
  owner 'data-team@jaffle.example',
  tags ['core', 'finance'],
  description 'One row per customer order. The source of truth for orders and revenue (AUD cents).',
  grain order_id,
  references (customer_id),
  columns (
    order_id INT,
    customer_id INT,
    order_date DATE,
    status TEXT,
    amount INT
  ),
  column_descriptions (
    order_id = 'Primary key of the order.',
    customer_id = 'Customer who placed the order. 0 is the internal test customer, always exclude it.',
    order_date = 'Date the order was placed in Australia/Sydney time.',
    status = 'Order lifecycle state. returned and return_pending orders are excluded from revenue.',
    amount = 'Order total including GST and shipping, in AUD cents.'
  ),
  audits (
    not_null(columns := (order_id, customer_id, order_date, status)),
    unique_values(columns := (order_id)),
    accepted_values(column := status, is_in := ('placed', 'shipped', 'completed', 'return_pending', 'returned'))
  )
);

SELECT
  order_id,
  customer_id,
  order_date,
  status,
  amount
FROM raw.orders
