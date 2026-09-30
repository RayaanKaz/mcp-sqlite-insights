# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "mcp[cli]>=2.2,<3",
#     "cryptography<49; sys_platform == 'darwin' and platform_machine == 'x86_64'",
# ]
# ///
"""mcp-sqlite-insights: a read-only SQLite gateway for Claude, built on the MCP Python SDK.

Tools
-----
inspect_schema  Map tables, views, column types, keys and indexes into Markdown tables.
execute_query   Run a single read-only SQL query; results are capped at 50 rows.

Read-only is enforced in four independent layers, so a bypass of any one
layer is still stopped by the others:

1. Query sanitizer   Only one statement, starting with SELECT / WITH / VALUES /
                     EXPLAIN, with no write or admin keywords outside of string
                     literals and comments.
2. Read-only open    The file is opened with a `file:...?mode=ro` URI, so SQLite
                     itself refuses to write to it or create it.
3. query_only pragma `PRAGMA query_only = ON` blocks writes on the connection,
                     including to any database that gets attached.
4. Authorizer        A native SQLite callback that allows only read operations
                     while the statement is being compiled.

Configuration (all optional)
----------------------------
--db PATH                 Path to the SQLite database file.
SQLITE_INSIGHTS_DB=PATH   Same, via environment variable (used if --db is absent).
If neither is set, a demo database (demo.db) is created next to this file.

Run `python server.py --check` for a quick self-test without any MCP client.
"""

from __future__ import annotations

import argparse
import logging
import os
import random
import re
import sqlite3
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

__version__ = "0.1.0"

# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #

MAX_ROWS = 50  # Hard cap on rows returned to Claude, to protect its context window.
MAX_CELL_CHARS = 300  # Long text values are truncated to this many characters.
QUERY_TIMEOUT_SECONDS = 10.0  # Queries running longer than this are cancelled.
COUNT_TIMEOUT_SECONDS = 2.0  # Per-table budget for row counts in inspect_schema.
MAX_DETAILED_TABLES = 25  # Above this, inspect_schema shows an overview only.
SAMPLE_ROWS = 3  # Example rows shown when inspecting a single table.

ENV_DB_PATH = "SQLITE_INSIGHTS_DB"
DEMO_DB_NAME = "demo.db"

# Logs go to stderr: over the stdio transport, stdout carries the protocol itself.
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [sqlite-insights] %(message)s",
)
log = logging.getLogger("sqlite-insights")

# Set by `--db`; takes priority over the environment variable.
_cli_db_path: Path | None = None


# --------------------------------------------------------------------------- #
# Database location and the zero-config demo database
# --------------------------------------------------------------------------- #


def resolve_db_path() -> Path:
    """Return the database to use: --db, then SQLITE_INSIGHTS_DB, then the demo database."""
    if _cli_db_path is not None:
        return _cli_db_path
    env_value = os.environ.get(ENV_DB_PATH, "").strip()
    if env_value:
        return Path(env_value).expanduser().resolve()
    return ensure_demo_db()


def ensure_demo_db() -> Path:
    """Create the demo database on first use and return its path.

    It is written next to this file; if that folder is read-only, the system
    temp folder is used instead. The file is built under a temporary name and
    then renamed into place, so an interrupted run never leaves a half-built
    database behind.
    """
    candidates = [
        Path(__file__).resolve().parent / DEMO_DB_NAME,
        Path(tempfile.gettempdir()) / f"mcp-sqlite-insights-{DEMO_DB_NAME}",
    ]
    for target in candidates:
        if target.exists():
            return target
        try:
            fd, tmp_name = tempfile.mkstemp(prefix=".demo-", suffix=".db", dir=target.parent)
            os.close(fd)
            try:
                _build_demo_db(Path(tmp_name))
                os.replace(tmp_name, target)
            finally:
                if os.path.exists(tmp_name):
                    os.remove(tmp_name)
            log.info("Created demo database at %s", target)
            return target
        except OSError as exc:
            if target.exists():  # Another process created it at the same moment.
                return target
            log.warning("Could not create demo database at %s (%s)", target, exc)
    raise ToolError(
        "No database configured and the demo database could not be created. "
        f"Set {ENV_DB_PATH} or pass --db to point at a SQLite file."
    )


def _build_demo_db(path: Path) -> None:
    """Populate a small, deterministic e-commerce database."""
    rng = random.Random(42)
    first = ["Ava", "Liam", "Noah", "Mia", "Zara", "Omar", "Priya", "Kenji", "Sofia", "Mateo",
             "Amara", "Lucas", "Chloe", "Ravi", "Elena", "Yusuf", "Hana", "Leo", "Nina", "Theo"]
    last = ["Khan", "Smith", "Garcia", "Chen", "Okafor", "Silva", "Novak", "Patel", "Kim", "Rossi"]
    cities = ["London", "Toronto", "Austin", "Berlin", "Lagos", "Mumbai", "Sydney", "Tokyo", None]
    products = [
        ("Mechanical Keyboard", "Electronics", 89.99), ("Wireless Mouse", "Electronics", 29.50),
        ("USB-C Hub", "Electronics", 45.00), ("27in Monitor", "Electronics", 249.00),
        ("Noise-Cancelling Headphones", "Electronics", 199.99), ("Standing Desk", "Furniture", 399.00),
        ("Ergonomic Chair", "Furniture", 289.00), ("Desk Lamp", "Furniture", 34.99),
        ("Notebook (A5)", "Stationery", 6.50), ("Gel Pens (10-pack)", "Stationery", 8.99),
        ("Sticky Notes", "Stationery", 4.25), ("Coffee Beans 1kg", "Pantry", 24.00),
        ("Green Tea (50 bags)", "Pantry", 7.75), ("Water Bottle", "Accessories", 18.00),
        ("Laptop Sleeve", "Accessories", 27.99),
    ]
    statuses = ["delivered"] * 6 + ["shipped"] * 2 + ["pending", "cancelled"]
    start = date(2025, 1, 1)

    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE customers (
                id          INTEGER PRIMARY KEY,
                name        TEXT NOT NULL,
                email       TEXT NOT NULL UNIQUE,
                city        TEXT,
                signup_date TEXT NOT NULL
            );
            CREATE TABLE products (
                id       INTEGER PRIMARY KEY,
                name     TEXT NOT NULL,
                category TEXT NOT NULL,
                price    REAL NOT NULL CHECK (price >= 0),
                in_stock INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE orders (
                id          INTEGER PRIMARY KEY,
                customer_id INTEGER NOT NULL REFERENCES customers(id),
                order_date  TEXT NOT NULL,
                status      TEXT NOT NULL
                            CHECK (status IN ('pending', 'shipped', 'delivered', 'cancelled'))
            );
            CREATE TABLE order_items (
                id         INTEGER PRIMARY KEY,
                order_id   INTEGER NOT NULL REFERENCES orders(id),
                product_id INTEGER NOT NULL REFERENCES products(id),
                quantity   INTEGER NOT NULL CHECK (quantity > 0),
                unit_price REAL NOT NULL
            );
            CREATE INDEX idx_orders_customer ON orders(customer_id);
            CREATE INDEX idx_order_items_order ON order_items(order_id);
            CREATE VIEW monthly_revenue AS
                SELECT substr(o.order_date, 1, 7)               AS month,
                       COUNT(DISTINCT o.id)                     AS orders,
                       ROUND(SUM(oi.quantity * oi.unit_price), 2) AS revenue
                FROM orders o
                JOIN order_items oi ON oi.order_id = o.id
                WHERE o.status != 'cancelled'
                GROUP BY month
                ORDER BY month;
            """
        )
        customers = []
        for cid in range(1, 41):
            given, family = rng.choice(first), rng.choice(last)
            signup = start + timedelta(days=rng.randrange(0, 300))
            customers.append((cid, f"{given} {family}", f"{given}.{family}{cid}@example.com".lower(),
                              rng.choice(cities), signup.isoformat()))
        conn.executemany("INSERT INTO customers VALUES (?, ?, ?, ?, ?)", customers)
        conn.executemany(
            "INSERT INTO products (id, name, category, price, in_stock) VALUES (?, ?, ?, ?, ?)",
            [(i, n, c, p, 0 if i in (4, 12) else 1) for i, (n, c, p) in enumerate(products, 1)],
        )
        item_id = 0
        for oid in range(1, 201):
            order_day = start + timedelta(days=rng.randrange(0, 546))
            conn.execute("INSERT INTO orders VALUES (?, ?, ?, ?)",
                         (oid, rng.randint(1, 40), order_day.isoformat(), rng.choice(statuses)))
            for pid in rng.sample(range(1, len(products) + 1), rng.randint(1, 4)):
                item_id += 1
                conn.execute("INSERT INTO order_items VALUES (?, ?, ?, ?, ?)",
                             (item_id, oid, pid, rng.randint(1, 3), products[pid - 1][2]))
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Read-only connection (layers 2, 3 and 4)
# --------------------------------------------------------------------------- #

_ALLOWED_ACTIONS = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    getattr(sqlite3, "SQLITE_RECURSIVE", 33),  # WITH RECURSIVE
}
_BLOCKED_FUNCTIONS = {"load_extension"}
# Introspection pragmas, usable as table-valued functions: SELECT * FROM pragma_table_info('t').
_READ_ONLY_PRAGMAS = {"table_info", "table_xinfo", "index_list", "index_info", "index_xinfo",
                      "foreign_key_list"}


def _authorizer(action: int, arg1: str | None, arg2: str | None, db_name: str | None,
                trigger: str | None) -> int:
    """Layer 4: allow only read operations while SQLite compiles a statement."""
    if action == sqlite3.SQLITE_PRAGMA:
        return sqlite3.SQLITE_OK if (arg1 or "").lower() in _READ_ONLY_PRAGMAS else sqlite3.SQLITE_DENY
    if action not in _ALLOWED_ACTIONS:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() in _BLOCKED_FUNCTIONS:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def open_readonly(db_path: Path) -> sqlite3.Connection:
    """Open `db_path` so that no statement on the connection can modify anything."""
    if not db_path.is_file():
        raise ToolError(
            f"Database file not found: {db_path}. "
            f"Check the --db argument or the {ENV_DB_PATH} environment variable."
        )
    # Layer 2: `mode=ro` makes SQLite open the file read-only (and never create it).
    # as_uri() percent-encodes spaces, '?' and '#', and handles Windows drive letters.
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0, isolation_level=None)
    try:
        # Layer 3: refuse writes at the connection level, including to attached databases.
        conn.execute("PRAGMA query_only = ON")
        # Hardening for untrusted database files: SQL functions with side effects
        # cannot be called from views or triggers stored in the schema.
        conn.execute("PRAGMA trusted_schema = OFF")
        if hasattr(conn, "setconfig"):  # Python 3.12+
            conn.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, True)
        if hasattr(conn, "setlimit"):  # Python 3.11+: forbid ATTACH entirely.
            conn.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
        # Layer 4 (installed last, so the setup PRAGMAs above are not subject to it).
        conn.set_authorizer(_authorizer)
    except Exception:
        conn.close()
        raise
    return conn


def _set_deadline(conn: sqlite3.Connection, seconds: float) -> None:
    """Make SQLite abort (raising 'interrupted') once `seconds` have elapsed."""
    deadline = time.monotonic() + seconds
    conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)


# --------------------------------------------------------------------------- #
# Query sanitizer (layer 1)
# --------------------------------------------------------------------------- #

_ALLOWED_FIRST_WORDS = {"SELECT", "WITH", "VALUES", "EXPLAIN"}
_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|ATTACH|DETACH|PRAGMA|VACUUM|REINDEX|"
    r"ANALYZE|BEGIN|COMMIT|ROLLBACK|SAVEPOINT|RELEASE|TRUNCATE|UPSERT|LOAD_EXTENSION)\b"
    r"|\bREPLACE\s+INTO\b",
    re.IGNORECASE,
)


def _mask_sql(sql: str) -> tuple[str, int]:
    """Blank out comments, string literals and quoted identifiers.

    Returns the masked text (same length as the input, so positions line up)
    and the index of the first top-level ';', or -1 if there is none. Keywords
    inside strings or quoted names can then no longer confuse the checks.
    """
    out: list[str] = []
    first_semicolon = -1
    i, n = 0, len(sql)
    closers = {"'": "'", '"': '"', "`": "`", "[": "]"}
    while i < n:
        ch = sql[i]
        if ch == "-" and sql.startswith("--", i):
            end = sql.find("\n", i)
            end = n if end == -1 else end
            out.append(" " * (end - i))
            i = end
        elif ch == "/" and sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            end = n if end == -1 else end + 2
            out.append(" " * (end - i))
            i = end
        elif ch in closers:
            close = closers[ch]
            j = i + 1
            while j < n:
                if sql[j] == close:
                    if close != "]" and j + 1 < n and sql[j + 1] == close:
                        j += 2  # Doubled quote is an escaped quote.
                        continue
                    break
                j += 1
            # Keep the delimiters, blank the content: 'DELETE' becomes '      '.
            if j < n:
                out.append(ch + " " * (j - i - 1) + close)
                i = j + 1
            else:  # Unterminated: blank everything to the end.
                out.append(ch + " " * (n - i - 1))
                i = n
        else:
            if ch == ";" and first_semicolon == -1:
                first_semicolon = i
            out.append(ch)
            i += 1
    return "".join(out), first_semicolon


def sanitize_query(sql: str) -> str:
    """Layer 1: validate `sql` and return the single statement to execute."""
    if not sql or not sql.strip():
        raise ToolError("The query is empty.")
    masked, semicolon = _mask_sql(sql)
    if semicolon != -1:
        if masked[semicolon + 1:].strip():
            raise ToolError("Only one SQL statement per call is allowed. Remove the extra ';'.")
        sql, masked = sql[:semicolon], masked[:semicolon]  # Drop the trailing ';' and comments.

    first = re.match(r"\s*([A-Za-z_]+)", masked)
    first_word = first.group(1).upper() if first else ""
    if first_word not in _ALLOWED_FIRST_WORDS:
        raise ToolError(
            f"Blocked: this server is read-only. Queries must start with SELECT, WITH, "
            f"VALUES or EXPLAIN (got {first_word or 'nothing'!r})."
        )
    match = _FORBIDDEN.search(masked)
    if match:
        keyword = " ".join(match.group(0).upper().split())
        raise ToolError(
            f"Blocked: the keyword {keyword} is not allowed on this read-only server. "
            'If it is a column or table name, wrap it in double quotes, e.g. "release".'
        )
    return sql.strip()


# --------------------------------------------------------------------------- #
# Markdown formatting
# --------------------------------------------------------------------------- #


def _cell(value: object) -> str:
    """Render one value as a safe, single-line Markdown table cell."""
    if value is None:
        return "NULL"
    if isinstance(value, bytes):
        return f"<BLOB {len(value)} bytes>"
    if isinstance(value, float):
        text = f"{value:.12g}"
    else:
        text = str(value)
    text = " ".join(text.splitlines())
    if len(text) > MAX_CELL_CHARS:
        text = text[:MAX_CELL_CHARS] + "..."
    return text.replace("\\", "\\\\").replace("|", "\\|")


def markdown_table(headers: list[str], rows: list[tuple]) -> str:
    lines = [
        "| " + " | ".join(_cell(h) for h in headers) + " |",
        "|" + "---|" * len(headers),
    ]
    lines += ["| " + " | ".join(_cell(v) for v in row) + " |" for row in rows]
    return "\n".join(lines)


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


# --------------------------------------------------------------------------- #
# MCP server and tools
# --------------------------------------------------------------------------- #

mcp = MCPServer(
    "sqlite-insights",
    version=__version__,
    instructions=(
        "Read-only access to a SQLite database. Call inspect_schema first to learn the "
        "tables, column types and relationships, then use execute_query with SELECT "
        f"statements. Results are capped at {MAX_ROWS} rows, so prefer aggregates "
        "(COUNT, SUM, GROUP BY) and LIMIT over fetching raw rows."
    ),
)

_READ_ONLY = dict(read_only_hint=True, destructive_hint=False, idempotent_hint=True,
                  open_world_hint=False)


@mcp.tool(
    annotations=ToolAnnotations(title="Inspect database schema", **_READ_ONLY),
    structured_output=False,
)
def inspect_schema(table_name: str | None = None) -> str:
    """Describe the SQLite database structure as Markdown tables.

    Without arguments: an overview of every table and view (row counts and
    column counts), followed by each table's columns, types, primary keys,
    NOT NULL constraints, defaults, foreign keys and indexes.
    With `table_name`: full detail for that one table or view, plus a few
    sample rows showing the real data formats (dates, casing, units).
    Call this before writing queries with execute_query.
    """
    db_path = resolve_db_path()
    conn = open_readonly(db_path)
    try:
        objects = conn.execute(
            "SELECT name, type FROM sqlite_schema "
            "WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' "
            "ORDER BY type, name"
        ).fetchall()
        if not objects:
            return f"# Schema: {db_path.name}\n\nThe database contains no tables or views."

        if table_name is not None:
            match = next((o for o in objects if o[0] == table_name), None)
            if match is None:
                match = next((o for o in objects if o[0].lower() == table_name.lower()), None)
            if match is None:
                names = ", ".join(o[0] for o in objects)
                raise ToolError(f"No table or view named {table_name!r}. Available: {names}")
            objects = [match]

        tables = sum(1 for _, kind in objects if kind == "table")
        views = len(objects) - tables
        parts = [
            f"# Schema: {db_path.name}",
            f"Database `{db_path}` (opened read-only): {tables} table(s), {views} view(s).",
        ]

        columns = {name: conn.execute(f"PRAGMA table_info({_quote_ident(name)})").fetchall()
                   for name, _ in objects}
        counts = {name: _row_count(conn, name) for name, kind in objects if kind == "table"}

        if table_name is None:
            overview = [(name, kind, counts.get(name, "-"), len(columns[name]))
                        for name, kind in objects]
            parts += ["## Overview", markdown_table(["Name", "Type", "Rows", "Columns"], overview)]
            if len(objects) > MAX_DETAILED_TABLES:
                parts.append(
                    f"_{len(objects)} objects is too many to detail at once. Call "
                    "inspect_schema with table_name to see one table's columns._"
                )
                return "\n\n".join(parts)

        for name, kind in objects:
            parts.append(_describe(conn, name, kind, columns[name], counts.get(name)))
            if table_name is not None:
                parts.append(_sample_rows(conn, name))
        return "\n\n".join(parts)
    except sqlite3.Error as exc:
        raise ToolError(f"Could not read the schema: {exc}") from exc
    finally:
        conn.close()


def _row_count(conn: sqlite3.Connection, table: str) -> int | str:
    """Count rows, giving up (and returning '?') on very large tables."""
    _set_deadline(conn, COUNT_TIMEOUT_SECONDS)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {_quote_ident(table)}").fetchone()[0]
    except sqlite3.OperationalError:
        return "?"
    finally:
        conn.set_progress_handler(None, 0)


def _describe(conn: sqlite3.Connection, name: str, kind: str, columns: list[tuple],
              count: int | str | None) -> str:
    foreign_keys: dict[str, str] = {}
    indexes: list[str] = []
    if kind == "table":
        for fk in conn.execute(f"PRAGMA foreign_key_list({_quote_ident(name)})"):
            # fk: (id, seq, table, from, to, on_update, on_delete, match)
            foreign_keys[fk[3]] = f"{fk[2]}.{fk[4] or '(primary key)'}"
        for idx in conn.execute(f"PRAGMA index_list({_quote_ident(name)})"):
            # idx: (seq, name, unique, origin, partial)
            cols = [c[2] for c in conn.execute(f"PRAGMA index_info({_quote_ident(idx[1])})")]
            label = "unique " if idx[2] else ""
            indexes.append(f"`{idx[1]}` ({label}{', '.join(str(c) for c in cols)})")

    rows = [
        (
            col[1],                                   # name
            col[2] or "(any)",                        # declared type
            "yes" if col[5] else "",                  # part of primary key
            "yes" if col[3] else "",                  # NOT NULL
            "" if col[4] is None else col[4],         # default
            foreign_keys.get(col[1], ""),             # references
        )
        for col in columns
    ]
    heading = f"## {name}" + (f" ({count} rows)" if kind == "table" else " (view)")
    section = [heading, markdown_table(
        ["Column", "Type", "Primary key", "Not null", "Default", "References"], rows)]
    if indexes:
        section.append("Indexes: " + "; ".join(indexes))
    return "\n\n".join(section)


def _sample_rows(conn: sqlite3.Connection, name: str) -> str:
    _set_deadline(conn, COUNT_TIMEOUT_SECONDS)
    try:
        cur = conn.execute(f"SELECT * FROM {_quote_ident(name)} LIMIT {SAMPLE_ROWS}")
        headers = [d[0] for d in cur.description]
        rows = cur.fetchall()
    except sqlite3.OperationalError:
        return "_Sample rows unavailable (timed out)._"
    finally:
        conn.set_progress_handler(None, 0)
    if not rows:
        return "_This table is empty._"
    return f"Sample rows:\n\n{markdown_table(headers, rows)}"


@mcp.tool(
    annotations=ToolAnnotations(title="Run read-only SQL query", **_READ_ONLY),
    structured_output=False,
)
def execute_query(sql: str) -> str:
    """Run one read-only SQL query against the SQLite database; returns a Markdown table.

    Only a single SELECT, WITH (CTE), VALUES or EXPLAIN statement is accepted.
    Anything that could modify data or settings is rejected. At most 50 rows are
    returned, so use aggregates (COUNT, SUM, AVG, GROUP BY), WHERE filters and
    LIMIT to get precise answers. Queries running longer than 10 seconds are
    cancelled. Use inspect_schema first to learn the table and column names.
    """
    statement = sanitize_query(sql)
    conn = open_readonly(resolve_db_path())
    started = time.perf_counter()
    try:
        _set_deadline(conn, QUERY_TIMEOUT_SECONDS)
        cursor = conn.execute(statement)
        if cursor.description is None:
            return "The statement ran but returned no result set."
        headers = [d[0] for d in cursor.description]
        # Fetch one extra row only to learn whether the result was truncated,
        # so memory use stays bounded however large the result set is.
        rows = cursor.fetchmany(MAX_ROWS + 1)
    except (sqlite3.Error, sqlite3.Warning) as exc:
        message = str(exc)
        if "interrupted" in message:
            raise ToolError(
                f"Query cancelled: it exceeded the {QUERY_TIMEOUT_SECONDS:g}-second limit. "
                "Simplify it, filter with WHERE, or add a LIMIT."
            ) from exc
        if "not authorized" in message:
            raise ToolError("Blocked: this server is read-only and the query tried a "
                            "disallowed operation. For schema details, use inspect_schema.") from exc
        if "no such table: pragma_" in message:  # Pragma functions are refused on SQLite < 3.42.
            message += ". For schema details, use inspect_schema"
        raise ToolError(f"SQLite error: {message}") from exc
    finally:
        conn.close()

    elapsed_ms = (time.perf_counter() - started) * 1000
    truncated = len(rows) > MAX_ROWS
    rows = rows[:MAX_ROWS]
    if not rows:
        return f"Columns: {', '.join(headers)}\n\n_0 rows returned ({elapsed_ms:.1f} ms)._"
    footer = (
        f"_Showing the first {MAX_ROWS} rows; the query matched more. Use COUNT(*), "
        f"GROUP BY, WHERE or LIMIT to narrow it down ({elapsed_ms:.1f} ms)._"
        if truncated
        else f"_{len(rows)} row(s) returned ({elapsed_ms:.1f} ms)._"
    )
    return f"{markdown_table(headers, rows)}\n\n{footer}"


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #


def _self_check() -> int:
    """Exercise both tools directly and print the results (no MCP client needed)."""
    print(f"mcp-sqlite-insights {__version__}: self-check\n")
    print(inspect_schema())
    if _cli_db_path is None and not os.environ.get(ENV_DB_PATH, "").strip():  # Demo database.
        print("\n--- execute_query: revenue by month (first 5) ---\n")
        print(execute_query("SELECT * FROM monthly_revenue LIMIT 5"))
    print("\n--- execute_query: row cap (query produces 200 rows) ---\n")
    many = "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n LIMIT 200) SELECT x FROM n"
    print(execute_query(many).splitlines()[-1])
    print("\n--- execute_query: write attempt (must be blocked) ---\n")
    try:
        execute_query("DELETE FROM sqlite_schema")
    except ToolError as exc:
        print(f"OK, blocked -> {exc}")
    else:
        print("FAILED: the write was not blocked")
        return 1
    print("\nSelf-check passed.")
    return 0


def main() -> None:
    global _cli_db_path
    parser = argparse.ArgumentParser(description="Read-only SQLite MCP server for Claude.")
    parser.add_argument("--db", help=f"SQLite database file (default: ${ENV_DB_PATH}, "
                                     "or a generated demo database)")
    parser.add_argument("--check", action="store_true",
                        help="run a quick self-test and exit instead of starting the server")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args()

    if args.db:
        _cli_db_path = Path(args.db).expanduser().resolve()
    if args.check:
        sys.exit(_self_check())

    try:
        log.info("Starting mcp-sqlite-insights %s (database: %s)", __version__, resolve_db_path())
    except ToolError as exc:  # Keep serving: each tool call will report the problem to Claude.
        log.error("%s", exc)
    mcp.run()  # stdio transport: Claude Desktop / Claude Code launch this process.


if __name__ == "__main__":
    main()
