import unittest

from synthetic.pricing import apply_discount


class ApplyDiscountTests(unittest.TestCase):
    def test_ten_percent_off(self):
        self.assertEqual(apply_discount(1000, 10), 900)

    def test_no_discount_keeps_the_price(self):
        self.assertEqual(apply_discount(1000, 0), 1000)

    def test_everything_off_is_free(self):
        self.assertEqual(apply_discount(1000, 100), 0)

    def test_the_discount_rounds_down(self):
        self.assertEqual(apply_discount(999, 50), 500)


if __name__ == "__main__":
    unittest.main()
