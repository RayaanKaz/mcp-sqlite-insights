"""Tests for mcp-sqlite-insights. Run with: python -m unittest -v"""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

import server
from mcp.server.mcpserver.exceptions import ToolError


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class DatabaseTestCase(unittest.TestCase):
    """Gives every test a fresh copy of the demo database."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "shop.db"
        server._build_demo_db(self.db)
        patcher = mock.patch.object(server, "_cli_db_path", self.db)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)


class SanitizerTests(unittest.TestCase):
    ALLOWED = [
        "SELECT 1",
        "select * from customers",
        "  SELECT 1;  ",
        "SELECT 1; -- trailing comment",
        "/* leading comment */ SELECT 1",
        "-- comment\nSELECT 1",
        "WITH t AS (SELECT 1 AS x) SELECT x FROM t",
        "VALUES (1, 2)",
        "EXPLAIN QUERY PLAN SELECT * FROM orders",
        "SELECT 'DELETE FROM x; DROP TABLE y' AS s",
        'SELECT "update", [drop], `insert` FROM t',
        "SELECT replace(name, 'a', 'b') FROM customers",
        "SELECT 'it''s; fine'",
        "SELECT created_at, updated_by FROM t",
        "SELECT*FROM customers",
        "SELECT(1)",
    ]
    BLOCKED = [
        "",
        "   ",
        "-- only a comment",
        "DELETE FROM customers",
        "delete from customers",
        "INSERT INTO customers VALUES (1)",
        "UPDATE customers SET name = 'x'",
        "DROP TABLE customers",
        "SELECT 1; DROP TABLE customers",
        "SELECT 1; SELECT 2",
        "WITH t AS (SELECT 1) DELETE FROM customers",
        "WITH t AS (SELECT 1) INSERT INTO customers SELECT * FROM t",
        "WITH t AS (SELECT 1) REPLACE INTO customers VALUES (1)",
        "REPLACE INTO customers VALUES (1)",
        "PRAGMA query_only = OFF",
        "ATTACH DATABASE 'x.db' AS x",
        "VACUUM INTO 'copy.db'",
        "CREATE TABLE t (x)",
        "BEGIN",
        "SELECT load_extension('evil')",
        "/* SELECT */ DELETE FROM customers",
        "SELECT 1 /* ; */ ; DELETE FROM customers",
    ]

    def test_allowed(self) -> None:
        for sql in self.ALLOWED:
            with self.subTest(sql=sql):
                server.sanitize_query(sql)

    def test_blocked(self) -> None:
        for sql in self.BLOCKED:
            with self.subTest(sql=sql), self.assertRaises(ToolError):
                server.sanitize_query(sql)

    def test_trailing_semicolon_and_comment_are_removed(self) -> None:
        self.assertEqual(server.sanitize_query("SELECT 1; -- bye"), "SELECT 1")

    def test_mask_keeps_positions(self) -> None:
        for sql in ["SELECT 'abc", "SELECT \"x", "SELECT [x", "SELECT 1 /* open", "'", "SELECT ''"]:
            with self.subTest(sql=sql):
                self.assertEqual(len(server._mask_sql(sql)[0]), len(sql))


class ReadOnlyLayerTests(DatabaseTestCase):
    """Writes must fail even when the sanitizer (layer 1) is bypassed entirely."""

    WRITES = [
        "DELETE FROM customers",
        "INSERT INTO customers (name, email, signup_date) VALUES ('x', 'x', 'x')",
        "UPDATE products SET price = 0",
        "DROP TABLE orders",
        "CREATE TABLE evil (x)",
        "CREATE TEMP TABLE evil (x)",
        "PRAGMA query_only = OFF",
        "PRAGMA writable_schema = ON",
        "ALTER TABLE customers ADD COLUMN x",
        "VACUUM",
    ]

    def _open(self, authorizer: bool) -> sqlite3.Connection:
        conn = server.open_readonly(self.db)
        if not authorizer:
            conn.set_authorizer(None)  # Simulate a bypass of layer 4 as well.
        return conn

    def _attempt_all(self, authorizer: bool) -> None:
        before = _digest(self.db)
        for sql in self.WRITES:
            conn = self._open(authorizer)
            try:
                with self.subTest(sql=sql, authorizer=authorizer), self.assertRaises(sqlite3.Error):
                    conn.execute(sql)
                    conn.execute("DELETE FROM customers")  # In case a PRAGMA "succeeded".
            finally:
                conn.close()
        self.assertEqual(_digest(self.db), before, "database file was modified")

    def test_writes_fail_with_all_native_layers(self) -> None:
        self._attempt_all(authorizer=True)

    def test_writes_fail_without_authorizer(self) -> None:
        # Layers 2 (mode=ro) and 3 (query_only) alone must still hold.
        self._attempt_all(authorizer=False)

    def test_attach_is_refused(self) -> None:
        target = Path(self._tmp.name) / "created.db"
        conn = self._open(authorizer=True)
        try:
            with self.assertRaisesRegex(sqlite3.Error, "not authorized|too many attached"):
                conn.execute(f"ATTACH DATABASE '{target}' AS x")
        finally:
            conn.close()
        self.assertFalse(target.exists())

    def test_attached_database_is_not_writable_without_authorizer(self) -> None:
        # Even with layers 1 and 4 bypassed, query_only stops writes to attached files.
        target = Path(self._tmp.name) / "other.db"
        sqlite3.connect(target).close()
        conn = self._open(authorizer=False)
        try:
            with self.assertRaises(sqlite3.Error):
                conn.execute(f"ATTACH DATABASE '{target}' AS x")
                conn.execute("CREATE TABLE x.t (a)")
        finally:
            conn.close()
        self.assertEqual(target.stat().st_size, 0)

    def test_missing_file_is_not_created(self) -> None:
        missing = Path(self._tmp.name) / "nope.db"
        with self.assertRaises(ToolError):
            server.open_readonly(missing)
        self.assertFalse(missing.exists())

    def test_authorizer_blocks_write_that_passes_sanitizer(self) -> None:
        conn = server.open_readonly(self.db)
        try:
            with self.assertRaisesRegex(sqlite3.DatabaseError, "not authorized"):
                conn.execute("SELECT load_extension('x')")
        finally:
            conn.close()


class ExecuteQueryTests(DatabaseTestCase):
    def test_returns_markdown_table(self) -> None:
        out = server.execute_query("SELECT id, name FROM products ORDER BY id LIMIT 2")
        self.assertTrue(out.startswith("| id | name |\n|---|---|\n| 1 | Mechanical Keyboard |"))
        self.assertIn("2 row(s) returned", out)

    def test_row_cap(self) -> None:
        exactly = server.execute_query("SELECT id FROM orders LIMIT 50")
        self.assertIn("50 row(s) returned", exactly)
        over = server.execute_query("SELECT id FROM orders")
        data_rows = [line for line in over.splitlines() if line.startswith("| ")][1:]
        self.assertEqual(len(data_rows), server.MAX_ROWS)
        self.assertIn("Showing the first 50 rows", over)

    def test_zero_rows(self) -> None:
        out = server.execute_query("SELECT id, name FROM customers WHERE id < 0")
        self.assertIn("Columns: id, name", out)
        self.assertIn("0 rows returned", out)

    def test_cte_join_and_aggregate(self) -> None:
        out = server.execute_query(
            "WITH spend AS (SELECT o.customer_id, SUM(oi.quantity * oi.unit_price) AS total "
            "FROM orders o JOIN order_items oi ON oi.order_id = o.id GROUP BY 1) "
            "SELECT COUNT(*) FROM spend"
        )
        self.assertIn("1 row(s) returned", out)

    def test_pragma_function(self) -> None:
        try:
            out = server.execute_query("SELECT name FROM pragma_table_info('orders')")
        except ToolError as exc:
            # SQLite < 3.42 reports a pragma function's first use as a write to
            # sqlite_master, which the authorizer refuses (surfacing as "not
            # authorized" or "no such table"): a safe failure.
            self.assertLess(sqlite3.sqlite_version_info, (3, 42, 0))
            self.assertIn("inspect_schema", str(exc))
        else:
            self.assertIn("customer_id", out)

    def test_write_is_blocked_with_clear_message(self) -> None:
        before = _digest(self.db)
        for sql in ["DELETE FROM customers", "WITH x AS (SELECT 1) DELETE FROM customers",
                    "SELECT 1; DROP TABLE customers"]:
            with self.subTest(sql=sql), self.assertRaisesRegex(ToolError, "(Blocked|one SQL)"):
                server.execute_query(sql)
        self.assertEqual(_digest(self.db), before)

    def test_disallowed_pragma_function(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            server.execute_query("SELECT * FROM pragma_database_list")
        if sqlite3.sqlite_version_info >= (3, 42, 0):
            self.assertIn("read-only", str(ctx.exception))

    def test_sql_error_is_reported(self) -> None:
        with self.assertRaisesRegex(ToolError, "no such table"):
            server.execute_query("SELECT * FROM not_a_table")

    def test_timeout(self) -> None:
        endless = "WITH RECURSIVE r(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM r) SELECT COUNT(*) FROM r"
        with mock.patch.object(server, "QUERY_TIMEOUT_SECONDS", 0.2), \
                self.assertRaisesRegex(ToolError, "cancelled"):
            server.execute_query(endless)

    def test_cell_formatting(self) -> None:
        out = server.execute_query(
            "SELECT NULL AS n, x'00ff' AS b, 'a|b' AS p, 'line1' || char(10) || 'line2' AS nl, "
            "0.1 + 0.2 AS f, 'a\\b' AS bs"
        )
        row = out.splitlines()[2]
        self.assertEqual(row, "| NULL | <BLOB 2 bytes> | a\\|b | line1 line2 | 0.3 | a\\\\b |")

    def test_long_values_are_truncated(self) -> None:
        out = server.execute_query("SELECT printf('%.1000c', 'x') AS long")
        cell = out.splitlines()[2]
        self.assertLess(len(cell), server.MAX_CELL_CHARS + 20)
        self.assertIn("...", cell)


class InspectSchemaTests(DatabaseTestCase):
    def test_overview_and_details(self) -> None:
        out = server.inspect_schema()
        self.assertIn("| customers | table | 40 | 5 |", out)
        self.assertIn("| monthly_revenue | view | - | 3 |", out)
        self.assertIn("| customer_id | INTEGER |  | yes |  | customers.id |", out)
        self.assertIn("| in_stock | INTEGER |  | yes | 1 |  |", out)
        self.assertIn("`idx_orders_customer` (customer_id)", out)

    def test_single_table_with_samples(self) -> None:
        out = server.inspect_schema("Orders")  # Case-insensitive fallback.
        self.assertIn("## orders (200 rows)", out)
        self.assertIn("Sample rows:", out)
        self.assertNotIn("## customers", out)

    def test_unknown_table(self) -> None:
        with self.assertRaisesRegex(ToolError, "Available: .*customers"):
            server.inspect_schema("nope")

    def test_many_tables_gives_overview_only(self) -> None:
        with mock.patch.object(server, "MAX_DETAILED_TABLES", 2):
            out = server.inspect_schema()
        self.assertIn("too many to detail", out)
        self.assertNotIn("## customers", out)

    def test_awkward_identifiers_and_paths(self) -> None:
        odd = Path(self._tmp.name) / "my data #1 %41.db"  # %41 must not be decoded to "A"
        conn = sqlite3.connect(odd)
        conn.execute('CREATE TABLE "weird ""name"" | t" ("select" TEXT, "a|b" INTEGER)')
        conn.execute('INSERT INTO "weird ""name"" | t" VALUES (\'v\', 1)')
        conn.commit()
        conn.close()
        with mock.patch.object(server, "_cli_db_path", odd):
            out = server.inspect_schema()
            self.assertIn('weird "name" \\| t', out)
            self.assertIn("| a\\|b | INTEGER |", out)
            self.assertIn("| v | 1 |", server.execute_query('SELECT * FROM "weird ""name"" | t"'))


class ConfigurationTests(unittest.TestCase):
    def test_env_var_selects_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "env.db"
            server._build_demo_db(db)
            with mock.patch.object(server, "_cli_db_path", None), \
                    mock.patch.dict("os.environ", {server.ENV_DB_PATH: str(db)}):
                self.assertEqual(server.resolve_db_path(), db.resolve())

    def test_demo_database_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp) / "a.db", Path(tmp) / "b.db"
            server._build_demo_db(a)
            server._build_demo_db(b)
            query = "SELECT group_concat(id || status) FROM orders"
            results = []
            for path in (a, b):
                with closing(sqlite3.connect(path)) as conn:  # Windows can't delete open files.
                    results.append(conn.execute(query).fetchone())
            self.assertEqual(results[0], results[1])


class EndToEndTests(DatabaseTestCase):
    """Launch server.py as a real subprocess and talk MCP to it over stdio."""

    def test_stdio_round_trip(self) -> None:
        from mcp import Client, StdioServerParameters

        params = StdioServerParameters(
            command=sys.executable,
            args=[str(Path(server.__file__).resolve()), "--db", str(self.db)],
        )

        async def scenario() -> None:
            async with Client(params) as client:
                tools = {t.name: t for t in (await client.list_tools()).tools}
                self.assertEqual(set(tools), {"inspect_schema", "execute_query"})
                for tool in tools.values():
                    self.assertTrue(tool.annotations.read_only_hint)
                    self.assertFalse(tool.annotations.destructive_hint)

                result = await client.call_tool("execute_query", {"sql": "SELECT COUNT(*) AS n FROM orders"})
                self.assertFalse(result.is_error)
                self.assertIn("| 200 |", result.content[0].text)

                result = await client.call_tool("execute_query", {"sql": "DROP TABLE orders"})
                self.assertTrue(result.is_error)
                self.assertIn("Blocked", result.content[0].text)

                result = await client.call_tool("inspect_schema", {})
                self.assertIn("## order_items (508 rows)", result.content[0].text)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
