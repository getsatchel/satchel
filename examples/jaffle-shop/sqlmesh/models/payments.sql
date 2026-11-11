MODEL (
  name analytics.payments,
  kind FULL,
  description 'One row per payment attempt or refund against an order (AUD cents).',
  grain payment_id,
  references (order_id),
  columns (
    payment_id INT,
    order_id INT,
    payment_method TEXT,
    status TEXT,
    amount INT,
    created_at TIMESTAMP
  ),
  column_descriptions (
    payment_id = 'Primary key of the payment.',
    order_id = 'Order the payment belongs to.',
    payment_method = 'How the customer paid.',
    status = 'Payment outcome.',
    amount = 'Payment amount in AUD cents.',
    created_at = 'When the payment was created (UTC).'
  ),
  audits (
    not_null(columns := (payment_id, order_id)),
    unique_values(columns := (payment_id)),
    accepted_values(column := payment_method, is_in := ('credit_card', 'bank_transfer', 'gift_card', 'coupon')),
    accepted_values(column := status, is_in := ('succeeded', 'failed', 'refunded'))
  )
);

SELECT
  payment_id,
  order_id,
  payment_method,
  status,
  amount,
  created_at
FROM raw.payments
