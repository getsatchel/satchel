---
applies_to: [analytics.orders, analytics.payments]
valid_to: 2023-01-31
---

Refunds before February 2023 were issued by hand and never written to `analytics.payments`.
For any period up to 2023-01-31, refund totals and net revenue are understated. Orders
returned in that period do have `status = 'returned'`, so revenue itself is correct.
