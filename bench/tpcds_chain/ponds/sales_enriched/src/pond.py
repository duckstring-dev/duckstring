"""sales_enriched: each sale joined to its date, item, store, customer, address and return.

The same query as chain_sql.ENRICHED, written with the Trickle builder so each run computes only the rows
its Sources' changes reach: a new or corrected sale, a reclassified item's sales, a moved customer's sales,
a newly returned sale.
"""

from duckstring import ripple

ZERO = "CAST(0 AS DECIMAL(7,2))"


@ripple
def enrich(pond):
    (
        pond.trickle("sales_feed.store_sales").alias("s")
        .join(pond.trickle("dimensions.date_dim").alias("d"), on={"ss_sold_date_sk": "d_date_sk"})
        .join(pond.trickle("dimensions.item").alias("i"), on={"ss_item_sk": "i_item_sk"})
        .join(pond.trickle("dimensions.store").alias("st"), on={"ss_store_sk": "s_store_sk"})
        .join(pond.trickle("dimensions.customer").alias("c"), on={"ss_customer_sk": "c_customer_sk"}, how="left")
        .join(pond.trickle("dimensions.customer_address").alias("ca"), on={"c_current_addr_sk": "ca_address_sk"},
              how="left")
        .join(pond.trickle("sales_feed.store_returns").alias("r"),
              on={"ss_ticket_number": "sr_ticket_number", "ss_item_sk": "sr_item_sk"}, how="left")
        .select(
            "s.ss_ticket_number, s.ss_item_sk, s.ss_customer_sk, "
            "d.d_date, d.d_year, d.d_moy, d.d_qoy, "
            "i.i_category, i.i_class, i.i_brand, "
            "st.s_store_name, st.s_state, "
            "ca.ca_state, "
            "s.ss_quantity, s.ss_net_paid, s.ss_net_profit, "
            f"coalesce(r.sr_return_amt, {ZERO}) AS return_amt, "
            f"s.ss_net_paid - coalesce(r.sr_return_amt, {ZERO}) AS net_revenue"
        )
        .merge("enriched", pk=["ss_ticket_number", "ss_item_sk"])
    )
