import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta

import db


class DbDateLogicTests(unittest.TestCase):
    def setUp(self):
        self.original_db = db.DB_PATH
        fd, path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        db.DB_PATH = path
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self.original_db
        try:
            os.remove(db.DB_PATH)
        except FileNotFoundError:
            pass

    def test_purchase_date_is_used_for_expiry(self):
        purchase_date = "2026-09-15"
        db.add_product(
            name="Йогурт",
            quantity=1,
            unit="шт",
            price=60,
            category="йогурт",
            purchase_date=purchase_date,
        )

        row = db.get_fridge()[0]
        self.assertEqual(row[6], (datetime.strptime(purchase_date, "%Y-%m-%d") + timedelta(days=5)).strftime("%Y-%m-%d"))

    def test_precise_category_has_specific_expiry(self):
        db.add_product(
            name="Творог",
            quantity=250,
            unit="г",
            price=120,
            category="творог",
            purchase_date="2026-09-10",
        )

        row = db.get_fridge()[0]
        self.assertEqual(row[6], (datetime.strptime("2026-09-10", "%Y-%m-%d") + timedelta(days=7)).strftime("%Y-%m-%d"))


if __name__ == "__main__":
    unittest.main()
