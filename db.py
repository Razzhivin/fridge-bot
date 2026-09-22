import sqlite3
from datetime import datetime, timedelta

DB_PATH = "fridge.db"

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


def add_product(name, quantity, unit, price, category, purchase_date=None):
    category_key = (category or "").strip().lower()
    days = EXPIRY_DAYS.get(category_key, 7)

    base_date = datetime.now()
    if purchase_date:
        normalized = purchase_date.strip()
        for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y", "%Y/%m/%d", "%d/%m/%Y"):
            try:
                base_date = datetime.strptime(normalized, fmt)
                break
            except ValueError:
                continue
        else:
            base_date = datetime.now()

    expiry = None
    if days is not None:
        expiry = (base_date + timedelta(days=days)).strftime("%Y-%m-%d")

    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        INSERT INTO products (name, quantity, unit, price, category, expiry_date, purchase_date)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (name, quantity, unit, price, category, expiry, purchase_date or None))
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