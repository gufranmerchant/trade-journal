import pytest

from app.ads_analyser import compute_metrics, flag_rows, parse_meta_ads_csv

VALID_CSV = (
    "Ad set name,Amount spent (USD),Impressions,Link clicks,Results\n"
    "Ad Set A,50.00,10000,50,5\n"
    "Ad Set B,100.00,5000,200,2\n"
    "Ad Set C,30.00,3000,150,3\n"
).encode("utf-8")

MISSING_COLUMNS_CSV = (
    "Campaign name,Amount spent (USD)\n"
    "Campaign A,50.00\n"
).encode("utf-8")

NO_CONVERSIONS_CSV = (
    "Campaign name,Amount spent (USD),Impressions,Link clicks\n"
    "Campaign A,25.00,2000,40\n"
).encode("utf-8")

WITH_REVENUE_CSV = (
    "Campaign name,Amount spent (USD),Impressions,Link clicks,Results,Purchases conversion value\n"
    "Campaign A,40.00,4000,80,4,200.00\n"
).encode("utf-8")


def test_parses_valid_export():
    rows = parse_meta_ads_csv(VALID_CSV)
    assert [r["name"] for r in rows] == ["Ad Set A", "Ad Set B", "Ad Set C"]
    assert rows[0]["spend"] == 50.0
    assert rows[0]["impressions"] == 10000
    assert rows[0]["clicks"] == 50
    assert rows[0]["results"] == 5
    assert rows[0]["revenue"] is None


def test_missing_required_columns_raises():
    with pytest.raises(ValueError):
        parse_meta_ads_csv(MISSING_COLUMNS_CSV)


def test_empty_file_raises():
    with pytest.raises(ValueError):
        parse_meta_ads_csv(b"")


def test_compute_metrics_ctr_cpc_cpa():
    rows = compute_metrics(parse_meta_ads_csv(VALID_CSV))
    a = rows[0]
    assert a["ctr"] == pytest.approx(0.005)
    assert a["cpc"] == pytest.approx(1.0)
    assert a["cpa"] == pytest.approx(10.0)


def test_compute_metrics_without_conversion_data_leaves_cpa_none():
    rows = compute_metrics(parse_meta_ads_csv(NO_CONVERSIONS_CSV))
    assert rows[0]["cpa"] is None
    assert rows[0]["roas"] is None


def test_compute_metrics_roas_when_revenue_present():
    rows = compute_metrics(parse_meta_ads_csv(WITH_REVENUE_CSV))
    assert rows[0]["roas"] == pytest.approx(5.0)  # 200 revenue / 40 spend


def test_flags_high_spend_low_ctr():
    rows = flag_rows(compute_metrics(parse_meta_ads_csv(VALID_CSV)))
    a = next(r for r in rows if r["name"] == "Ad Set A")
    assert any("low CTR" in f for f in a["flags"])


def test_flags_cpa_outlier():
    rows = flag_rows(compute_metrics(parse_meta_ads_csv(VALID_CSV)))
    b = next(r for r in rows if r["name"] == "Ad Set B")
    assert any("CPA outlier" in f for f in b["flags"])


def test_unflagged_row_has_no_flags():
    rows = flag_rows(compute_metrics(parse_meta_ads_csv(VALID_CSV)))
    c = next(r for r in rows if r["name"] == "Ad Set C")
    assert c["flags"] == []
