---
name: Customer analysis
description: Defining active, new and repeat customers, and how to count customers without double counting.
tags: [customers, retention, growth]
---

# Customer analysis

## Counting customers

Always count distinct `customer_id` from `analytics.orders`, not rows from
`analytics.customers`, when the question is about buying behaviour. The customers table
also contains people who signed up and never ordered.

Exclude the test customer with `customer_id = 0`.

## Active, new and repeat

- An **active customer** placed at least one non-returned order in the last 90 days.
- A **new customer** has their first ever order in the period (`first_order_date` on
  `analytics.customers`).
- A **repeat customer** has two or more non-returned orders in total.

## Cohorts and retention

For cohort analysis group customers by the month of `first_order_date`. Retention for month
N is the share of the cohort who placed an order N months later. Do not use `signup_date`
for cohorts; a large share of customers sign up months before their first order.

## Guest checkout

Guest orders are linked to a placeholder customer per email address. See the guest
checkout caveat before reporting customer counts for periods before 2023-06.
