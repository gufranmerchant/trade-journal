import pytest

from app.kdp import calculate_kdp_breakeven


def test_ebook_70_plan():
    result = calculate_kdp_breakeven(
        format="ebook",
        list_price=6.99,
        plan="70",
        file_size_mb=1.7,
        delivery_fee_per_mb=0.15,
    )
    assert result["royalty_per_sale"] == pytest.approx(4.71, abs=0.01)
    assert result["breakeven_acos"] == pytest.approx(0.67, abs=0.01)


def test_ebook_35_plan_has_no_delivery_cost():
    result = calculate_kdp_breakeven(
        format="ebook",
        list_price=6.99,
        plan="35",
        file_size_mb=1.7,
        delivery_fee_per_mb=0.15,
    )
    assert result["royalty_per_sale"] == pytest.approx(2.45, abs=0.01)
    # No cost is ever subtracted on the 35% plan, so ACOS == the rate exactly.
    assert result["breakeven_acos"] == pytest.approx(0.35, abs=0.0001)


def test_paperback():
    result = calculate_kdp_breakeven(
        format="paperback",
        list_price=9.99,
        royalty_rate=0.60,
        printing_cost=2.30,
    )
    assert result["royalty_per_sale"] == pytest.approx(3.69, abs=0.01)
    assert result["breakeven_acos"] == pytest.approx(0.37, abs=0.01)


def test_max_cpc_scales_with_conversion_rate():
    result = calculate_kdp_breakeven(
        format="paperback",
        list_price=9.99,
        royalty_rate=0.60,
        printing_cost=2.30,
    )
    royalty = result["royalty_per_sale"]
    assert result["max_cpc_5pct"] == pytest.approx(royalty * 0.05, abs=0.01)
    assert result["max_cpc_10pct"] == pytest.approx(royalty * 0.10, abs=0.01)
    assert result["max_cpc_15pct"] == pytest.approx(royalty * 0.15, abs=0.01)


def test_invalid_format_raises():
    with pytest.raises(ValueError):
        calculate_kdp_breakeven(format="audiobook", list_price=9.99)


def test_invalid_ebook_plan_raises():
    with pytest.raises(ValueError):
        calculate_kdp_breakeven(format="ebook", list_price=6.99, plan="50")
