import unittest

from codex_probe.range_sum import inclusive_range_sum


class InclusiveRangeSumTests(unittest.TestCase):
    def test_the_endpoint_is_included(self):
        self.assertEqual(inclusive_range_sum(1, 4), 10)

    def test_a_single_value_range_returns_that_value(self):
        self.assertEqual(inclusive_range_sum(3, 3), 3)

    def test_negative_and_mixed_ranges(self):
        self.assertEqual(inclusive_range_sum(-2, 2), 0)

    def test_an_empty_range_sums_to_zero(self):
        self.assertEqual(inclusive_range_sum(5, 4), 0)


if __name__ == "__main__":
    unittest.main()
