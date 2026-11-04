"""Download NASA FIRMS fire alerts with the FIRMS area API.

Adapted from ``rs_open_source.nasa.firms`` of remote-sensing-open-source-docker. Plain Python,
no GDAL command line needed. See https://firms.modaps.eosdis.nasa.gov/api/area/
"""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from io import StringIO
from logging import getLogger
from math import ceil, floor
from time import sleep
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

import geopandas as gpd
import pandas as pd

logger = getLogger(__name__)

FIRMS_API = "https://firms.modaps.eosdis.nasa.gov"

# Days of one request, the area API answers "Invalid day range. Expects [1..5]" above 5
WINDOW_DAYS = 5
# Requests at the same time, the MAP_KEY has a limit of 5000 transactions per 10 minutes
REQUEST_WORKERS = 4

# A FIRMS CSV starts with the latitude column, anything else is a message, e.g. "Invalid MAP_KEY."
# with HTTP 200, or HTTP 400 when the transaction limit is reached
CSV_HEADER_START = "latitude"
DOWNLOAD_ATTEMPTS = 6
# Waits double from 1 min up to 10 min, the interval of the transaction limit, 37 min in total
RETRY_WAIT_SECONDS = 60
MAX_RETRY_WAIT_SECONDS = 600

# Columns read as text. acq_time is HHMM in UTC without the leading zeros, confidence is a
# percent for MODIS and l, n or h for VIIRS
TEXT_COLUMNS = {"acq_date": str, "acq_time": str, "confidence": str, "version": str}


def read_text(url: str) -> tuple[int, str]:
    """Read a FIRMS link as text. The MAP_KEY is in the link, so the link is never logged."""
    try:
        with urlopen(url, timeout=300) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")


def check_map_key(map_key: str | None):
    """Fail at once on a missing or invalid MAP_KEY instead of after all the retries."""
    if not map_key:
        raise ValueError(
            "FIRMS_MAP_KEY is not set, get one at https://firms.modaps.eosdis.nasa.gov/api/map_key/"
        )
    code, text = read_text(f"{FIRMS_API}/mapserver/mapkey_status/?MAP_KEY={map_key}")
    if code != 200:
        raise ValueError(f"FIRMS_MAP_KEY is not valid: {text.strip()[:200]}")
    status = json.loads(text)
    logger.info(
        f"FIRMS_MAP_KEY used {status['current_transactions']} of {status['transaction_limit']} "
        f"transactions per {status['transaction_interval']}"
    )


def data_availability(map_key: str) -> dict[str, tuple[date, date]]:
    """First and last date of each FIRMS source, so windows without data are not requested."""
    code, text = read_text(f"{FIRMS_API}/api/data_availability/csv/{map_key}/all")
    if code != 200 or not text.startswith("data_id"):
        raise RuntimeError(f"No FIRMS data availability: {text.strip()[:200]}")
    table = pd.read_csv(StringIO(text))
    return {
        row.data_id: (date.fromisoformat(row.min_date), date.fromisoformat(row.max_date))
        for row in table.itertuples()
    }


def windows(
    sources: list[str], start_date: date, end_date: date, availability: dict[str, tuple[date, date]]
) -> list[tuple[str, date, int]]:
    """Windows of up to 5 days of each source between the dates, only inside its availability."""
    result = []
    for source in sources:
        if source not in availability:
            logger.info(f"{source} is not in the FIRMS data availability, skipped")
            continue
        first, last = availability[source]
        # NRT and SP of a satellite overlap, so the NRT starts after the SP when both are chosen
        archive = source.replace("_NRT", "_SP")
        if source.endswith("_NRT") and archive in sources and archive in availability:
            first = max(first, availability[archive][1] + timedelta(days=1))
        start, end = max(start_date, first), min(end_date, last)
        if start > end:
            logger.info(f"{source} has data from {first} to {last}, none from {start_date} to {end_date}")
            continue
        if (start, end) != (start_date, end_date):
            logger.info(f"{source} has data from {first} to {last}, only {start} to {end} is downloaded")

        while start <= end:
            days = min(WINDOW_DAYS, (end - start).days + 1)
            result.append((source, start, days))
            start += timedelta(days=days)
    return result


def download_window(
    map_key: str, source: str, bbox: tuple[float, float, float, float], start: date, days: int
) -> pd.DataFrame:
    """Download the alerts of one source over a bbox for up to 5 days, retrying until it is a CSV."""
    bbox_text = ",".join(f"{v:.4f}" for v in bbox)
    request_path = f"{source}/{bbox_text}/{days}/{start.isoformat()}"
    # Logged without the prefix, it holds the MAP_KEY
    logger.info(f"Download {request_path}")

    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            code, text = read_text(f"{FIRMS_API}/api/area/csv/{map_key}/{request_path}")
        except (URLError, TimeoutError) as e:
            code, text = None, str(e)
        if text.startswith(CSV_HEADER_START):
            table = pd.read_csv(StringIO(text), dtype=TEXT_COLUMNS)
            table["source"] = source
            return table

        wait = min(RETRY_WAIT_SECONDS * 2 ** (attempt - 1), MAX_RETRY_WAIT_SECONDS)
        logger.warning(
            f"No CSV for {request_path} (attempt {attempt}, HTTP {code}, "
            f"response {text.strip()[:80]!r}), retrying in {wait} s"
        )
        sleep(wait)

    raise RuntimeError(f"No FIRMS CSV for {request_path} after {DOWNLOAD_ATTEMPTS} attempts")


def download_firms(
    area: gpd.GeoDataFrame,
    start_date: str,
    end_date: str,
    map_key: str,
    sources: list[str],
    availability: dict[str, tuple[date, date]] | None = None,
) -> gpd.GeoDataFrame:
    """Download the FIRMS alerts of ``sources`` inside the polygons of ``area`` (EPSG:4326).

    The FIRMS API works with a bounding box, so the alerts are requested for the bounding box
    of ``area`` and then clipped to its polygons. Returns the alerts as points.
    """
    if availability is None:
        availability = data_availability(map_key)
    todo = windows(sources, date.fromisoformat(start_date), date.fromisoformat(end_date), availability)
    if not todo:
        raise ValueError(f"No FIRMS source {sources} has data from {start_date} to {end_date}")

    # Rounded outward to 4 decimals (about 10 m) so no alert on the edge is cut off
    xmin, ymin, xmax, ymax = area.to_crs("EPSG:4326").total_bounds
    bbox = (floor(xmin * 1e4) / 1e4, floor(ymin * 1e4) / 1e4, ceil(xmax * 1e4) / 1e4, ceil(ymax * 1e4) / 1e4)
    logger.info(f"Download {len(todo)} windows of up to {WINDOW_DAYS} days")
    with ThreadPoolExecutor(REQUEST_WORKERS) as executor:
        tables = list(executor.map(lambda w: download_window(map_key, w[0], bbox, w[1], w[2]), todo))

    # MODIS has brightness and bright_t31, VIIRS bright_ti4 and bright_ti5, the rest is the same
    table = pd.concat(tables, ignore_index=True)
    table["acq_datetime"] = pd.to_datetime(
        table["acq_date"] + table["acq_time"].str.zfill(4), format="%Y-%m-%d%H%M", utc=True
    )
    points = gpd.GeoDataFrame(
        table.drop(columns=["latitude", "longitude"]),
        geometry=gpd.points_from_xy(table["longitude"], table["latitude"]),
        crs="EPSG:4326",
    )

    # Keep only the alerts inside the polygons, not the whole bounding box
    inside = points.sjoin(area[["geometry"]].to_crs("EPSG:4326"), predicate="within").index.unique()
    return points.loc[inside]
