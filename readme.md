# Cross-Border Shipment Border-Crossing Analyzer

A Streamlit tool that measures how long US-MX cross-border shipments spend
actually crossing the border (not the entire journey — just the crossing).
It ingests a CSV exported from Snowflake and produces a 2-sheet Excel
report.

## Quick start

1. **Run the query.** Open `query.sql` in Snowsight, edit the three
   variables at the top (`tenant_id`, `tracking_end_from`,
   `tracking_end_to`), execute it, and export the result as CSV.
2. **Upload to the app.** Open the Streamlit app, upload the CSV.
3. **Download the report.** Click "Download Excel report".

## Files

| File | Purpose |
|---|---|
| `query.sql` | Snowflake query that produces the input CSV. Edit the variables at the top. |
| `app.py` | Streamlit app. Reads the CSV, computes crossings, builds Excel. |
| `requirements.txt` | Python dependencies (for Streamlit Cloud or local run). |
| `README.md` | This file. |

## Output — Excel report

**Sheet 1 — Per Shipment.** One row per cross-border shipment, with:

| Column | Description |
|---|---|
| `BILL_OF_LADING` | Customer-facing BOL identifier (typically 8 digits) |
| `SHIPMENT_ID` | Internal project44 shipment id |
| `ORDER_NUMBER` | Order reference (if any) |
| `DIRECTION` | `MX -> US` or `US -> MX` |
| `ORIGIN_*`, `DEST_*` | Pickup / destination location, city, state, country |
| `LANE` | Pre-built lane string from RCA (origin city/state → dest city/state) |
| `CARRIER_NAME`, `CARRIER_ID`, `SCAC` | Carrier identity |
| `TRACKING_START_UTC`, `TRACKING_END_UTC` | Tracking window |
| `T_EXIT_ORIGIN_COUNTRY` | Timestamp of last position ping in the origin country |
| `T_ENTER_DEST_COUNTRY` | Timestamp of first position ping in the destination country |
| `BORDER_CROSSING_DURATION` | Crossing time as `H:MM:SS`, or **`missing data`** if not computable |
| `BORDER_CROSSING_MINUTES` | Same value in raw minutes (numeric, blank if missing) — useful for sorting/filtering |
| `CONFIDENCE` | `HIGH` / `MEDIUM` / `LOW` / `N/A` |
| `USED_PING_COUNT` | How many non-null pings the algorithm worked with |
| `RCA_PING_COUNT` | The `PING_COUNT` value carried over from `TL_ANALYTICS_RCA` |
| `TRANSITIONS_FOUND` | How many origin→destination country transitions were detected (>1 indicates noise; we use the last one) |
| `NOTES` | Reason if the shipment is `missing data` |

Missing-data rows are highlighted in light red, and the sheet is sorted
so the analyzed shipments come first.

**Sheet 2 — Lane Statistics.** One row per unique (direction, lane), with:

| Column | Description |
|---|---|
| `DIRECTION` | `MX -> US` or `US -> MX` |
| `LANE`, `ORIGIN_*`, `DEST_*` | Lane identity |
| `SHIPMENTS_TOTAL` | All shipments on this lane in the time window |
| `SHIPMENTS_ANALYZED` | Subset with a computable crossing time |
| `COVERAGE_PCT` | `SHIPMENTS_ANALYZED / SHIPMENTS_TOTAL` |
| `AVG_DURATION`, `MEDIAN_DURATION`, `MIN_DURATION`, `MAX_DURATION`, `P90_DURATION` | Crossing time stats as `H:MM:SS` |
| `AVG_MINUTES`, `MEDIAN_MINUTES`, … | Same stats in raw minutes (numeric) |
| `STDDEV_MINUTES` | Standard deviation in minutes |

## How border-crossing time is calculated (customer-friendly)

For each shipment we look at every GPS position ping recorded during the
trip. Each ping has a timestamp and a country (`US` or `MX`). We sort
them in time order and walk through them looking for the moment the
truck moved from the origin country into the destination country —
specifically, the **last** consecutive ping pair where the country
changes in the shipment's direction (`MX→US` for an MX→US shipment,
`US→MX` for a US→MX shipment).

- `t_exit` = timestamp of the last ping in the origin country
- `t_enter` = timestamp of the first ping in the destination country
- **Border-crossing duration = `t_enter − t_exit`**

**Why "last" and not "first"?** Occasionally a stale or out-of-order
ping briefly shows the truck in the destination country well before the
real crossing — for example, a Texas position recorded while the truck
is still loading in Mexico. Taking the *last* transition avoids these
false positives, because once the truck has truly entered the
destination country, it doesn't go back.

**"Missing data" is reported when:**
- The shipment has zero or one GPS pings (cannot compute).
- All available pings are in a single country — the truck never appears
  to have crossed in the data.
- No country transition matches the shipment's declared direction.

In each of these cases, the `NOTES` column explains exactly which case
applied for that shipment.

**Confidence flag:**
- `HIGH` — at least 20 pings recorded, and ≤ 2 country transitions
  detected (clean, high-density tracking).
- `MEDIUM` — at least 6 pings recorded.
- `LOW` — fewer than 6 pings recorded (computable but sparse — the
  crossing-time estimate has more slop).

## Known limitations of the source data

- **US → MX direction often has very sparse position pings** for some
  tenants because in-transit telemetry from US carriers may not flow
  into project44's position table. Expect a high share of `missing
  data` for US→MX shipments. The `NOTES` column will say so explicitly.
- **Tracking gaps near the border:** if there are missing pings during
  the actual crossing window, the computed duration will include the
  gap (i.e. it will over-estimate). The `USED_PING_COUNT` and
  `CONFIDENCE` columns help spot these.
- **Geocoding errors:** if a position ping has an incorrect country tag,
  it can create spurious transitions. The "last transition" approach
  handles most of these, but very noisy pings can still produce odd
  results — `TRANSITIONS_FOUND > 2` is a good signal that the row is
  worth eyeballing.

## Running locally

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Deploying on Streamlit Cloud

1. Push these four files to a public GitHub repo.
2. Go to https://share.streamlit.io and connect the repo.
3. Set the main file to `app.py`. Streamlit Cloud auto-detects
   `requirements.txt`.
4. (Optional) In the app's advanced settings, raise the upload size
   limit if you'll be processing very large windows:
   `[server] maxUploadSize = 1000`.

## Privacy note

Snowflake credentials never leave your machine — the tool only reads
the CSV file you upload. Uploaded files are processed in memory and not
persisted by the tool. If you deploy on Streamlit Cloud (which is
public-internet hosted), uploaded customer data passes through
Streamlit's infrastructure; for stricter data handling, run the app
locally instead.
