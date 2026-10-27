"""customer_value: each customer's revenue, purchases and last purchase date (chain_sql.AGGREGATES)."""

from duckstring import agg, ripple


@ripple
def aggregate(pond):
    (
        pond.trickle("sales_enriched.enriched")
        .filter("s0.ss_customer_sk IS NOT NULL")
        .aggregate(
            by=["ss_customer_sk", "ca_state"],
            revenue=agg.sum("net_revenue"),
            purchases=agg.count(),
            last_purchase=agg.max("d_date"),
        )
        .merge("customer_value")
    )
