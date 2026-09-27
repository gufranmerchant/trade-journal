"""Pure break-even math for KDP authors — no I/O, no DB, just arithmetic."""

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
    if format == "ebook":
        if plan not in EBOOK_PLAN_RATES:
            raise ValueError('ebook plan must be "70" or "35"')
        rate = EBOOK_PLAN_RATES[plan]
        # KDP only charges a delivery fee on the 70% royalty plan — the 35%
        # plan never has one, regardless of file size.
        delivery_cost = (file_size_mb or 0) * (delivery_fee_per_mb or 0) if plan == "70" else 0.0
        royalty = rate * (list_price - vat - delivery_cost)
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
