"""Storage: Postgres when DATABASE_URL/POSTGRES_URL is set (Vercel + Neon), else SQLite."""
import os
import sqlite3
from contextlib import contextmanager

def _database_url() -> str | None:
    for key in ("DATABASE_URL", "POSTGRES_URL"):
        if os.getenv(key):
            return os.getenv(key)
    # Vercel's "Connect Project" dialog can add a prefix, e.g. STORAGE_DATABASE_URL
    for key, val in sorted(os.environ.items()):
        if key.endswith(("_DATABASE_URL", "_POSTGRES_URL")) and val.startswith(("postgres://", "postgresql://")):
            return val
    return None


DATABASE_URL = _database_url()
IS_PG = bool(DATABASE_URL)
# Vercel's filesystem is read-only except /tmp, and /tmp doesn't survive between instances.
DB_PATH = os.getenv("KHATA_DB") or ("/tmp/khata.db" if os.getenv("VERCEL")
                                    else os.path.join(os.path.dirname(__file__), "khata.db"))
STORAGE = "postgres" if IS_PG else ("sqlite-ephemeral" if os.getenv("VERCEL") else "sqlite")

_NOW_PG = "to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')"
SCHEMA_PG = f"""
CREATE TABLE IF NOT EXISTS customers (
  id SERIAL PRIMARY KEY, name TEXT NOT NULL, phone TEXT, created_at TEXT DEFAULT {_NOW_PG});
CREATE UNIQUE INDEX IF NOT EXISTS customers_name_ci ON customers (lower(name));
CREATE TABLE IF NOT EXISTS products (
  id SERIAL PRIMARY KEY, name TEXT NOT NULL UNIQUE, unit TEXT NOT NULL, price DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS entries (
  id SERIAL PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customers(id),
  kind TEXT NOT NULL CHECK (kind IN ('udhaar','payment')),
  item TEXT, qty DOUBLE PRECISION, unit TEXT, amount DOUBLE PRECISION NOT NULL, transcript TEXT,
  created_at TEXT DEFAULT {_NOW_PG});
CREATE TABLE IF NOT EXISTS bills (
  id SERIAL PRIMARY KEY, supplier TEXT, bill_no TEXT, bill_date TEXT,
  total DOUBLE PRECISION, gst DOUBLE PRECISION, line_items TEXT, status TEXT DEFAULT 'unpaid',
  job_id TEXT, created_at TEXT DEFAULT {_NOW_PG});
CREATE TABLE IF NOT EXISTS calls (
  id SERIAL PRIMARY KEY, phone TEXT, customer_id INTEGER, campaign_id TEXT,
  status TEXT, outcome TEXT, transcript TEXT, created_at TEXT DEFAULT {_NOW_PG})
"""

SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS customers (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE, phone TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS products (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
  unit TEXT NOT NULL, price REAL NOT NULL);
CREATE TABLE IF NOT EXISTS entries (
  id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customers(id),
  kind TEXT NOT NULL CHECK (kind IN ('udhaar','payment')),
  item TEXT, qty REAL, unit TEXT, amount REAL NOT NULL, transcript TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS bills (
  id INTEGER PRIMARY KEY, supplier TEXT, bill_no TEXT, bill_date TEXT,
  total REAL, gst REAL, line_items TEXT, status TEXT DEFAULT 'unpaid',
  job_id TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS calls (
  id INTEGER PRIMARY KEY, phone TEXT, customer_id INTEGER, campaign_id TEXT,
  status TEXT, outcome TEXT, transcript TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)
"""

SEED_PRODUCTS = [
    ("atta", "kg", 45), ("chawal", "kg", 60), ("cheeni", "kg", 44), ("dal", "kg", 120),
    ("tel", "litre", 150), ("doodh", "litre", 56), ("namak", "packet", 25),
    ("chai patti", "packet", 120), ("sabun", "piece", 35), ("biscuit", "packet", 10),
    ("maggi", "packet", 14), ("anda", "piece", 7), ("bread", "packet", 40),
    ("ghee", "kg", 600), ("besan", "kg", 90),
]


class _Conn:
    """Lets the app write SQLite-style '?' placeholders for both backends."""

    def __init__(self, raw):
        self.raw = raw

    def execute(self, sql: str, args=()):
        return self.raw.execute(sql.replace("?", "%s") if IS_PG else sql, tuple(args))

    def executemany(self, sql: str, seq):
        if IS_PG:
            with self.raw.cursor() as cur:
                cur.executemany(sql.replace("?", "%s"), seq)
        else:
            self.raw.executemany(sql, seq)


def _connect():
    if IS_PG:
        import psycopg
        from psycopg.rows import dict_row
        # prepare_threshold=None: Neon's pooled endpoint (PgBouncer) can't hold prepared statements
        return psycopg.connect(DATABASE_URL, row_factory=dict_row, prepare_threshold=None)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


_ready = False


@contextmanager
def conn():
    global _ready
    raw = _connect()
    try:
        c = _Conn(raw)
        if not _ready:
            _init(c)
            _ready = True
        yield c
        raw.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        raw.close()


def _init(c: _Conn):
    for stmt in (SCHEMA_PG if IS_PG else SCHEMA_SQLITE).split(";"):
        if stmt.strip():
            c.execute(stmt)
    if not c.execute("SELECT 1 FROM products LIMIT 1").fetchone():
        c.executemany("INSERT INTO products(name, unit, price) VALUES (?,?,?) ON CONFLICT DO NOTHING",
                      SEED_PRODUCTS)


def init():
    with conn():
        pass


def rows(sql: str, *args) -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def insert(c: _Conn, sql: str, args) -> int:
    """INSERT ... RETURNING id works on Postgres and SQLite >= 3.35."""
    return c.execute(sql + " RETURNING id", args).fetchone()["id"]


def get_or_create_customer(c: _Conn, name: str) -> int:
    name = name.strip()
    row = c.execute("SELECT id FROM customers WHERE lower(name) = lower(?)", (name,)).fetchone()
    if row:
        return row["id"]
    return insert(c, "INSERT INTO customers(name) VALUES (?)", (name,))


BALANCES_SQL = """
SELECT cu.id, cu.name, cu.phone,
  COALESCE(SUM(CASE WHEN e.kind='udhaar' THEN e.amount ELSE -e.amount END), 0) AS balance,
  MAX(e.created_at) AS last_activity
FROM customers cu LEFT JOIN entries e ON e.customer_id = cu.id
GROUP BY cu.id ORDER BY balance DESC, cu.name
"""
