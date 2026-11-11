// Drives the pg wire the way Power BI's PostgreSQL connector does, through Npgsql. Each check prints
// "PASS name" or "FAIL name: error", and the exit code is the number of failures.
// Usage: NpgsqlProbe "<connection string>"
using System;
using System.Collections.Generic;
using System.Data;
using Npgsql;

static class Probe
{
    static int failures;

    static void Check(string name, Func<object> body, object expected = null)
    {
        try
        {
            var got = body();
            if (expected != null && !Equals(got, expected))
            {
                failures++;
                Console.WriteLine($"FAIL {name}: expected {Show(expected)}, got {Show(got)}");
                return;
            }
            Console.WriteLine($"PASS {name}");
        }
        catch (Exception e)
        {
            failures++;
            Console.WriteLine($"FAIL {name}: {e.GetType().Name}: {e.Message.Replace('\n', ' ')}");
        }
    }

    // A date: Npgsql 4 reads it as a DateTime, Npgsql 6 and later as a DateOnly.
    static string Day(object v) => ((IFormattable)v).ToString("yyyy-MM-dd", null);

    static string Show(object v) => v == null ? "null" : $"{v} ({v.GetType().Name})";

    static object Scalar(NpgsqlConnection con, string sql, params (string, object)[] ps)
    {
        using var cmd = new NpgsqlCommand(sql, con);
        foreach (var (k, v) in ps) cmd.Parameters.AddWithValue(k, v);
        return cmd.ExecuteScalar();
    }

    static List<object[]> Rows(NpgsqlConnection con, string sql)
    {
        using var cmd = new NpgsqlCommand(sql, con);
        using var r = cmd.ExecuteReader();
        var rows = new List<object[]>();
        while (r.Read())
        {
            var row = new object[r.FieldCount];
            r.GetValues(row);
            rows.Add(row);
        }
        return rows;
    }

    static List<(string, string)> PowerBiSession(string schema, string table) => new List<(string, string)>
    {
        ("character sets", "select character_set_name from INFORMATION_SCHEMA.character_sets"),
        ("tables", @"select TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE
        from INFORMATION_SCHEMA.tables
        where TABLE_SCHEMA not in ('information_schema', 'pg_catalog')
        order by TABLE_SCHEMA, TABLE_NAME"),
        ("columns", $@"select COLUMN_NAME, ORDINAL_POSITION, IS_NULLABLE, case when (data_type like '%unsigned%') then DATA_TYPE || ' unsigned' else DATA_TYPE end as DATA_TYPE
        from INFORMATION_SCHEMA.columns
        where TABLE_SCHEMA = '{schema}' and TABLE_NAME = '{table}'
        order by TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION"),
        ("foreign keys to", $@"select
            pkcol.COLUMN_NAME as PK_COLUMN_NAME,
            fkcol.TABLE_SCHEMA AS FK_TABLE_SCHEMA,
            fkcol.TABLE_NAME AS FK_TABLE_NAME,
            fkcol.COLUMN_NAME as FK_COLUMN_NAME,
            fkcol.ORDINAL_POSITION as ORDINAL,
            fkcon.CONSTRAINT_SCHEMA || '_' || fkcol.TABLE_NAME || '_' || '{table}' || '_' || fkcon.CONSTRAINT_NAME as FK_NAME
        from
            (select distinct constraint_catalog, constraint_schema, unique_constraint_schema, constraint_name, unique_constraint_name
                from INFORMATION_SCHEMA.REFERENTIAL_CONSTRAINTS) fkcon
                inner join
            INFORMATION_SCHEMA.KEY_COLUMN_USAGE fkcol
                on fkcon.CONSTRAINT_SCHEMA = fkcol.CONSTRAINT_SCHEMA
                and fkcon.CONSTRAINT_NAME = fkcol.CONSTRAINT_NAME
                inner join
            INFORMATION_SCHEMA.KEY_COLUMN_USAGE pkcol
                on fkcon.UNIQUE_CONSTRAINT_SCHEMA = pkcol.CONSTRAINT_SCHEMA
                and fkcon.UNIQUE_CONSTRAINT_NAME = pkcol.CONSTRAINT_NAME
        where pkcol.TABLE_SCHEMA = '{schema}' and pkcol.TABLE_NAME = '{table}'
                and pkcol.ORDINAL_POSITION = fkcol.ORDINAL_POSITION
        order by FK_NAME, fkcol.ORDINAL_POSITION"),
        ("foreign keys from", $@"select
            pkcol.TABLE_SCHEMA AS PK_TABLE_SCHEMA,
            pkcol.TABLE_NAME AS PK_TABLE_NAME,
            pkcol.COLUMN_NAME as PK_COLUMN_NAME,
            fkcol.COLUMN_NAME as FK_COLUMN_NAME,
            fkcol.ORDINAL_POSITION as ORDINAL,
            fkcon.CONSTRAINT_SCHEMA || '_' || '{table}' || '_' || pkcol.TABLE_NAME || '_' || fkcon.CONSTRAINT_NAME as FK_NAME
        from
            (select distinct constraint_catalog, constraint_schema, unique_constraint_schema, constraint_name, unique_constraint_name
                from INFORMATION_SCHEMA.REFERENTIAL_CONSTRAINTS) fkcon
                inner join
            INFORMATION_SCHEMA.KEY_COLUMN_USAGE fkcol
                on fkcon.CONSTRAINT_SCHEMA = fkcol.CONSTRAINT_SCHEMA
                and fkcon.CONSTRAINT_NAME = fkcol.CONSTRAINT_NAME
                inner join
            INFORMATION_SCHEMA.KEY_COLUMN_USAGE pkcol
                on fkcon.UNIQUE_CONSTRAINT_SCHEMA = pkcol.CONSTRAINT_SCHEMA
                and fkcon.UNIQUE_CONSTRAINT_NAME = pkcol.CONSTRAINT_NAME
        where fkcol.TABLE_SCHEMA = '{schema}' and fkcol.TABLE_NAME = '{table}'
                and pkcol.ORDINAL_POSITION = fkcol.ORDINAL_POSITION
        order by FK_NAME, fkcol.ORDINAL_POSITION"),
        ("keys", $@"select i.CONSTRAINT_SCHEMA || '_' || i.CONSTRAINT_NAME as INDEX_NAME, ii.COLUMN_NAME, ii.ORDINAL_POSITION, case when i.CONSTRAINT_TYPE = 'PRIMARY KEY' then 'Y' else 'N' end as PRIMARY_KEY
        from INFORMATION_SCHEMA.table_constraints i inner join INFORMATION_SCHEMA.key_column_usage ii on i.CONSTRAINT_SCHEMA = ii.CONSTRAINT_SCHEMA and i.CONSTRAINT_NAME = ii.CONSTRAINT_NAME and i.TABLE_SCHEMA = ii.TABLE_SCHEMA and i.TABLE_NAME = ii.TABLE_NAME
        where i.TABLE_SCHEMA = '{schema}' and i.TABLE_NAME = '{table}'
        and i.CONSTRAINT_TYPE in ('PRIMARY KEY', 'UNIQUE')
        order by i.CONSTRAINT_SCHEMA || '_' || i.CONSTRAINT_NAME, ii.TABLE_SCHEMA, ii.TABLE_NAME, ii.ORDINAL_POSITION"),
        ("preview", $@"select ""$Ordered"".""i"",
            ""$Ordered"".""b"",
            ""$Ordered"".""d"",
            ""$Ordered"".""amount"",
            ""$Ordered"".""s"",
            ""$Ordered"".""day"",
            ""$Ordered"".""ts"",
            ""$Ordered"".""flag""
        from ""{schema}"".""{table}"" ""$Ordered""
        order by ""$Ordered"".""i""
        limit 4096"),
    };

    static int Main(string[] args)
    {
        Console.WriteLine($"Npgsql {typeof(NpgsqlConnection).Assembly.GetName().Version}");
        var con = new NpgsqlConnection(args[0]);
        try { con.Open(); Console.WriteLine("PASS open"); }
        catch (Exception e) { Console.WriteLine($"FAIL open: {e.GetType().Name}: {e.Message.Replace('\n', ' ')}"); return 1; }

        // Typed reads: Npgsql asks for every column in binary.
        Check("int", () => Scalar(con, "SELECT 42::INTEGER"), 42);
        Check("bigint", () => Scalar(con, "SELECT 9000000000::BIGINT"), 9000000000L);
        Check("smallint", () => Scalar(con, "SELECT 7::SMALLINT"), (short)7);
        Check("double", () => Scalar(con, "SELECT 1.5::DOUBLE"), 1.5);
        Check("real", () => Scalar(con, "SELECT 2.5::REAL"), 2.5f);
        Check("bool", () => Scalar(con, "SELECT true"), true);
        Check("text", () => Scalar(con, "SELECT 'héllo'"), "héllo");
        Check("decimal", () => Scalar(con, "SELECT 1234.56::DECIMAL(18,2)"), 1234.56m);
        Check("negative decimal", () => Scalar(con, "SELECT -0.0001::DECIMAL(18,4)"), -0.0001m);
        Check("big decimal", () => Scalar(con, "SELECT 12345678901234567890.123::DECIMAL(38,3)"),
              12345678901234567890.123m);
        Check("date", () => Day(Scalar(con, "SELECT DATE '2026-11-05'")), "2026-11-05");
        Check("timestamp", () => Scalar(con, "SELECT TIMESTAMP '2026-11-05 13:14:15.25'"),
              new DateTime(2026, 11, 5, 13, 14, 15, 250));
        Check("timestamptz", () => ((DateTime)Scalar(con, "SELECT TIMESTAMPTZ '2026-11-05 13:14:15+00'")).ToUniversalTime(),
              new DateTime(2026, 11, 5, 13, 14, 15, DateTimeKind.Utc));
        Check("null", () => Scalar(con, "SELECT NULL::INTEGER"), DBNull.Value);
        Check("hugeint", () => Scalar(con, "SELECT sum(range) FROM range(5)")?.ToString(), "10");

        // A served table, and the row count Power BI's preview reads.
        Check("table", () => Rows(con, "SELECT id FROM sales.revenue ORDER BY id").Count, 7);
        Check("typed table", () => Rows(con, "SELECT * FROM sales.kinds").Count, 3);

        // Parameters, bound in binary.
        Check("int param", () => Scalar(con, "SELECT count(*) FROM sales.revenue WHERE id < @n", ("n", 3))?.ToString(), "3");
        Check("text param", () => Scalar(con, "SELECT @s || '!'", ("s", "hi")), "hi!");
        Check("timestamp param", () => Scalar(con, "SELECT CAST(@d AS VARCHAR)", ("d", new DateTime(2026, 1, 1, 9, 30, 0))),
              "2026-01-01 09:30:00");
        Check("prepared", () =>
        {
            using var cmd = new NpgsqlCommand("SELECT count(*) FROM sales.revenue WHERE id >= @n", con);
            cmd.Parameters.AddWithValue("n", 2);
            cmd.Prepare();
            var a = cmd.ExecuteScalar()?.ToString();
            cmd.Parameters["n"].Value = 5;
            return a + "," + cmd.ExecuteScalar()?.ToString();
        }, "5,2");

        // A Power BI session, query for query: Desktop's navigator and preview of a table, logged by a Postgres
        // server (github.com/datafusion-contrib/datafusion-postgres/issues/218), with public.users as sales.kinds.
        // Npgsql sends DISCARD ALL before each one, since Power Query returns the connection to the pool.
        foreach (var (name, sql) in PowerBiSession("sales", "kinds"))
            Check("power bi: " + name, () => { con.Close(); con.Open(); return Rows(con, sql).Count >= 0; }, true);
        Check("power bi: tables listed", () => Rows(con, PowerBiSession("sales", "kinds")[1].Item2)
            .Exists(r => (string)r[0] == "sales" && (string)r[1] == "kinds"), true);
        Check("power bi: columns listed", () => Rows(con, PowerBiSession("sales", "kinds")[2].Item2).Count, 8);
        Check("power bi: preview", () =>
        {
            var rows = Rows(con, PowerBiSession("sales", "kinds")[6].Item2);
            return $"{rows.Count} {rows[2][0]} {rows[2][3]} {rows[2][4]} {Day(rows[2][5])}";
        }, "3 2 7.00 name 2 2026-01-03");

        // An error comes back as an error, and the connection is still usable after it.
        Check("error", () =>
        {
            try { Scalar(con, "SELECT * FROM does_not_exist"); return "no error"; }
            catch (PostgresException) { return Scalar(con, "SELECT 1::INTEGER"); }
        }, 1);

        con.Close();
        Console.WriteLine($"{failures} failed");
        return failures;
    }
}
