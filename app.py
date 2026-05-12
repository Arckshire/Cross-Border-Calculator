"""
Cross-Border Shipment Border-Crossing Analyzer
==============================================
A Streamlit tool that ingests a Snowflake CSV export of US-MX cross-border
shipments (with position pings) and produces a 2-sheet Excel report:

    Sheet 1 — Per Shipment: one row per shipment with the border-crossing
              duration (or "missing data" + reason).
    Sheet 2 — Lane Statistics: per-lane aggregates (avg, median, min, max,
              p90, stddev) over the analyzable shipments.

Companion file: query.sql (the Snowflake query that produces the input).
"""

from __future__ import annotations

import io
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd
import streamlit as st


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Cross-Border Shipment Analyzer",
    layout="wide",
)

# Columns we expect (and that the Snowflake query produces). All UPPER_CASE.
SHIPMENT_LEVEL_COLS = [
    "SHIPMENT_ID", "SHIPMENT_LEG_ID", "BILL_OF_LADING", "ORDER_NUMBER",
    "TENANT_ID", "TENANT_NAME",
    "CARRIER_ID", "CARRIER_NAME", "SCAC",
    "PICKUP_LOCATION_NAME", "PICKUP_LOCATION_CITY", "PICKUP_LOCATION_STATE",
    "PICKUP_POSTAL_CODE", "PICKUP_COUNTRY_CODE",
    "DESTINATION_LOCATION_NAME", "DESTINATION_LOCATION_CITY",
    "DESTINATION_LOCATION_STATE", "DESTINATION_POSTAL_CODE",
    "DESTINATION_COUNTRY_CODE",
    "LANE", "TOTAL_STOPS",
    "TRACKING_METHOD", "TRACKING_TYPE", "IS_TRACKED",
    "RCA_PING_COUNT", "AVG_PING_FREQUENCY_MINS",
    "TRACKING_START_UTC_DT", "TRACKING_END_UTC_DT",
    "PICKUP_ARRIVAL_UTC_DT", "PICKUP_DEPARTURE_UTC_DT",
    "DESTINATION_ARRIVAL_UTC_DT", "DESTINATION_DEPARTURE_UTC_DT",
    "FINAL_STATUS", "FINAL_STATUS_REASON",
]

PING_COLS = [
    "PING_TIMESTAMP", "PING_LATITUDE", "PING_LONGITUDE",
    "PING_COUNTRY", "PING_CITY", "PING_STATE", "PING_SOURCE_TYPE",
]

DATETIME_COLS = [
    "TRACKING_START_UTC_DT", "TRACKING_END_UTC_DT",
    "PICKUP_ARRIVAL_UTC_DT", "PICKUP_DEPARTURE_UTC_DT",
    "DESTINATION_ARRIVAL_UTC_DT", "DESTINATION_DEPARTURE_UTC_DT",
    "PING_TIMESTAMP",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def fmt_duration(minutes: Optional[float]) -> str:
    """Format minutes as H:MM:SS string; return 'missing data' for NaN/None."""
    if minutes is None or pd.isna(minutes):
        return "missing data"
    total_seconds = int(round(minutes * 60))
    h = total_seconds // 3600
    m = (total_seconds % 3600) // 60
    s = total_seconds % 60
    return f"{h}:{m:02d}:{s:02d}"


def direction_for(origin_cc: str, dest_cc: str) -> Optional[str]:
    """Return 'MX -> US' / 'US -> MX' or None if not a US-MX cross-border."""
    if origin_cc == "MX" and dest_cc == "US":
        return "MX -> US"
    if origin_cc == "US" and dest_cc == "MX":
        return "US -> MX"
    return None


def compute_crossing(pings: pd.DataFrame, direction: str) -> dict:
    """
    Apply the "last country transition" algorithm to one shipment's pings.

    Returns a dict with:
        t_exit, t_enter            -> datetimes or None
        duration_minutes           -> float or NaN
        confidence                 -> 'HIGH' | 'MEDIUM' | 'LOW' | 'N/A'
        used_ping_count            -> int (non-null ping rows used)
        transitions_found          -> int (count of matching direction transitions)
        notes                      -> reason if missing
    """
    blank = dict(
        t_exit=None, t_enter=None,
        duration_minutes=np.nan,
        confidence="N/A",
        used_ping_count=0,
        transitions_found=0,
        notes="",
    )

    # Drop rows with no ping data (LEFT JOIN nulls)
    valid = pings.dropna(subset=["PING_TIMESTAMP", "PING_COUNTRY"])
    n = len(valid)

    if n == 0:
        return {**blank, "notes": "No position pings recorded for this shipment"}

    if n == 1:
        return {**blank, "used_ping_count": 1,
                "notes": "Only 1 position ping recorded — cannot compute crossing"}

    valid = valid.sort_values("PING_TIMESTAMP").reset_index(drop=True)

    if direction == "MX -> US":
        origin_cc, dest_cc = "MX", "US"
    elif direction == "US -> MX":
        origin_cc, dest_cc = "US", "MX"
    else:
        return {**blank, "used_ping_count": n, "notes": f"Unsupported direction: {direction}"}

    # Find every (origin -> dest) country transition between consecutive pings
    countries = valid["PING_COUNTRY"].tolist()
    timestamps = valid["PING_TIMESTAMP"].tolist()
    transitions = []
    for i in range(1, n):
        if countries[i - 1] == origin_cc and countries[i] == dest_cc:
            transitions.append((timestamps[i - 1], timestamps[i]))

    if not transitions:
        countries_seen = sorted(set(countries))
        return {**blank, "used_ping_count": n,
                "notes": (
                    f"No {origin_cc}->{dest_cc} transition in pings; "
                    f"countries observed: {', '.join(countries_seen)}"
                )}

    # Use the LAST transition — most reliable crossing point
    t_exit, t_enter = transitions[-1]
    duration_min = (t_enter - t_exit).total_seconds() / 60.0

    # Confidence rules:
    #   HIGH   = >=20 pings AND <=2 transitions
    #   MEDIUM = >=6 pings
    #   LOW    = otherwise (still computable; just sparse / multiple flips)
    if n >= 20 and len(transitions) <= 2:
        confidence = "HIGH"
    elif n >= 6:
        confidence = "MEDIUM"
    else:
        confidence = "LOW"

    return dict(
        t_exit=t_exit,
        t_enter=t_enter,
        duration_minutes=duration_min,
        confidence=confidence,
        used_ping_count=n,
        transitions_found=len(transitions),
        notes="",
    )


# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------
def process_dataframe(df_raw: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Group by shipment leg, compute per-shipment crossing, and build the
    per-lane statistics dataframe.

    Returns (per_shipment_df, per_lane_df).
    """
    df = df_raw.copy()

    # Parse datetimes
    for col in DATETIME_COLS:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce", utc=True)

    # Safety filter: cross-border US-MX only
    df["DIRECTION"] = df.apply(
        lambda r: direction_for(r.get("PICKUP_COUNTRY_CODE"),
                                r.get("DESTINATION_COUNTRY_CODE")),
        axis=1,
    )
    df = df[df["DIRECTION"].notna()].copy()

    if df.empty:
        return (
            pd.DataFrame(columns=["BILL_OF_LADING", "SHIPMENT_ID", "DIRECTION",
                                  "BORDER_CROSSING_DURATION"]),
            pd.DataFrame(columns=["DIRECTION", "LANE", "SHIPMENTS_ANALYZED"]),
        )

    # Group by leg (most unique unit). For each leg we have one shipment row
    # of metadata, repeated across its position pings.
    shipment_results = []
    grouped = df.groupby("SHIPMENT_LEG_ID", dropna=False, sort=False)

    progress = st.progress(0.0)
    total_groups = len(grouped)

    for i, (leg_id, group) in enumerate(grouped):
        # Take shipment-level metadata from the first row of the group
        meta = group.iloc[0]
        direction = meta["DIRECTION"]

        pings = group[PING_COLS].copy()
        result = compute_crossing(pings, direction)

        shipment_results.append({
            "BILL_OF_LADING": meta.get("BILL_OF_LADING"),
            "SHIPMENT_ID": meta.get("SHIPMENT_ID"),
            "ORDER_NUMBER": meta.get("ORDER_NUMBER"),
            "SHIPMENT_LEG_ID": leg_id,
            "CARRIER_NAME": meta.get("CARRIER_NAME"),
            "CARRIER_ID": meta.get("CARRIER_ID"),
            "SCAC": meta.get("SCAC"),
            "DIRECTION": direction,
            "ORIGIN_CITY": meta.get("PICKUP_LOCATION_CITY"),
            "ORIGIN_STATE": meta.get("PICKUP_LOCATION_STATE"),
            "ORIGIN_COUNTRY": meta.get("PICKUP_COUNTRY_CODE"),
            "ORIGIN_LOCATION_NAME": meta.get("PICKUP_LOCATION_NAME"),
            "DEST_CITY": meta.get("DESTINATION_LOCATION_CITY"),
            "DEST_STATE": meta.get("DESTINATION_LOCATION_STATE"),
            "DEST_COUNTRY": meta.get("DESTINATION_COUNTRY_CODE"),
            "DEST_LOCATION_NAME": meta.get("DESTINATION_LOCATION_NAME"),
            "LANE": meta.get("LANE"),
            "TRACKING_START_UTC": meta.get("TRACKING_START_UTC_DT"),
            "TRACKING_END_UTC": meta.get("TRACKING_END_UTC_DT"),
            "RCA_PING_COUNT": meta.get("RCA_PING_COUNT"),
            "USED_PING_COUNT": result["used_ping_count"],
            "TRANSITIONS_FOUND": result["transitions_found"],
            "T_EXIT_ORIGIN_COUNTRY": result["t_exit"],
            "T_ENTER_DEST_COUNTRY": result["t_enter"],
            "BORDER_CROSSING_MINUTES": result["duration_minutes"],
            "BORDER_CROSSING_DURATION": fmt_duration(result["duration_minutes"]),
            "CONFIDENCE": result["confidence"],
            "FINAL_STATUS": meta.get("FINAL_STATUS"),
            "TRACKING_METHOD": meta.get("TRACKING_METHOD"),
            "NOTES": result["notes"],
        })

        if (i + 1) % 50 == 0 or (i + 1) == total_groups:
            progress.progress((i + 1) / total_groups)

    progress.empty()

    per_shipment = pd.DataFrame(shipment_results)

    # ---------- Lane statistics ----------
    if per_shipment.empty:
        per_lane = pd.DataFrame()
    else:
        # Group keys: direction + origin/dest city/state
        group_keys = [
            "DIRECTION", "LANE",
            "ORIGIN_CITY", "ORIGIN_STATE", "ORIGIN_COUNTRY",
            "DEST_CITY", "DEST_STATE", "DEST_COUNTRY",
        ]

        # Total shipments per lane (including missing)
        totals = (
            per_shipment.groupby(group_keys, dropna=False)
            .size()
            .reset_index(name="SHIPMENTS_TOTAL")
        )

        # Stats over the analyzable subset
        analyzed = per_shipment[per_shipment["BORDER_CROSSING_MINUTES"].notna()]
        if analyzed.empty:
            stats = pd.DataFrame(columns=group_keys + [
                "SHIPMENTS_ANALYZED", "AVG_MINUTES", "MEDIAN_MINUTES",
                "MIN_MINUTES", "MAX_MINUTES", "P90_MINUTES", "STDDEV_MINUTES",
            ])
        else:
            stats = (
                analyzed.groupby(group_keys, dropna=False)["BORDER_CROSSING_MINUTES"]
                .agg(
                    SHIPMENTS_ANALYZED="count",
                    AVG_MINUTES="mean",
                    MEDIAN_MINUTES="median",
                    MIN_MINUTES="min",
                    MAX_MINUTES="max",
                    P90_MINUTES=lambda x: x.quantile(0.90),
                    STDDEV_MINUTES="std",
                )
                .reset_index()
            )

        per_lane = totals.merge(stats, on=group_keys, how="left")
        per_lane["SHIPMENTS_ANALYZED"] = per_lane["SHIPMENTS_ANALYZED"].fillna(0).astype(int)
        per_lane["COVERAGE_PCT"] = (
            100.0 * per_lane["SHIPMENTS_ANALYZED"] / per_lane["SHIPMENTS_TOTAL"]
        ).round(1)

        # Human-readable HH:MM:SS columns
        for src, dst in [
            ("AVG_MINUTES", "AVG_DURATION"),
            ("MEDIAN_MINUTES", "MEDIAN_DURATION"),
            ("MIN_MINUTES", "MIN_DURATION"),
            ("MAX_MINUTES", "MAX_DURATION"),
            ("P90_MINUTES", "P90_DURATION"),
        ]:
            if src in per_lane.columns:
                per_lane[dst] = per_lane[src].apply(fmt_duration)

        # Round numeric stats for readability
        for col in ["AVG_MINUTES", "MEDIAN_MINUTES", "MIN_MINUTES",
                    "MAX_MINUTES", "P90_MINUTES", "STDDEV_MINUTES"]:
            if col in per_lane.columns:
                per_lane[col] = per_lane[col].round(1)

        # Order columns nicely
        ordered = [
            "DIRECTION", "LANE",
            "ORIGIN_CITY", "ORIGIN_STATE", "ORIGIN_COUNTRY",
            "DEST_CITY", "DEST_STATE", "DEST_COUNTRY",
            "SHIPMENTS_TOTAL", "SHIPMENTS_ANALYZED", "COVERAGE_PCT",
            "AVG_DURATION", "MEDIAN_DURATION", "MIN_DURATION", "MAX_DURATION", "P90_DURATION",
            "AVG_MINUTES", "MEDIAN_MINUTES", "MIN_MINUTES", "MAX_MINUTES", "P90_MINUTES", "STDDEV_MINUTES",
        ]
        per_lane = per_lane[[c for c in ordered if c in per_lane.columns]]
        per_lane = per_lane.sort_values(
            ["DIRECTION", "SHIPMENTS_TOTAL"], ascending=[True, False]
        ).reset_index(drop=True)

    # Order columns in per-shipment sheet
    ordered_ship = [
        "BILL_OF_LADING", "SHIPMENT_ID", "ORDER_NUMBER", "SHIPMENT_LEG_ID",
        "DIRECTION",
        "ORIGIN_LOCATION_NAME", "ORIGIN_CITY", "ORIGIN_STATE", "ORIGIN_COUNTRY",
        "DEST_LOCATION_NAME", "DEST_CITY", "DEST_STATE", "DEST_COUNTRY",
        "LANE",
        "CARRIER_NAME", "CARRIER_ID", "SCAC",
        "TRACKING_METHOD", "FINAL_STATUS",
        "TRACKING_START_UTC", "TRACKING_END_UTC",
        "T_EXIT_ORIGIN_COUNTRY", "T_ENTER_DEST_COUNTRY",
        "BORDER_CROSSING_DURATION", "BORDER_CROSSING_MINUTES",
        "CONFIDENCE",
        "USED_PING_COUNT", "RCA_PING_COUNT", "TRANSITIONS_FOUND",
        "NOTES",
    ]
    per_shipment = per_shipment[[c for c in ordered_ship if c in per_shipment.columns]]

    # Sort for readability: analyzable first (by direction + duration desc), then missing
    per_shipment["_sort_missing"] = per_shipment["BORDER_CROSSING_MINUTES"].isna().astype(int)
    per_shipment = per_shipment.sort_values(
        by=["_sort_missing", "DIRECTION", "BORDER_CROSSING_MINUTES"],
        ascending=[True, True, False],
        na_position="last",
    ).drop(columns=["_sort_missing"]).reset_index(drop=True)

    return per_shipment, per_lane


# ---------------------------------------------------------------------------
# Excel builder
# ---------------------------------------------------------------------------
def _strip_tz(df: pd.DataFrame) -> pd.DataFrame:
    """Excel doesn't support timezone-aware datetimes. Convert any tz-aware
    datetime columns to tz-naive UTC."""
    out = df.copy()
    for col in out.columns:
        s = out[col]
        if pd.api.types.is_datetime64_any_dtype(s):
            try:
                if getattr(s.dt, "tz", None) is not None:
                    out[col] = s.dt.tz_convert("UTC").dt.tz_localize(None)
            except (AttributeError, TypeError):
                pass
    return out


def _col_width(series: pd.Series,
               hard_min: int = 14,
               hard_max: int = 42,
               pad: int = 2) -> int:
    """Return a safe Excel column width that survives NaN / Arrow / mixed dtypes."""
    try:
        if len(series) == 0:
            return hard_min
        # Fill NaN BEFORE converting to string so PyArrow backend doesn't
        # leak floats through .astype(str). Then use the vectorized str.len()
        # which returns NA-safe lengths, and coerce NA to 0 with fillna.
        s = series.astype(object).where(series.notna(), "")
        lengths = s.astype(str).str.len()
        try:
            lengths = lengths.fillna(0)
        except Exception:
            pass
        max_len = int(lengths.max()) if len(lengths) else 0
        return min(hard_max, max(hard_min, max_len + pad))
    except Exception:
        return hard_min


# ---------------------------------------------------------------------------
# Excel column-type maps
# ---------------------------------------------------------------------------
DATETIME_OUTPUT_COLS = {
    "TRACKING_START_UTC", "TRACKING_END_UTC",
    "T_EXIT_ORIGIN_COUNTRY", "T_ENTER_DEST_COUNTRY",
}
SHIPMENT_NUM2_COLS = {"BORDER_CROSSING_MINUTES"}
SHIPMENT_INT_COLS = {"USED_PING_COUNT", "RCA_PING_COUNT", "TRANSITIONS_FOUND",
                     "SHIPMENT_ID", "CARRIER_ID"}
SHIPMENT_TEXT_COLS = {
    "BILL_OF_LADING", "ORDER_NUMBER", "SHIPMENT_LEG_ID",
    "DIRECTION", "ORIGIN_LOCATION_NAME", "ORIGIN_CITY", "ORIGIN_STATE", "ORIGIN_COUNTRY",
    "DEST_LOCATION_NAME", "DEST_CITY", "DEST_STATE", "DEST_COUNTRY",
    "LANE", "CARRIER_NAME", "SCAC", "TRACKING_METHOD", "FINAL_STATUS",
    "BORDER_CROSSING_DURATION", "CONFIDENCE", "NOTES",
}

LANE_NUM1_COLS = {
    "AVG_MINUTES", "MEDIAN_MINUTES", "MIN_MINUTES",
    "MAX_MINUTES", "P90_MINUTES", "STDDEV_MINUTES", "COVERAGE_PCT",
}
LANE_INT_COLS = {"SHIPMENTS_TOTAL", "SHIPMENTS_ANALYZED"}
LANE_TEXT_COLS = {
    "DIRECTION", "LANE",
    "ORIGIN_CITY", "ORIGIN_STATE", "ORIGIN_COUNTRY",
    "DEST_CITY", "DEST_STATE", "DEST_COUNTRY",
    "AVG_DURATION", "MEDIAN_DURATION", "MIN_DURATION",
    "MAX_DURATION", "P90_DURATION",
}


def _write_key_sheet(wb) -> None:
    """Write a customer-facing Key & Glossary sheet to the workbook."""
    ws = wb.add_worksheet("Key & Glossary")
    ws.set_tab_color("#1F4E78")
    ws.set_column(0, 0, 36)
    ws.set_column(1, 1, 95)

    title_fmt = wb.add_format({
        "bold": True, "font_size": 16,
        "bg_color": "#1F4E78", "font_color": "white",
        "align": "left", "valign": "vcenter",
    })
    section_fmt = wb.add_format({
        "bold": True, "font_size": 12,
        "bg_color": "#D9E1F2", "font_color": "#1F4E78",
        "align": "left", "valign": "vcenter", "top": 1,
    })
    label_fmt = wb.add_format({
        "bold": True, "font_color": "#1F4E78",
        "align": "left", "valign": "top",
    })
    body_fmt = wb.add_format({
        "text_wrap": True, "valign": "top", "align": "left",
    })
    note_fmt = wb.add_format({
        "italic": True, "font_color": "#555555",
        "text_wrap": True, "valign": "top", "align": "left",
    })

    rows = [
        ("title", "Cross-Border Border-Crossing Report — Guide"),
        ("space",),
        ("section", "What this report measures"),
        ("body", "This report analyzes US-Mexico cross-border truckload "
                 "shipments and computes how long each shipment took to "
                 "actually cross the border — not the full journey, just the "
                 "border crossing. Two sheets summarize the results:"),
        ("kv", "Per Shipment", "One row per cross-border shipment, with the "
                                "computed border-crossing duration or "
                                "\"missing data\" if it could not be computed."),
        ("kv", "Lane Statistics", "Per-lane aggregates (average, median, min, "
                                   "max, p90, stddev) over the analyzable "
                                   "shipments."),
        ("space",),
        ("section", "How border-crossing time is calculated"),
        ("body",
            "For each shipment we look at every GPS position ping recorded "
            "during the trip. Each ping has a timestamp and a country (US "
            "or MX). We sort the pings in time order and find the moment "
            "the truck moved from the origin country into the destination "
            "country — specifically, the LAST consecutive ping pair where "
            "the country flips in the shipment's direction (MX→US for an "
            "MX→US shipment, US→MX for a US→MX shipment)."),
        ("kv", "T_EXIT_ORIGIN_COUNTRY", "Timestamp of the last ping in the origin country."),
        ("kv", "T_ENTER_DEST_COUNTRY", "Timestamp of the first ping in the destination country."),
        ("kv", "Border-crossing duration", "T_ENTER_DEST_COUNTRY − T_EXIT_ORIGIN_COUNTRY."),
        ("body",
            "Why \"last\" and not \"first\"? Occasionally a stale or "
            "out-of-order ping briefly shows the truck in the destination "
            "country before the real crossing. Taking the LAST transition "
            "avoids these false positives — once the truck has truly "
            "entered the destination country, it doesn't go back."),
        ("space",),
        ("section", "When a shipment is marked \"missing data\""),
        ("kv", "No position pings", "The shipment has no GPS pings recorded."),
        ("kv", "Only one ping", "There is only one ping — not enough to detect a transition."),
        ("kv", "Pings all in one country", "All pings are in a single country; the truck never appears to have crossed in the data."),
        ("kv", "No matching transition", "Pings exist in both countries, but no country transition matches the shipment's declared direction."),
        ("note", "In every case the NOTES column explains exactly which one applied."),
        ("space",),
        ("section", "Confidence flag"),
        ("kv", "HIGH",
            "20+ pings AND at most 2 country transitions detected. "
            "The actual crossing was pinpointed within roughly 15 minutes."),
        ("kv", "MEDIUM",
            "6+ pings recorded. Directionally correct but may have up to "
            "an hour of slop."),
        ("kv", "LOW",
            "Only 2–5 pings recorded. Crossing is computable but sparse — "
            "the gap between T_EXIT and T_ENTER may span hours of "
            "unobserved driving (the duration is an upper bound)."),
        ("kv", "N/A", "The crossing could not be computed at all."),
        ("space",),
        ("section", "Per Shipment sheet — column reference"),
        ("kv", "BILL_OF_LADING", "Customer-facing BOL identifier."),
        ("kv", "SHIPMENT_ID", "Internal project44 shipment ID."),
        ("kv", "ORDER_NUMBER", "Order reference, if provided."),
        ("kv", "SHIPMENT_LEG_ID", "Internal leg UUID (used to join positions)."),
        ("kv", "DIRECTION", "MX -> US or US -> MX."),
        ("kv", "ORIGIN_LOCATION_NAME / ORIGIN_CITY / ORIGIN_STATE / ORIGIN_COUNTRY", "Pickup location details."),
        ("kv", "DEST_LOCATION_NAME / DEST_CITY / DEST_STATE / DEST_COUNTRY", "Delivery location details."),
        ("kv", "LANE", "Pre-built lane string (origin city, state — destination city, state)."),
        ("kv", "CARRIER_NAME / CARRIER_ID / SCAC", "Carrier identity."),
        ("kv", "TRACKING_METHOD", "How the carrier shared tracking (PUSH_TRACKING, ELD, etc.)."),
        ("kv", "FINAL_STATUS", "Final shipment status from project44."),
        ("kv", "TRACKING_START_UTC", "When tracking began (UTC, date + time)."),
        ("kv", "TRACKING_END_UTC", "When tracking ended (UTC, date + time)."),
        ("kv", "T_EXIT_ORIGIN_COUNTRY", "Timestamp of the last ping in the origin country (UTC). Blank if no crossing was detected."),
        ("kv", "T_ENTER_DEST_COUNTRY", "Timestamp of the first ping in the destination country (UTC). Blank if no crossing was detected."),
        ("kv", "BORDER_CROSSING_DURATION",
            "Crossing time formatted as H:MM:SS for visual reading, or \"missing data\" if not computable. "
            "TEXT type — use BORDER_CROSSING_MINUTES for sorting/filtering/calculations."),
        ("kv", "BORDER_CROSSING_MINUTES",
            "Same crossing time expressed in decimal minutes (e.g. 7.93). "
            "NUMERIC type — use this column for sorting, filtering, pivots, charts."),
        ("kv", "CONFIDENCE", "HIGH / MEDIUM / LOW / N/A — see Confidence section above."),
        ("kv", "USED_PING_COUNT", "Number of valid pings the algorithm worked with."),
        ("kv", "RCA_PING_COUNT", "Total ping count reported in project44's RCA view (for reference)."),
        ("kv", "TRANSITIONS_FOUND",
            "Number of origin→destination country flips detected in the ping sequence. "
            "If > 1, the data had spurious flips; we use the last transition."),
        ("kv", "NOTES", "Reason a shipment was marked \"missing data\" (blank if successfully computed)."),
        ("space",),
        ("section", "Lane Statistics sheet — column reference"),
        ("kv", "DIRECTION", "MX -> US or US -> MX."),
        ("kv", "LANE", "Lane string (origin city, state — destination city, state)."),
        ("kv", "ORIGIN_CITY / ORIGIN_STATE / ORIGIN_COUNTRY", "Pickup details."),
        ("kv", "DEST_CITY / DEST_STATE / DEST_COUNTRY", "Delivery details."),
        ("kv", "SHIPMENTS_TOTAL", "Total shipments on this lane in the report window."),
        ("kv", "SHIPMENTS_ANALYZED", "Subset with a computable crossing time."),
        ("kv", "COVERAGE_PCT", "SHIPMENTS_ANALYZED ÷ SHIPMENTS_TOTAL × 100."),
        ("kv", "AVG_DURATION / MEDIAN_DURATION / MIN_DURATION / MAX_DURATION / P90_DURATION",
            "Crossing-time stats formatted as H:MM:SS (text)."),
        ("kv", "AVG_MINUTES / MEDIAN_MINUTES / MIN_MINUTES / MAX_MINUTES / P90_MINUTES / STDDEV_MINUTES",
            "Same stats in decimal minutes (numeric)."),
        ("space",),
        ("section", "Limitations to be aware of"),
        ("body",
            "• Many shipments — especially US→MX direction — may have very sparse position pings, "
            "leading to a higher share of \"missing data\" rows. This reflects what the tracking data "
            "actually contains, not a tool limitation.\n"
            "• If pings are missing during the actual crossing window, the duration will include the gap "
            "and will over-estimate. The USED_PING_COUNT and CONFIDENCE columns help spot these.\n"
            "• Reverse-geocoding errors can briefly tag a ping with the wrong country. The \"last "
            "transition\" rule absorbs most of these; cases where TRANSITIONS_FOUND > 2 are worth a "
            "manual spot-check."),
    ]

    r = 0
    for row in rows:
        kind = row[0]
        if kind == "title":
            ws.set_row(r, 28)
            ws.merge_range(r, 0, r, 1, row[1], title_fmt)
            r += 2
        elif kind == "section":
            ws.set_row(r, 22)
            ws.merge_range(r, 0, r, 1, row[1], section_fmt)
            r += 1
        elif kind == "body":
            # Estimate height by length
            ws.set_row(r, max(18, min(120, 18 + (len(row[1]) // 70) * 16)))
            ws.merge_range(r, 0, r, 1, row[1], body_fmt)
            r += 1
        elif kind == "note":
            ws.set_row(r, 18)
            ws.merge_range(r, 0, r, 1, row[1], note_fmt)
            r += 1
        elif kind == "kv":
            ws.set_row(r, max(18, min(80, 18 + (len(row[2]) // 90) * 16)))
            ws.write(r, 0, row[1], label_fmt)
            ws.write(r, 1, row[2], body_fmt)
            r += 1
        elif kind == "space":
            r += 1


def build_excel(per_shipment: pd.DataFrame, per_lane: pd.DataFrame) -> bytes:
    """Build the 3-sheet Excel report (Per Shipment + Lane Statistics + Key) and return bytes."""
    per_shipment = _strip_tz(per_shipment)
    per_lane = _strip_tz(per_lane)
    buf = io.BytesIO()
    with pd.ExcelWriter(
        buf,
        engine="xlsxwriter",
        datetime_format="yyyy-mm-dd hh:mm:ss",
        date_format="yyyy-mm-dd",
    ) as writer:
        per_shipment.to_excel(writer, sheet_name="Per Shipment", index=False)
        per_lane.to_excel(writer, sheet_name="Lane Statistics", index=False)

        wb = writer.book

        # ---- shared formats ----
        header_fmt = wb.add_format({
            "bold": True, "bg_color": "#1F4E78", "font_color": "white",
            "border": 1, "align": "left", "valign": "vcenter",
        })
        missing_fmt = wb.add_format({"bg_color": "#FFEBEB", "font_color": "#B00020"})
        dt_col_fmt   = wb.add_format({"num_format": "yyyy-mm-dd hh:mm:ss"})
        num2_col_fmt = wb.add_format({"num_format": "0.00"})
        num1_col_fmt = wb.add_format({"num_format": "0.0"})
        int_col_fmt  = wb.add_format({"num_format": "0"})
        txt_col_fmt  = wb.add_format({"num_format": "@"})

        sheet_specs = [
            ("Per Shipment", per_shipment,
             DATETIME_OUTPUT_COLS, SHIPMENT_NUM2_COLS, set(),
             SHIPMENT_INT_COLS, SHIPMENT_TEXT_COLS),
            ("Lane Statistics", per_lane,
             set(), set(), LANE_NUM1_COLS,
             LANE_INT_COLS, LANE_TEXT_COLS),
        ]

        for (sheet_name, df, dt_cols, num2_cols,
             num1_cols, int_cols, txt_cols) in sheet_specs:
            ws = writer.sheets[sheet_name]

            for col_idx, col_name in enumerate(df.columns):
                try:
                    ws.write(0, col_idx, col_name, header_fmt)
                except Exception:
                    pass

                width = _col_width(df[col_name])

                if col_name in dt_cols:
                    fmt = dt_col_fmt
                elif col_name in num2_cols:
                    fmt = num2_col_fmt
                elif col_name in num1_cols:
                    fmt = num1_col_fmt
                elif col_name in int_cols:
                    fmt = int_col_fmt
                elif col_name in txt_cols:
                    fmt = txt_col_fmt
                else:
                    fmt = None

                if fmt is not None:
                    ws.set_column(col_idx, col_idx, width, fmt)
                else:
                    ws.set_column(col_idx, col_idx, width)

            ws.freeze_panes(1, 0)
            if len(df) > 0 and len(df.columns) > 0:
                ws.autofilter(0, 0, len(df), len(df.columns) - 1)

            # Highlight "missing data" rows on the Per Shipment sheet
            if (sheet_name == "Per Shipment"
                    and "BORDER_CROSSING_DURATION" in df.columns
                    and len(df) > 0):
                col_idx = list(df.columns).index("BORDER_CROSSING_DURATION")
                ws.conditional_format(
                    1, col_idx, len(df), col_idx,
                    {"type": "text", "criteria": "containing",
                     "value": "missing data", "format": missing_fmt},
                )

        # ---- Key & Glossary sheet (last) ----
        _write_key_sheet(wb)

    return buf.getvalue()


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
st.title("Cross-Border Shipment Border-Crossing Analyzer")

st.markdown(
    """
This tool processes a Snowflake CSV export and produces an Excel report
with per-shipment border-crossing durations and per-lane statistics.

**How to use:**
1. Run `query.sql` in Snowflake (edit tenant id + date range at the top).
2. Export the result as CSV.
3. Upload the CSV below.
4. Download the Excel report.
"""
)

with st.expander("How border-crossing time is calculated"):
    st.markdown(
        """
**Algorithm — "Last country transition"**

For each shipment we look at every GPS position ping recorded during the
trip. We sort them by time and check the country (`US` or `MX`) each ping
reports. We then find the moment the truck moved from the origin country
into the destination country — specifically, the *last* consecutive ping
pair where the country flips in the shipment's direction (MX→US or
US→MX). The time gap between those two pings is the border-crossing
duration.

**Why "last", not "first"?** Occasionally a stale or out-of-order ping
briefly shows the truck in the destination country well before the real
crossing (e.g. a Texas ping while the truck is still loading in Mexico).
Taking the *last* transition avoids these false positives — once the
truck truly enters the destination country, it doesn't go back.

**"Missing data"** is shown when:
- The shipment has no GPS pings.
- The shipment has only one ping.
- All pings are in a single country (the truck never appears to have
  crossed in the data).
- No transition matches the shipment's declared direction.

**Confidence flag:**
- **HIGH** — 20+ pings and ≤ 2 country transitions detected.
- **MEDIUM** — 6+ pings.
- **LOW** — fewer than 6 pings (computable but sparse).
"""
    )

uploaded = st.file_uploader("Upload Snowflake CSV export", type=["csv"])

if uploaded is not None:
    try:
        with st.spinner("Reading CSV…"):
            df_raw = pd.read_csv(uploaded, low_memory=False)
        st.success(f"Loaded **{len(df_raw):,}** rows from CSV.")

        # Sanity-check expected columns
        missing_cols = [c for c in SHIPMENT_LEVEL_COLS if c not in df_raw.columns]
        if missing_cols:
            st.warning(
                "The uploaded CSV is missing some expected columns. The tool "
                "may still work, but please verify it was produced by `query.sql`."
                f"\n\nMissing: {', '.join(missing_cols[:8])}"
                + ("…" if len(missing_cols) > 8 else "")
            )

        with st.spinner("Computing per-shipment border crossings…"):
            per_shipment, per_lane = process_dataframe(df_raw)

        # ---------------- Summary metrics ----------------
        total = len(per_shipment)
        analyzed = per_shipment["BORDER_CROSSING_MINUTES"].notna().sum() if total else 0
        missing = total - analyzed
        coverage = (100.0 * analyzed / total) if total else 0.0

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total cross-border shipments", f"{total:,}")
        c2.metric("Analyzed", f"{analyzed:,}")
        c3.metric("Missing data", f"{missing:,}")
        c4.metric("Coverage", f"{coverage:.1f}%")

        # Direction split
        if total:
            dir_summary = (
                per_shipment.groupby("DIRECTION")
                .agg(
                    total=("SHIPMENT_LEG_ID", "count"),
                    analyzed=("BORDER_CROSSING_MINUTES", "count"),
                )
                .reset_index()
            )
            dir_summary["coverage_pct"] = (
                100.0 * dir_summary["analyzed"] / dir_summary["total"]
            ).round(1)
            st.subheader("By direction")
            st.dataframe(dir_summary, use_container_width=True)

        # Confidence breakdown
        if analyzed:
            conf = (
                per_shipment[per_shipment["BORDER_CROSSING_MINUTES"].notna()]
                .groupby(["DIRECTION", "CONFIDENCE"])
                .size()
                .reset_index(name="shipments")
            )
            st.subheader("Confidence breakdown (analyzed shipments)")
            st.dataframe(conf, use_container_width=True)

        # Previews
        st.subheader("Per Shipment — preview (first 25 rows)")
        st.dataframe(per_shipment.head(25), use_container_width=True)

        st.subheader("Lane Statistics — preview (first 25 rows)")
        st.dataframe(per_lane.head(25), use_container_width=True)

        # ---------------- Excel download ----------------
        with st.spinner("Building Excel report…"):
            xlsx_bytes = build_excel(per_shipment, per_lane)

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        st.download_button(
            label="Download Excel report",
            data=xlsx_bytes,
            file_name=f"crossborder_report_{stamp}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    except Exception as exc:
        st.error(f"Failed to process the uploaded CSV: {exc}")
        st.exception(exc)

else:
    st.info("Upload a CSV produced by `query.sql` to get started.")
