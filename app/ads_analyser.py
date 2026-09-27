"""Parses a Meta (Facebook) Ads Manager or Amazon Ads (Sponsored Products)
CSV export and computes per-row metrics + simple discipline-style flags.
Pure functions only — no I/O, no DB — same convention as app/kdp.py, tested
the same way in tests/test_ads_analyser.py.

parse_ads_csv is the one entry point main.py calls: it looks at the header
row and dispatches to parse_meta_ads_csv or parse_amazon_ads_csv, whichever
one's required columns are actually present — the user never picks a
platform. Both parsers produce the same normalized row shape
(name/spend/impressions/clicks/results/revenue), plus "acos" when the
export itself reports it natively (Amazon; see parse_amazon_ads_csv).
compute_metrics and flag_rows don't otherwise know or care which platform a
row came from. A third platform would plug in the same way: a sibling
parse_*_csv producing that same shape, registered in _PLATFORM_PARSERS.
"""

import csv
import io

# Each canonical field maps to the header names a platform actually uses
# across its various export presets, checked case-insensitively. First
# match wins. Meta and Amazon's own column names never actually collide on
# the field that matters for detection (spend) even though some of the
# supporting ones (name/impressions/clicks) happen to overlap.
META_NAME_COLUMNS = ["Ad set name", "Campaign name", "Ad name"]
META_SPEND_COLUMNS = ["Amount spent (USD)", "Amount spent"]
META_IMPRESSIONS_COLUMNS = ["Impressions"]
META_CLICKS_COLUMNS = ["Link clicks", "Clicks (all)", "Clicks"]
META_RESULTS_COLUMNS = ["Results"]
META_REVENUE_COLUMNS = [
    "Purchases conversion value",
    "Website purchases conversion value",
    "Purchase conversion value",
]

# Amazon's Sponsored Products "Ad" report — confirmed against a real export
# (State, Ad name, Status code, Status, ASIN, SKU, Impressions, Clicks, CTR,
# Total cost (USD), CPC (USD), Purchases, Sales (USD), ACOS, KENP read,
# Estimated KENP royalties (USD)). "Campaign Name"/"Portfolio name" variants
# are included for the campaign/portfolio-level report presets, which use
# the same column family but weren't in the sample export checked.
AMAZON_NAME_COLUMNS = ["Ad name", "Campaign Name", "Campaign name", "Portfolio name"]
AMAZON_SPEND_COLUMNS = ["Total cost (USD)", "Spend (USD)", "Cost (USD)"]
AMAZON_IMPRESSIONS_COLUMNS = ["Impressions"]
AMAZON_CLICKS_COLUMNS = ["Clicks"]
AMAZON_RESULTS_COLUMNS = ["Purchases", "Orders", "Units ordered"]
AMAZON_REVENUE_COLUMNS = ["Sales (USD)", "Total Sales (USD)", "7 Day Total Sales (USD)"]
AMAZON_ACOS_COLUMNS = ["ACOS", "ACOS (%)", "Total Advertising Cost of Sales (ACOS)"]

# A row needs at least these four to be usable at all — everything else
# (results/revenue, and therefore CPA/ROAS/ACOS) is optional per row.
META_REQUIRED_FIELDS = {
    "name": META_NAME_COLUMNS,
    "spend": META_SPEND_COLUMNS,
    "impressions": META_IMPRESSIONS_COLUMNS,
    "clicks": META_CLICKS_COLUMNS,
}
AMAZON_REQUIRED_FIELDS = {
    "name": AMAZON_NAME_COLUMNS,
    "spend": AMAZON_SPEND_COLUMNS,
    "impressions": AMAZON_IMPRESSIONS_COLUMNS,
    "clicks": AMAZON_CLICKS_COLUMNS,
}

# Deliberately flat v1 thresholds, like ai.XP_PER_RULE / main.DISCIPLINE_WINDOW
# — not tuned against real ad-account data yet, just named constants instead
# of magic numbers so they're easy to find and adjust later. Reused as-is for
# both the CPA-outlier check (Meta, or any row with no native ACOS) and the
# ACOS-outlier check (Amazon) — same "how many times worse than the account
# average" idea, just applied to whichever cost-efficiency metric that row has.
HIGH_SPEND_FLOOR_USD = 20.0
LOW_CTR_THRESHOLD = 0.01  # 1%
COST_OUTLIER_MULTIPLIER = 2.0


def _to_float(raw: str | None) -> float | None:
    if raw is None:
        return None
    cleaned = raw.strip().replace("$", "").replace(",", "").replace("%", "")
    if cleaned in ("", "-", "--"):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _find_column(headers: list[str], candidates: list[str]) -> str | None:
    lookup = {h.strip().lower(): h for h in headers}
    for candidate in candidates:
        if candidate.lower() in lookup:
            return lookup[candidate.lower()]
    return None


def _map_required_columns(headers: list[str], required_fields: dict[str, list[str]]):
    """Resolves each required field to an actual header, or collects a
    human-readable "missing" description for it. Shared by every platform
    parser (and by _detect_platform, which just checks whether the list
    comes back empty) so the "which columns does this format need" logic
    lives in exactly one place per platform, not duplicated per parser.
    """
    column_map: dict[str, str] = {}
    missing = []
    for field, candidates in required_fields.items():
        found = _find_column(headers, candidates)
        if found is None:
            missing.append(f'{field} (expected one of: {", ".join(candidates)})')
        else:
            column_map[field] = found
    return column_map, missing


def _detect_platform(headers: list[str]) -> str | None:
    if not _map_required_columns(headers, META_REQUIRED_FIELDS)[1]:
        return "meta"
    if not _map_required_columns(headers, AMAZON_REQUIRED_FIELDS)[1]:
        return "amazon"
    return None


def parse_meta_ads_csv(csv_bytes: bytes) -> list[dict]:
    """Turns a raw Meta Ads Manager CSV export into normalized rows:
    {"name", "spend", "impressions", "clicks", "results", "revenue"}.
    results/revenue are None when that column isn't in the export at all,
    or is blank for a given row — everything else is required.
    """
    try:
        text = csv_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("Couldn't read that file as text — is it a CSV export?")

    reader = csv.DictReader(io.StringIO(text))
    headers = reader.fieldnames
    if not headers:
        raise ValueError("The CSV has no header row — is this a Meta Ads Manager export?")

    column_map, missing = _map_required_columns(headers, META_REQUIRED_FIELDS)
    if missing:
        raise ValueError(
            "This doesn't look like a Meta Ads Manager export — missing required "
            "column(s): " + "; ".join(missing)
        )

    results_column = _find_column(headers, META_RESULTS_COLUMNS)
    revenue_column = _find_column(headers, META_REVENUE_COLUMNS)

    rows = []
    for raw_row in reader:
        name = (raw_row.get(column_map["name"]) or "").strip()
        if not name:
            continue  # Meta exports often end in a blank "Total"/summary row
        rows.append({
            "name": name,
            "spend": _to_float(raw_row.get(column_map["spend"])) or 0.0,
            "impressions": int(_to_float(raw_row.get(column_map["impressions"])) or 0),
            "clicks": int(_to_float(raw_row.get(column_map["clicks"])) or 0),
            "results": _to_float(raw_row.get(results_column)) if results_column else None,
            "revenue": _to_float(raw_row.get(revenue_column)) if revenue_column else None,
        })

    if not rows:
        raise ValueError("No data rows found in that CSV.")
    return rows


def parse_amazon_ads_csv(csv_bytes: bytes) -> list[dict]:
    """Turns a raw Amazon Ads (Sponsored Products) CSV export into the same
    normalized row shape parse_meta_ads_csv produces — {"name", "spend",
    "impressions", "clicks", "results", "revenue"} — plus "acos": Amazon's
    own reported Advertising Cost of Sales for that row, used as-is rather
    than re-derived from spend/revenue (see compute_metrics/flag_rows).
    "results"/"revenue" map to Amazon's "Purchases"/"Sales (USD)" columns —
    the same underlying concepts (order count, resulting revenue) as Meta's
    Results/purchase-conversion-value, just Amazon's own names for them.
    """
    try:
        text = csv_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("Couldn't read that file as text — is it a CSV export?")

    reader = csv.DictReader(io.StringIO(text))
    headers = reader.fieldnames
    if not headers:
        raise ValueError("The CSV has no header row — is this an Amazon Ads export?")

    column_map, missing = _map_required_columns(headers, AMAZON_REQUIRED_FIELDS)
    if missing:
        raise ValueError(
            "This doesn't look like an Amazon Ads export — missing required "
            "column(s): " + "; ".join(missing)
        )

    results_column = _find_column(headers, AMAZON_RESULTS_COLUMNS)
    revenue_column = _find_column(headers, AMAZON_REVENUE_COLUMNS)
    acos_column = _find_column(headers, AMAZON_ACOS_COLUMNS)

    rows = []
    for raw_row in reader:
        name = (raw_row.get(column_map["name"]) or "").strip()
        if not name:
            continue  # bulk exports commonly end in a blank/"Total" summary row
        row = {
            "name": name,
            "spend": _to_float(raw_row.get(column_map["spend"])) or 0.0,
            "impressions": int(_to_float(raw_row.get(column_map["impressions"])) or 0),
            "clicks": int(_to_float(raw_row.get(column_map["clicks"])) or 0),
            "results": _to_float(raw_row.get(results_column)) if results_column else None,
            "revenue": _to_float(raw_row.get(revenue_column)) if revenue_column else None,
        }
        if acos_column:
            # Amazon's bulk-sheet ACOS is a plain percentage number (e.g. "34.52"
            # meaning 34.52%), with or without a literal "%" — normalized to a
            # fraction (0.3452) to match ctr/roas elsewhere in this module. Not
            # verified against a real non-zero ACOS value (the sample export
            # checked had no spend yet), so this is the standard Amazon Ads
            # convention, flagged here in case a real export proves otherwise.
            acos_raw = _to_float(raw_row.get(acos_column))
            row["acos"] = acos_raw / 100 if acos_raw is not None else None
        rows.append(row)

    if not rows:
        raise ValueError("No data rows found in that CSV.")
    return rows


_PLATFORM_PARSERS = {
    "meta": parse_meta_ads_csv,
    "amazon": parse_amazon_ads_csv,
}


def parse_ads_csv(csv_bytes: bytes) -> list[dict]:
    """Auto-detects the platform from the CSV's header row and dispatches
    to the matching parser — the user never picks a platform. A CSV that
    matches neither format's required columns gets a single error listing
    what's missing for both, same "clear message, never a crash" contract
    each individual parser already has for its own format.
    """
    try:
        text = csv_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("Couldn't read that file as text — is it a CSV export?")

    headers = csv.DictReader(io.StringIO(text)).fieldnames
    if not headers:
        raise ValueError("The CSV has no header row — is this a Meta or Amazon Ads export?")

    platform = _detect_platform(headers)
    if platform is None:
        _, meta_missing = _map_required_columns(headers, META_REQUIRED_FIELDS)
        _, amazon_missing = _map_required_columns(headers, AMAZON_REQUIRED_FIELDS)
        raise ValueError(
            "This doesn't look like a Meta Ads Manager or Amazon Ads export. "
            "Missing for Meta: " + "; ".join(meta_missing) + ". "
            "Missing for Amazon: " + "; ".join(amazon_missing) + "."
        )
    return _PLATFORM_PARSERS[platform](csv_bytes)


def compute_metrics(rows: list[dict]) -> list[dict]:
    """Adds ctr/cpc/cpa/roas to each row. cpa/roas are only computed when
    that row has results/revenue data — never inferred."""
    out = []
    for row in rows:
        ctr = row["clicks"] / row["impressions"] if row["impressions"] > 0 else None
        cpc = row["spend"] / row["clicks"] if row["clicks"] > 0 else None
        cpa = (
            row["spend"] / row["results"]
            if row["results"] is not None and row["results"] > 0
            else None
        )
        roas = (
            row["revenue"] / row["spend"]
            if row["revenue"] is not None and row["spend"] > 0
            else None
        )
        out.append({**row, "ctr": ctr, "cpc": cpc, "cpa": cpa, "roas": roas})
    return out


def flag_rows(rows_with_metrics: list[dict]) -> list[dict]:
    """Adds a "flags" list to each row. The cost-efficiency outlier check
    uses ACOS instead of CPA for a row that has Amazon's native ACOS —
    they're the same "how inefficient is this row" idea, just Amazon's own
    metric instead of one we'd otherwise derive — and CPA for every other
    row (Meta, or Amazon without ACOS data), unchanged from before. Each
    check needs its own account-wide average, so this takes the whole set
    rather than one row at a time; a single-platform upload (the normal
    case — one CSV is always one platform, never mixed) means only one of
    the two averages ends up populated in practice.
    """
    cpa_rows = [r for r in rows_with_metrics if r.get("acos") is None]
    acos_rows = [r for r in rows_with_metrics if r.get("acos") is not None]

    cpas = [r["cpa"] for r in cpa_rows if r["cpa"] is not None]
    avg_cpa = sum(cpas) / len(cpas) if cpas else None

    acoses = [r["acos"] for r in acos_rows if r["acos"] is not None]
    avg_acos = sum(acoses) / len(acoses) if acoses else None

    out = []
    for row in rows_with_metrics:
        flags = []
        if row["spend"] >= HIGH_SPEND_FLOOR_USD and row["ctr"] is not None and row["ctr"] < LOW_CTR_THRESHOLD:
            flags.append(
                f"High spend (${row['spend']:.2f}) with low CTR ({row['ctr'] * 100:.2f}%)"
            )

        acos = row.get("acos")
        if acos is not None:
            if avg_acos is not None and acos > avg_acos * COST_OUTLIER_MULTIPLIER:
                flags.append(
                    f"ACOS outlier: {acos * 100:.2f}% vs account average {avg_acos * 100:.2f}%"
                )
        elif avg_cpa is not None and row["cpa"] is not None and row["cpa"] > avg_cpa * COST_OUTLIER_MULTIPLIER:
            flags.append(
                f"CPA outlier: ${row['cpa']:.2f} vs account average ${avg_cpa:.2f}"
            )
        out.append({**row, "flags": flags})
    return out
