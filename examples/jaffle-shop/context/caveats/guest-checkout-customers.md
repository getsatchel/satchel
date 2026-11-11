---
applies_to: [analytics.customers]
valid_to: 2023-05-31
---

Before June 2023 guest checkout created a new customer row for every order, even for the
same email address. Customer counts, new customer counts and repeat rates for periods before
2023-06 are overstated and repeat rates are understated. Deduplicate by `email` for those
periods.
