---
name: Refunds and returns
description: How returns and refunds are recorded, and which table to use for each question.
tags: [refunds, returns, payments]
---

# Refunds and returns

## Two different things

A **return** changes the order. The order's `status` moves to `return_pending` when the
customer ships the jaffle maker back, and to `returned` once the warehouse confirms receipt.
Returned orders are excluded from revenue.

A **refund** is money paid back. It appears as a row in `analytics.payments` with
`status = 'refunded'`. One returned order normally has one refund row, but goodwill refunds
exist with no return.

## Which table answers which question

| Question | Use |
|---|---|
| How many orders were returned? | `analytics.orders` where `status = 'returned'` |
| How much money did we refund? | `analytics.payments` where `status = 'refunded'` |
| Refund rate | refunded amount divided by revenue for the same month |

## Timing

Refunds are dated by `created_at` on the payment, which can be weeks after `order_date`.
When reporting a refund rate for a month, use refunds created in that month against revenue
of the same month; say that the two populations differ.
