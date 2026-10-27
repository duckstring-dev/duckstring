"""category_month: revenue, units, sales and profit per category per month (chain_sql.AGGREGATES)."""

from duckstring import agg, ripple


@ripple
def aggregate(pond):
    (
        pond.trickle("sales_enriched.enriched")
        .aggregate(
            by=["d_year", "d_moy", "i_category"],
            revenue=agg.sum("net_revenue"),
            units=agg.sum("ss_quantity"),
            sales=agg.count(),
            profit=agg.sum("ss_net_profit"),
        )
        .merge("category_month")
    )
