MODEL (
  name warehouse.sales.dim_customer,
  kind FULL,
  grain id,
  columns (
    id INT,
    name TEXT NOT NULL,
    price DECIMAL(10, 2),
    created TIMESTAMP WITH TIME ZONE
  ),
  audits (not_null(columns := id), unique_values(columns := id))
);

SELECT id, name, price, created FROM raw.customers
