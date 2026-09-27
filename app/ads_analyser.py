"""Parses a Meta (Facebook) Ads Manager CSV export and computes per-row
metrics + simple discipline-style flags. Pure functions only — no I/O, no
DB — same convention as app/kdp.py, tested the same way in
tests/test_ads_analyser.py.

Meta's own export column names vary by export preset/currency, so parsing
matches against known header variants rather than one fixed set. A Google
Ads (or other platform) export would plug in as a sibling
`parse_google_ads_csv` that produces the same normalized row shape
(name/spend/impressions/clicks/results/revenue) — compute_metrics and
flag_rows below don't know or care which platform a row came from.
"""

import csv
import io

# Each canonical field maps to the header names Meta actually uses across
# its various export presets, checked case-insensitively. First match wins.
NAME_COLUMNS = ["Ad set name", "Campaign name", "Ad name"]
SPEND_COLUMNS = ["Amount spent (USD)", "Amount spent"]
IMPRESSIONS_COLUMNS = ["Impressions"]
CLICKS_COLUMNS = ["Link clicks", "Clicks (all)", "Clicks"]
RESULTS_COLUMNS = ["Results"]
REVENUE_COLUMNS = [
    "Purchases conversion value",
    "Website purchases conversion value",
    "Purchase conversion value",
]

# A row needs at least these four to be usable at all — everything else
# (results/revenue, and therefore CPA/ROAS) is optional per row.
REQUIRED_FIELDS = {
    "name": NAME_COLUMNS,
    "spend": SPEND_COLUMNS,
    "impressions": IMPRESSIONS_COLUMNS,
    "clicks": CLICKS_COLUMNS,
}

# Deliberately flat v1 thresholds, like ai.XP_PER_RULE / main.DISCIPLINE_WINDOW
# — not tuned against real ad-account data yet, just named constants instead
# of magic numbers so they're easy to find and adjust later.
HIGH_SPEND_FLOOR_USD = 20.0
LOW_CTR_THRESHOLD = 0.01  # 1%
CPA_OUTLIER_MULTIPLIER = 2.0


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

    column_map: dict[str, str] = {}
    missing = []
    for field, candidates in REQUIRED_FIELDS.items():
        found = _find_column(headers, candidates)
        if found is None:
            missing.append(f'{field} (expected one of: {", ".join(candidates)})')
        else:
            column_map[field] = found
    if missing:
        raise ValueError(
            "This doesn't look like a Meta Ads Manager export — missing required "
            "column(s): " + "; ".join(missing)
        )

    results_column = _find_column(headers, RESULTS_COLUMNS)
    revenue_column = _find_column(headers, REVENUE_COLUMNS)

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
    """Adds a "flags" list to each row. CPA-outlier detection needs the
    account-wide average CPA, so this takes the whole set rather than one
    row at a time."""
    cpas = [r["cpa"] for r in rows_with_metrics if r["cpa"] is not None]
    avg_cpa = sum(cpas) / len(cpas) if cpas else None

    out = []
    for row in rows_with_metrics:
        flags = []
        if row["spend"] >= HIGH_SPEND_FLOOR_USD and row["ctr"] is not None and row["ctr"] < LOW_CTR_THRESHOLD:
            flags.append(
                f"High spend (${row['spend']:.2f}) with low CTR ({row['ctr'] * 100:.2f}%)"
            )
        if avg_cpa is not None and row["cpa"] is not None and row["cpa"] > avg_cpa * CPA_OUTLIER_MULTIPLIER:
            flags.append(
                f"CPA outlier: ${row['cpa']:.2f} vs account average ${avg_cpa:.2f}"
            )
        out.append({**row, "flags": flags})
    return out
