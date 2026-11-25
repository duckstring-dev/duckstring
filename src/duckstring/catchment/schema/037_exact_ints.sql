-- Whether a major line publishes a 128-bit integer column (an integer SUM's HUGEINT) as BIGINT, exactly, rather
-- than as DOUBLE, which reads 9 as 9.0 and rounds above 2^53 (dataplane._out). Lines first deployed from here on
-- get it (routes/deploy.py sets it when it creates the `pond` row); existing lines keep DOUBLE, which their Sinks'
-- schema contracts record. A major bump moves a Pond onto exact integers.
ALTER TABLE pond ADD COLUMN exact_ints INTEGER NOT NULL DEFAULT 0;
