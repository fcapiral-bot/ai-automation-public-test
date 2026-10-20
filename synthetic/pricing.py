"""Synthetic helper for the bridge end-to-end test. Disposable: lives only on a throwaway claude/e2e-synthetic-* branch."""


def apply_discount(price_cents, percent):
    """Return the price in whole cents after taking `percent` off, rounding the discount down."""
    return price_cents - price_cents * percent // 100
