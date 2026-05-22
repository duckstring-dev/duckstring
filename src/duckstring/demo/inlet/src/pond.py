from duckstring import ripple


@ripple
def daily(pond):
    # Generate synthetic data — replace with your actual external data source.
    data = pond.con.sql(
        "SELECT range AS id, 'value_' || range::VARCHAR AS label FROM range(10)"
    )
    pond.write_table("daily", data)
