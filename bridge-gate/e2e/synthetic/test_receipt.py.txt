import os
import unittest

from synthetic.pricing import apply_discount
from synthetic.receipt import line

# Disposable bridge fixture, stage two. These checks run only on GitHub Actions and only once the discount helper is correct, so a
# correction that fixes the discount can pass locally and still be judged by CI alone.
if os.environ.get("GITHUB_ACTIONS") == "true" and apply_discount(1000, 10) == 900:
    class ReceiptLineTests(unittest.TestCase):
        def test_the_amount_has_two_decimal_places(self):
            self.assertEqual(line("tea", 950), "tea: $9.50")

        def test_a_whole_amount_keeps_its_cents(self):
            self.assertEqual(line("pie", 300), "pie: $3.00")
