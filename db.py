import os
import sqlite3
from datetime import datetime, timedelta

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fridge.db")

EXPIRY_DAYS = {
    "молоко": 5,
    "йогурт": 5,
    "кефир": 5,
    "творог": 7,
    "сыр": 21,
    "масло": 14,
    "сливки": 5,
    "мясо": 3,
    "курица": 3,
    "говядина": 4,
    "рыба": 2,
    "морепродукты": 2,
    "овощи": 7,
    "огурцы": 7,
    "помидоры": 5,
    "морковь": 14,
    "фрукты": 7,
    "яблоки": 14,
    "бананы": 7,
    "хлеб": 3,
    "напитки": 30,
    "сок": 14,
    "вода": 30,
    "бакалея": 180,
    "заморозка": 90,
    "не еда": None,
}

MAX_PURCHASE_AGE = timedelta(days=365)


def get_connection():
    return sqlite3.connect(DB_PATH)


def init_db():
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            quantity REAL,
            unit TEXT,
            price REAL,
            category TEXT,
            expiry_date TEXT,
            purchase_date TEXT,
            status TEXT DEFAULT 'fresh',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    try:
        c.execute("SELECT purchase_date FROM products LIMIT 1")
    except sqlite3.OperationalError:
        c.execute("ALTER TABLE products ADD COLUMN purchase_date TEXT")
    conn.commit()
    conn.close()


def _normalize_purchase_date(purchase_date):
    today = datetime.now().date()
    parsed_date = None

    if purchase_date:
        normalized = str(purchase_date).strip()
        for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y", "%Y/%m/%d", "%d/%m/%Y"):
            try:
                parsed_date = datetime.strptime(normalized, fmt).date()
                break
            except ValueError:
                continue

    if parsed_date is None or parsed_date > today or today - parsed_date > MAX_PURCHASE_AGE:
        parsed_date = today

    return parsed_date.strftime("%Y-%m-%d")


def add_product(name, quantity, unit, price, category, purchase_date=None):
    category_key = (category or "").strip().lower()
    days = EXPIRY_DAYS.get(category_key, 7)

    normalized_purchase_date = _normalize_purchase_date(purchase_date)
    base_date = datetime.strptime(normalized_purchase_date, "%Y-%m-%d")

    expiry = None
    if days is not None:
        expiry = (base_date + timedelta(days=days)).strftime("%Y-%m-%d")

    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        INSERT INTO products (name, quantity, unit, price, category, expiry_date, purchase_date)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (name, quantity, unit, price, category, expiry, normalized_purchase_date))
    conn.commit()
    conn.close()


def get_fridge():
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM products ORDER BY expiry_date ASC")
    rows = c.fetchall()
    conn.close()
    return rows


def get_expiring(days=3):
    threshold = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        SELECT name, quantity, unit, expiry_date FROM products
        WHERE expiry_date IS NOT NULL AND expiry_date <= ? AND category != 'не еда'
        ORDER BY expiry_date ASC
    """, (threshold,))
    rows = c.fetchall()
    conn.close()
    return rows


def get_all_for_cooking():
    """Возвращает продукты для готовки: скоропортящиеся идут первыми."""
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        SELECT name, quantity, unit, category, expiry_date FROM products
        WHERE category NOT IN ('не еда', 'хлеб', 'батон', 'выпечка', 'напитки', 'сладости')
        ORDER BY CASE WHEN expiry_date IS NULL THEN 1 ELSE 0 END, expiry_date ASC
    """)
    rows = c.fetchall()
    conn.close()
    return rows


def delete_product(product_id):
    conn = get_connection()
    c = conn.cursor()
    c.execute("DELETE FROM products WHERE id = ?", (product_id,))
    deleted = c.rowcount
    conn.commit()
    conn.close()
    return deleted


def delete_expired():
    today = datetime.now().strftime("%Y-%m-%d")
    conn = get_connection()
    c = conn.cursor()
    c.execute("DELETE FROM products WHERE expiry_date IS NOT NULL AND expiry_date < ?", (today,))
    deleted = c.rowcount
    conn.commit()
    conn.close()
    return deleted


def delete_all():
    conn = get_connection()
    c = conn.cursor()
    c.execute("DELETE FROM products")
    deleted = c.rowcount
    conn.commit()
    conn.close()
    return deleted