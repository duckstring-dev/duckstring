from duckstring import ripple


@ripple
def daily(pond):
    df = pond.read_table("pond.clean")
    pond.write_table("daily", df)
