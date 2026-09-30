"""Pure break-even math for KDP authors — no I/O, no DB, just arithmetic."""

import math

# Sanity ceilings, not KDP eligibility rules: 650 MB is KDP's own max ebook
# file size; $1000 is far above any real list price and only exists to stop
# typo-sized inputs ($99999) from producing a confident-looking result.
MAX_LIST_PRICE = 1000
MAX_FILE_SIZE_MB = 650

EBOOK_PLAN_RATES = {"70": 0.70, "35": 0.35}


def calculate_kdp_breakeven(
    format: str,
    list_price: float,
    plan: str | None = None,
    royalty_rate: float | None = None,
    file_size_mb: float | None = None,
    delivery_fee_per_mb: float | None = None,
    printing_cost: float | None = None,
    vat: float = 0.0,
) -> dict:
    for name, value in (("list_price", list_price), ("file_size_mb", file_size_mb),
                        ("delivery_fee_per_mb", delivery_fee_per_mb), ("printing_cost", printing_cost),
                        ("royalty_rate", royalty_rate), ("vat", vat)):
        if value is not None and not math.isfinite(value):
            raise ValueError(f"{name} must be a finite number")
    if list_price <= 0:
        raise ValueError("List price must be above $0.")
    if list_price > MAX_LIST_PRICE:
        raise ValueError(f"List price must be ${MAX_LIST_PRICE} or less.")
    if file_size_mb is not None and file_size_mb > MAX_FILE_SIZE_MB:
        raise ValueError(f"File size must be {MAX_FILE_SIZE_MB} MB or less (KDP's maximum).")
    if any(v is not None and v < 0 for v in (file_size_mb, delivery_fee_per_mb, printing_cost, royalty_rate, vat)):
        raise ValueError("file size, delivery fee, printing cost, royalty rate and VAT can't be negative")

    if format == "ebook":
        if plan not in EBOOK_PLAN_RATES:
            raise ValueError('ebook plan must be "70" or "35"')
        rate = EBOOK_PLAN_RATES[plan]
        # KDP only charges a delivery fee on the 70% royalty plan — the 35%
        # plan never has one, regardless of file size.
        #
        # Formula is Amazon's own (kdp.amazon.com/en_US/help/topic/G200634500):
        #   royalty = rate x (list price - VAT - delivery cost)
        # i.e. the 70% is applied AFTER delivery cost comes off, not before.
        # Rounding rule (also from that page): file size is rounded UP to the
        # nearest kilobyte (1 MB = 1024 KB here) — not to the nearest MB — and
        # the final royalty is rounded to 2 decimals (done in the return below).
        size_mb = math.ceil(round((file_size_mb or 0) * 1024, 6)) / 1024
        delivery_cost = size_mb * (delivery_fee_per_mb or 0) if plan == "70" else 0.0
        royalty = rate * (list_price - vat - delivery_cost)
        if royalty < 0:
            raise ValueError("Delivery cost is higher than the list price — royalty would be negative.")
    elif format == "paperback":
        if royalty_rate is None:
            raise ValueError("royalty_rate is required for paperback")
        royalty = royalty_rate * list_price - (printing_cost or 0)
    else:
        raise ValueError('format must be "ebook" or "paperback"')

    breakeven_acos = royalty / list_price
    return {
        "royalty_per_sale": round(royalty, 2),
        "breakeven_acos": round(breakeven_acos, 4),
        "max_cpc_5pct": round(royalty * 0.05, 2),
        "max_cpc_10pct": round(royalty * 0.10, 2),
        "max_cpc_15pct": round(royalty * 0.15, 2),
    }
