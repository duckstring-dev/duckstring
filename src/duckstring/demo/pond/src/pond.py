from duckstring import ripple


@ripple
def clean(pond):
    raw = pond.read_table("inlet.daily")
    pond.write_table("clean", raw)
