"""store_quarter: revenue, returns and the mean amount paid per store per quarter (chain_sql.AGGREGATES)."""

from duckstring import agg, ripple


@ripple
def aggregate(pond):
    (
        pond.trickle("sales_enriched.enriched")
        .aggregate(
            by=["s_store_name", "s_state", "d_year", "d_qoy"],
            revenue=agg.sum("net_revenue"),
            returns=agg.sum("return_amt"),
            avg_paid=agg.mean("ss_net_paid"),
        )
        .merge("store_quarter")
    )
