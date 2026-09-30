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


# Hand-calculated with Amazon's own formula (kdp.amazon.com/en_US/help/topic/G200634500):
#   royalty = 0.70 x (list price - delivery cost), delivery = $0.15/MB
# (file sizes below are exact in KB, so the round-up-to-nearest-KB rule is a no-op).
@pytest.mark.parametrize("price, mb, expected", [
    (4.99, 1.5, 3.34),    # 0.7 x (4.99 - 0.225)  = 3.3355
    (4.99, 10.0, 2.44),   # 0.7 x (4.99 - 1.50)   = 2.443
    (2.99, 0.5, 2.04),    # 0.7 x (2.99 - 0.075)  = 2.0405 -> 2.04
    (9.99, 3.0, 6.68),    # 0.7 x (9.99 - 0.45)   = 6.678
    (6.99, 2.0, 4.68),    # 0.7 x (6.99 - 0.30)   = 4.683
])
def test_ebook_70_hand_calculated(price, mb, expected):
    result = calculate_kdp_breakeven(format="ebook", list_price=price, plan="70",
                                     file_size_mb=mb, delivery_fee_per_mb=0.15)
    assert result["royalty_per_sale"] == expected


def test_ebook_70_rounds_file_size_up_to_nearest_kb():
    # 1.0001 MB = 1024.1 KB -> billed as 1025 KB = 1.0009765625 MB.
    result = calculate_kdp_breakeven(format="ebook", list_price=4.99, plan="70",
                                     file_size_mb=1.0001, delivery_fee_per_mb=0.15)
    assert result["royalty_per_sale"] == pytest.approx(0.7 * (4.99 - 1025 / 1024 * 0.15), abs=0.005)


@pytest.mark.parametrize("kwargs", [
    dict(list_price=-5), dict(list_price=0), dict(list_price=float("nan")),
    dict(list_price=4.99, file_size_mb=-1), dict(list_price=4.99, file_size_mb=500),
])
def test_ebook_70_invalid_inputs_raise(kwargs):
    kwargs.setdefault("file_size_mb", 1.5)
    with pytest.raises(ValueError):
        calculate_kdp_breakeven(format="ebook", plan="70", delivery_fee_per_mb=0.15, **kwargs)
