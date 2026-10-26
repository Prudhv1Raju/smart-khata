"""SQLite storage: customers, khata entries, products, supplier bills, call log."""
import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.getenv("KHATA_DB", os.path.join(os.path.dirname(__file__), "khata.db"))

SCHEMA = """
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
  status TEXT, outcome TEXT, transcript TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
"""

SEED_PRODUCTS = [
    ("atta", "kg", 45), ("chawal", "kg", 60), ("cheeni", "kg", 44), ("dal", "kg", 120),
    ("tel", "litre", 150), ("doodh", "litre", 56), ("namak", "packet", 25),
    ("chai patti", "packet", 120), ("sabun", "piece", 35), ("biscuit", "packet", 10),
    ("maggi", "packet", 14), ("anda", "piece", 7), ("bread", "packet", 40),
    ("ghee", "kg", 600), ("besan", "kg", 90),
]


@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init():
    with conn() as c:
        c.executescript(SCHEMA)
        if not c.execute("SELECT 1 FROM products LIMIT 1").fetchone():
            c.executemany("INSERT INTO products(name, unit, price) VALUES (?,?,?)", SEED_PRODUCTS)


def rows(sql: str, *args) -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def get_or_create_customer(c: sqlite3.Connection, name: str) -> int:
    name = name.strip()
    row = c.execute("SELECT id FROM customers WHERE name = ?", (name,)).fetchone()
    if row:
        return row["id"]
    return c.execute("INSERT INTO customers(name) VALUES (?)", (name,)).lastrowid


BALANCES_SQL = """
SELECT cu.id, cu.name, cu.phone,
  COALESCE(SUM(CASE WHEN e.kind='udhaar' THEN e.amount ELSE -e.amount END), 0) AS balance,
  MAX(e.created_at) AS last_activity
FROM customers cu LEFT JOIN entries e ON e.customer_id = cu.id
GROUP BY cu.id ORDER BY balance DESC, cu.name
"""
