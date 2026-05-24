import time

from duckstring import ripple

_BASE_CATALOG = [
    (1, "Laptop Pro", "Electronics", 1299.00),
    (2, "Wireless Earbuds", "Electronics", 89.99),
    (3, "Running Shoes", "Clothing", 79.99),
    (4, "Winter Jacket", "Clothing", 149.99),
    (5, "Espresso Beans", "Food", 14.99),
    (6, "Olive Oil", "Food", 11.49),
    (7, "Desk Lamp", "Home", 34.99),
    (8, "Throw Pillow", "Home", 24.99),
    (9, "Sci-Fi Novel", "Books", 16.99),
    (10, "Cookbook", "Books", 22.99),
]

_EXTENDED_CATALOG = [
    (11, "Smart Watch", "Electronics", 249.99),
    (12, "Yoga Mat", "Clothing", 39.99),
    (13, "Cold Brew Kit", "Food", 29.99),
    (14, "Candle Set", "Home", 19.99),
    (15, "Travel Guide", "Books", 18.99),
    (16, "USB Hub", "Electronics", 44.99),
    (17, "Hiking Boots", "Clothing", 119.99),
    (18, "Hot Sauce Pack", "Food", 24.99),
    (19, "Picture Frame", "Home", 14.99),
    (20, "Journal", "Books", 12.99),
]


def _to_values(rows):
    return ", ".join(f"({r[0]}, '{r[1]}', '{r[2]}', {r[3]:.2f})" for r in rows)


@ripple
def ingest(pond):
    time.sleep(2)
    try:
        existing = pond.con.sql('SELECT * FROM "products"."product"')
        current_max = pond.con.execute(
            'SELECT MAX(id) FROM "products"."product"'
        ).fetchone()[0]
    except Exception:
        existing = None
        current_max = 0

    if existing is None:
        vals = _to_values(_BASE_CATALOG)
        all_data = pond.con.sql(
            f"SELECT * FROM (VALUES {vals}) t(id, name, category, unit_price)"
        )
        pond.write_table("product", all_data)
        return

    next_batch = [r for r in _EXTENDED_CATALOG if r[0] > current_max][:2]
    if not next_batch:
        pond.write_table("product", existing)
        return

    vals = _to_values(next_batch)
    new_rows = pond.con.sql(  # noqa: F841
        f"SELECT * FROM (VALUES {vals}) t(id, name, category, unit_price)"
    )
    combined = pond.con.sql("SELECT * FROM existing UNION ALL SELECT * FROM new_rows")
    pond.write_table("product", combined)
