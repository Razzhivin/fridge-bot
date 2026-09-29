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

    def test_photo_quota_is_persistent_and_atomic(self):
        self.assertTrue(db.consume_photo_quota(self.USER_A))
        self.assertTrue(db.consume_photo_quota(self.USER_A))
        self.assertTrue(db.consume_photo_quota(self.USER_A))
        self.assertTrue(db.consume_photo_quota(self.USER_A))
        self.assertFalse(db.consume_photo_quota(self.USER_A))

        connection = db.get_connection()
        rows = connection.execute(
            "SELECT period_type, used_count FROM usage_counters WHERE user_id = ? ORDER BY period_type",
            (self.USER_A,),
        ).fetchall()
        connection.close()
        self.assertEqual(rows, [("total", 4)])

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


class SubscriptionTests(unittest.TestCase):
    USER = 123456

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

    def test_free_user_can_process(self):
        self.assertTrue(db.can_process_receipt(self.USER))

    def test_free_user_blocked_after_4_receipts(self):
        db.consume_photo_quota(self.USER)
        db.consume_photo_quota(self.USER)
        db.consume_photo_quota(self.USER)
        db.consume_photo_quota(self.USER)
        self.assertFalse(db.can_process_receipt(self.USER))

    def test_subscribed_user_can_process_after_limit(self):
        db.consume_photo_quota(self.USER)
        db.consume_photo_quota(self.USER)
        db.consume_photo_quota(self.USER)
        db.consume_photo_quota(self.USER)
        db.activate_subscription(self.USER, amount=50, currency="XTR", payment_id="charge_1")
        self.assertTrue(db.can_process_receipt(self.USER))

    def test_unsubscribed_user_with_expired_subscription_cannot_process(self):
        conn = db.get_connection()
        c = conn.cursor()
        past = (datetime.now().date() - timedelta(days=1)).strftime("%Y-%m-%d")
        c.execute("""
            INSERT INTO payments (user_id, status, amount, currency, payment_id, paid_at, paid_until)
            VALUES (?, 'paid', 50, 'XTR', 'old_charge', ?, ?)
        """, (self.USER, past, past))
        conn.commit()
        conn.close()
        db.cleanup_expired_subscriptions()
        self.assertFalse(db.has_active_subscription(self.USER))

    def test_activate_subscription_sets_paid_until(self):
        paid_until = db.activate_subscription(self.USER, amount=50, currency="XTR", payment_id="charge_1")
        expected = (datetime.now().date() + timedelta(days=db.SUBSCRIPTION_DAYS)).strftime("%Y-%m-%d")
        self.assertEqual(paid_until, expected)

    def test_get_subscription_status_returns_active(self):
        db.activate_subscription(self.USER, amount=50, currency="XTR", payment_id="charge_1")
        status = db.get_subscription_status(self.USER)
        self.assertIsNotNone(status)
        self.assertEqual(status["status"], "paid")
        self.assertEqual(status["amount"], 50)
        self.assertEqual(status["currency"], "XTR")
        self.assertEqual(status["transaction_id"], "charge_1")

    def test_get_subscription_status_returns_none_when_no_payment(self):
        status = db.get_subscription_status(self.USER)
        self.assertIsNone(status)

    def test_cleanup_expired_subscriptions(self):
        conn = db.get_connection()
        c = conn.cursor()
        past = (datetime.now().date() - timedelta(days=1)).strftime("%Y-%m-%d")
        c.execute("""
            INSERT INTO payments (user_id, status, amount, currency, payment_id, paid_at, paid_until)
            VALUES (?, 'paid', 50, 'XTR', 'old_charge', ?, ?)
        """, (self.USER, past, past))
        conn.commit()
        conn.close()

        db.cleanup_expired_subscriptions()

        conn = db.get_connection()
        row = conn.execute("SELECT status FROM payments WHERE user_id = ?", (self.USER,)).fetchone()
        conn.close()
        self.assertEqual(row[0], "expired")

    def test_multiple_subscriptions_stored(self):
        db.activate_subscription(self.USER, amount=50, currency="XTR", payment_id="charge_1")
        db.activate_subscription(self.USER, amount=50, currency="XTR", payment_id="charge_2")
        statuses = []
        conn = db.get_connection()
        for row in conn.execute("SELECT payment_id FROM payments WHERE user_id = ? ORDER BY paid_at", (self.USER,)):
            statuses.append(row[0])
        conn.close()
        self.assertEqual(len(statuses), 2)

if __name__ == "__main__":
    unittest.main()
