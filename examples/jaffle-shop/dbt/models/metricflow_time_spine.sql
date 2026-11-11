{{ config(materialized='table') }}

select cast(date_day as date) as date_day
from (
    {{ dbt.date_spine('day', "cast('2024-01-01' as date)", "cast('2026-01-01' as date)") }}
) as spine
