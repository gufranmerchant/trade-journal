import pytest

from app.ads_analyser import (
    compute_metrics,
    flag_rows,
    parse_ads_csv,
    parse_amazon_ads_csv,
    parse_meta_ads_csv,
)

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


# ---------------------------------------------------------------------
# Amazon Ads (Sponsored Products) — column names modeled on a real
# Sponsored Products "Ad" report export (State, Ad name, Status code,
# Status, ASIN, SKU, Impressions, Clicks, CTR, Total cost (USD),
# CPC (USD), Purchases, Sales (USD), ACOS, KENP read, Estimated KENP
# royalties (USD)); campaign names/ASINs/numbers below are fabricated,
# not real account data.
# ---------------------------------------------------------------------

AMAZON_HEADER = (
    "State,Ad name,Status code,Status,ASIN,SKU,Impressions,Clicks,CTR,"
    "Total cost (USD),CPC (USD),Purchases,Sales (USD),ACOS,KENP read,"
    "Estimated KENP royalties (USD)\n"
)

AMAZON_VALID_CSV = (
    AMAZON_HEADER +
    "ENABLED,Sample Book Title One,AD_STATUS_LIVE,Delivering,B0FAKE0001,B0FAKE0001,"
    "10000,50,0.5,50.00,1.00,5,75.00,66.67,0,0\n"
    "ENABLED,Sample Book Title Two,AD_STATUS_LIVE,Delivering,B0FAKE0002,B0FAKE0002,"
    "5000,200,4.0,100.00,0.50,2,50.00,200.00,0,0\n"
    "ENABLED,Sample Book Title Three,AD_STATUS_LIVE,Delivering,B0FAKE0003,B0FAKE0003,"
    "3000,150,5.0,30.00,0.20,6,180.00,16.67,0,0\n"
).encode("utf-8")

AMAZON_NO_CONVERSIONS_CSV = (
    "State,Ad name,Status code,Status,ASIN,SKU,Impressions,Clicks,CTR,"
    "Total cost (USD),CPC (USD),KENP read,Estimated KENP royalties (USD)\n"
    "ENABLED,Sample Book Title Four,AD_STATUS_LIVE,Delivering,B0FAKE0004,B0FAKE0004,"
    "2000,40,2.0,25.00,0.63,0,0\n"
).encode("utf-8")


def test_parses_valid_amazon_export():
    rows = parse_amazon_ads_csv(AMAZON_VALID_CSV)
    assert [r["name"] for r in rows] == [
        "Sample Book Title One", "Sample Book Title Two", "Sample Book Title Three",
    ]
    assert rows[0]["spend"] == 50.0
    assert rows[0]["impressions"] == 10000
    assert rows[0]["clicks"] == 50
    assert rows[0]["results"] == 5
    assert rows[0]["revenue"] == 75.0
    assert rows[0]["acos"] == pytest.approx(0.6667)


def test_amazon_missing_required_columns_raises():
    with pytest.raises(ValueError):
        parse_amazon_ads_csv(MISSING_COLUMNS_CSV)


def test_amazon_empty_file_raises():
    with pytest.raises(ValueError):
        parse_amazon_ads_csv(b"")


def test_amazon_compute_metrics_ctr_cpc_cpa():
    rows = compute_metrics(parse_amazon_ads_csv(AMAZON_VALID_CSV))
    a = rows[0]
    assert a["ctr"] == pytest.approx(0.005)
    assert a["cpc"] == pytest.approx(1.0)
    assert a["cpa"] == pytest.approx(10.0)  # spend/purchases, same formula as Meta


def test_amazon_compute_metrics_without_conversion_data_leaves_acos_and_cpa_none():
    rows = compute_metrics(parse_amazon_ads_csv(AMAZON_NO_CONVERSIONS_CSV))
    assert rows[0].get("acos") is None
    assert rows[0]["cpa"] is None


def test_amazon_flags_high_spend_low_ctr():
    rows = flag_rows(compute_metrics(parse_amazon_ads_csv(AMAZON_VALID_CSV)))
    a = next(r for r in rows if r["name"] == "Sample Book Title One")
    assert any("low CTR" in f for f in a["flags"])


def test_amazon_flags_acos_outlier_not_cpa_outlier():
    rows = flag_rows(compute_metrics(parse_amazon_ads_csv(AMAZON_VALID_CSV)))
    b = next(r for r in rows if r["name"] == "Sample Book Title Two")
    assert any("ACOS outlier" in f for f in b["flags"])
    assert not any("CPA outlier" in f for f in b["flags"])


def test_amazon_unflagged_row_has_no_flags():
    rows = flag_rows(compute_metrics(parse_amazon_ads_csv(AMAZON_VALID_CSV)))
    c = next(r for r in rows if r["name"] == "Sample Book Title Three")
    assert c["flags"] == []


def test_parse_ads_csv_auto_detects_meta():
    rows = parse_ads_csv(VALID_CSV)
    assert [r["name"] for r in rows] == ["Ad Set A", "Ad Set B", "Ad Set C"]
    assert "acos" not in rows[0]


def test_parse_ads_csv_auto_detects_amazon():
    rows = parse_ads_csv(AMAZON_VALID_CSV)
    assert [r["name"] for r in rows] == [
        "Sample Book Title One", "Sample Book Title Two", "Sample Book Title Three",
    ]
    assert rows[0]["acos"] == pytest.approx(0.6667)


def test_parse_ads_csv_unrecognized_format_raises_with_both_platforms_mentioned():
    with pytest.raises(ValueError) as exc_info:
        parse_ads_csv(MISSING_COLUMNS_CSV)
    message = str(exc_info.value)
    assert "Meta" in message
    assert "Amazon" in message
