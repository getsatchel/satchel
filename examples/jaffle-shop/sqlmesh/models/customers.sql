MODEL (
  name analytics.customers,
  kind FULL,
  owner 'crm-team@jaffle.example',
  tags ['core'],
  description 'One row per registered customer. Guest checkouts (customer_id 0) are not in this table.',
  grain customer_id,
  columns (
    customer_id INT,
    first_name TEXT,
    last_name TEXT,
    email TEXT,
    signup_date DATE,
    first_order_date DATE,
    number_of_orders INT
  ),
  column_descriptions (
    customer_id = 'Primary key of the customer.',
    first_name = 'Customer first name.',
    last_name = 'Customer last name.',
    email = 'Contact email. Personal data.',
    signup_date = 'Date the customer registered.',
    first_order_date = 'Date of the customer''s first order of any status.',
    number_of_orders = 'Count of non-returned orders placed by the customer.'
  ),
  audits (
    not_null(columns := (customer_id)),
    unique_values(columns := (customer_id))
  )
);

SELECT
  customer_id,
  first_name,
  last_name,
  email,
  signup_date,
  first_order_date,
  number_of_orders
FROM raw.customers
