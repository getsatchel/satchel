---
name: Revenue reporting
description: How to calculate and present revenue, monthly revenue and revenue trends at Jaffle Shop.
tags: [revenue, finance, reporting]
---

# Revenue reporting

## Definition

Revenue is the sum of `analytics.orders.amount` for orders whose `status` is not `returned`
and not `return_pending`. Amounts are in AUD cents, so divide by 100 for dollars.

Revenue is attributed to the month of `order_date` (the day the order was placed in the
Australia/Sydney timezone), not the day it shipped or was paid.

## Monthly and period reports

Group by `date_trunc('month', order_date)`. "Last month" means the previous complete
calendar month, never month-to-date. For year-on-year comparisons, line up complete months
only, and mention if the current month is partial.

When a user asks for revenue "by month", return one row per month with a zero row for months
that had no orders, so that charts do not skip months.

## Revenue versus net revenue

Revenue ignores refunds issued as money back on completed orders. If the user wants cash
actually kept, use net revenue (revenue minus refunds) from the glossary. See the refunds
guide for how refunds are recorded.

## Common mistakes

- Summing `amount` without filtering status includes returned orders and overstates revenue
  by roughly 4 to 6 percent.
- Reporting cents as dollars. Always divide by 100.
- Using `analytics.payments` to compute revenue; it double counts orders paid in two
  instalments.
