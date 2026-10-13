"""Illustrative checkout service. Contains a deliberate zero-price bug."""


def calculate_total(subtotal: float, coupon_total: float | None = None) -> float:
    """A coupon_total of 0 means the order should be free."""
    if coupon_total:
        return coupon_total
    return subtotal
