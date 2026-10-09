"""Synthetic probe module for an automatic code-review test. Not used by anything."""


def inclusive_range_sum(start, end):
    """Return the sum of every integer from start to end, including both ends.

    inclusive_range_sum(1, 4) is expected to return 1 + 2 + 3 + 4 = 10.
    """
    total = 0
    for n in range(start, end):
        total += n
    return total
