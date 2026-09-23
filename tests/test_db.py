import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta

import db


class DbDateLogicTests(unittest.TestCase):
    USER_A = 465246312
    USER_B = 987654321

    def setUp(self):
        self.original_db = db.DB_PATH
        fd, path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        db.DB_PATH = path
        db.init_db()

    def tearDown(self):
        test_db = db.DB_PATH
        db.DB_PATH = self.original_db
        try:
            os.remove(test_db)
        except FileNotFoundError:
            pass

    def test_purchase_date_is_used_for_expiry(self):
        purchase_date = "2026-09-15"
        db.add_product(
            self.USER_A,
            name="Йогурт",
            quantity=1,
            unit="шт",
            price=60,
            category="йогурт",
            purchase_date=purchase_date,
        )

        row = db.get_fridge(self.USER_A)[0]
        self.assertEqual(row[6], (datetime.strptime(purchase_date, "%Y-%m-%d") + timedelta(days=5)).strftime("%Y-%m-%d"))

    def test_precise_category_has_specific_expiry(self):
        db.add_product(
            self.USER_A,
            name="Творог",
            quantity=250,
            unit="г",
            price=120,
            category="творог",
            purchase_date="2026-09-10",
        )

        row = db.get_fridge(self.USER_A)[0]
        self.assertEqual(row[6], (datetime.strptime("2026-09-10", "%Y-%m-%d") + timedelta(days=7)).strftime("%Y-%m-%d"))

    def test_stale_purchase_date_falls_back_to_today(self):
        db.add_product(
            self.USER_A,
            name="Молоко",
            quantity=1,
            unit="л",
            price=100,
            category="молоко",
            purchase_date="2022-01-04",
        )

        row = db.get_fridge(self.USER_A)[0]
        today = datetime.now().strftime("%Y-%m-%d")
        self.assertEqual(row[7], today)
        self.assertEqual(row[6], (datetime.now() + timedelta(days=5)).strftime("%Y-%m-%d"))

    def test_users_have_separate_fridges(self):
        db.add_product(self.USER_A, "Молоко", 1, "л", 100, "молоко")
        db.add_product(self.USER_B, "Яблоки", 2, "кг", 200, "яблоки")

        self.assertEqual([row[1] for row in db.get_fridge(self.USER_A)], ["Молоко"])
        self.assertEqual([row[1] for row in db.get_fridge(self.USER_B)], ["Яблоки"])

    def test_user_cannot_delete_another_users_product(self):
        db.add_product(self.USER_A, "Молоко", 1, "л", 100, "молоко")
        product_id = db.get_fridge(self.USER_A)[0][0]

        self.assertEqual(db.delete_product(self.USER_B, product_id), 0)
        self.assertEqual(len(db.get_fridge(self.USER_A)), 1)

    def test_clear_only_removes_current_users_products(self):
        db.add_product(self.USER_A, "Молоко", 1, "л", 100, "молоко")
        db.add_product(self.USER_B, "Яблоки", 2, "кг", 200, "яблоки")

        self.assertEqual(db.delete_all(self.USER_A), 1)
        self.assertEqual(db.get_fridge(self.USER_A), [])
        self.assertEqual(len(db.get_fridge(self.USER_B)), 1)

    def test_expiration_alerts_are_isolated_and_deduplicated(self):
        today = datetime.now().date()
        connection = db.get_connection()
        connection.executemany(
            """
            INSERT INTO products
                (name, quantity, unit, category, expiry_date, purchase_date, user_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("Молоко", 1, "л", "молоко", (today + timedelta(days=2)).strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d"), self.USER_A),
                ("Рыба", 1, "кг", "рыба", (today - timedelta(days=1)).strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d"), self.USER_A),
                ("Сыр", 1, "кг", "сыр", (today + timedelta(days=2)).strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d"), self.USER_B),
            ],
        )
        connection.commit()
        connection.close()

        user_a_alerts = db.get_expiration_alerts(user_id=self.USER_A)
        user_b_alerts = db.get_expiration_alerts(user_id=self.USER_B)
        self.assertEqual([row[2] for row in user_a_alerts], ["Рыба", "Молоко"])
        self.assertEqual([row[2] for row in user_b_alerts], ["Сыр"])

        expired_id = user_a_alerts[0][0]
        self.assertEqual(db.mark_expiration_alert_sent(self.USER_A, expired_id, "expired"), 1)
        self.assertEqual(db.mark_expiration_alert_sent(self.USER_A, expired_id, "expired"), 0)
        self.assertEqual(len(db.get_expiration_alerts(user_id=self.USER_A)), 1)

    class DbMigrationTests(unittest.TestCase):
        def setUp(self):
            self.original_db = db.DB_PATH
            fd, self.test_db = tempfile.mkstemp(suffix=".db")
            os.close(fd)
            db.DB_PATH = self.test_db
            connection = sqlite3.connect(self.test_db)
            connection.execute("""
                CREATE TABLE products (
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
            connection.execute(
                "INSERT INTO products (name, quantity, unit, category) VALUES (?, ?, ?, ?)",
                ("Старый товар", 1, "шт", "бакалея"),
            )
            connection.commit()
            connection.close()

        def tearDown(self):
            db.DB_PATH = self.original_db
            try:
                os.remove(self.test_db)
            except FileNotFoundError:
                pass

        def test_legacy_rows_are_assigned_to_migration_user(self):
            db.init_db()
            rows = db.get_fridge(db.LEGACY_USER_ID)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][1], "Старый товар")

if __name__ == "__main__":
    unittest.main()
