---
name: Payments reconciliation
description: Using the payments table to reconcile with the payment provider and to analyse payment methods.
tags: [payments, finance]
---

# Payments reconciliation

## What the payments table is for

`analytics.payments` has one row per payment attempt event from the payment provider. It is
the right source for payment method mix, failed payment analysis and reconciling to bank
deposits. It is not the source for revenue.

## Payment methods

`payment_method` is one of `credit_card`, `bank_transfer`, `gift_card`, `coupon`. Orders can
be paid by several methods; an order paid half by gift card and half by credit card has two
rows.

## Statuses

Only `succeeded` rows represent money received. `failed` rows are retries that did not go
through and must be excluded from any total. `refunded` rows are money paid back.

## Reconciling to orders

Sum of succeeded payments for an order should equal `orders.amount`. Differences usually
come from coupons (the coupon row carries the discount) or from an order that is still
`placed` and only partly paid.
