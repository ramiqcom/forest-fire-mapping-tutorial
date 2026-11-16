"""Settings shared by the jobs and the notebook, read from the environment or ``.env``."""

import json
from datetime import date
from os import getenv
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# NASA FIRMS MAP_KEY, free at https://firms.modaps.eosdis.nasa.gov/api/map_key/
FIRMS_MAP_KEY = getenv("FIRMS_MAP_KEY")

# Area of interest: GADM provinces (level 1) of a country, see forest_fire.aoi.
# COUNTRY is the ISO 3166 alpha-3 code that GADM uses, IDN for Indonesia
COUNTRY = getenv("COUNTRY", "IDN").strip().upper()
# GADM province names, comma separated, written with or without spaces. Empty for the whole country.
# The default example is one province, Kalimantan Tengah
PROVINCES = [p.strip() for p in getenv("PROVINCES", "Kalimantan Tengah").split(",") if p.strip()]
# Name of the AOI in the file names of the grid and figures, and its title on the maps
AOI_NAME = getenv("AOI_NAME", "kalimantan_tengah")
AOI_TITLE = getenv("AOI_TITLE", "Kalimantan Tengah")

# Period of the fire alerts, both dates included
START_DATE = getenv("START_DATE", "2026-08-01")
END_DATE = getenv("END_DATE", "2026-10-31")
# Part of every file name, so the outputs of different periods never mix and a file of
# another period is never skipped as done: 2026-08-01_2026-10-31
DATE_NAME = f"{START_DATE}_{END_DATE}"

# FIRMS sources. NRT (near real time) covers the last months, SP (standard processing) the
# archive before it. Add the SP sources, e.g. "VIIRS_SNPP_SP", for dates older than the NRT
FIRMS_SOURCES = json.loads(
    getenv(
        "FIRMS_SOURCES",
        '["MODIS_NRT", "VIIRS_SNPP_NRT", "VIIRS_NOAA20_NRT", "VIIRS_NOAA21_NRT"]',
    )
)

# Pixel size of the Forest Typology 2020 output in metres (the dataset is 10 m). 100 m keeps a
# province small, 1000 m gives the cells of the FIRMS grid
FOREST_TYPOLOGY_RESOLUTION = int(getenv("FOREST_TYPOLOGY_RESOLUTION", "100"))


def parse_periods(value: str) -> list[tuple[str, str]]:
    """Periods written START/END, comma separated, e.g. 2026-06-01/2026-06-14,2026-11-01/2026-11-14."""
    periods = []
    for item in value.split(","):
        if not item.strip():
            continue
        start, _, end = item.strip().partition("/")
        if not end or date.fromisoformat(start) > date.fromisoformat(end):
            raise ValueError(
                f"Period {item!r} is not START/END with START not after END, e.g. 2026-06-01/2026-06-14"
            )
        periods.append((start, end))
    return periods


# Sentinel-1 composites of job.s1_composite: one per period. Two weeks before the fire season and two
# weeks after it by default; Sentinel-1 passes every 12 days, so two weeks cover the whole AOI
DEFAULT_S1_PERIODS = "2026-06-01/2026-06-14,2026-11-01/2026-11-14"
S1_PERIODS = parse_periods(getenv("S1_PERIODS", DEFAULT_S1_PERIODS))
# Pixel size in metres, the orbits to use (both, ascending or descending) and the size of the
# gaussian filter against speckle (3 or 5, 0 for no filter)
S1_RESOLUTION = int(getenv("S1_RESOLUTION", "30"))
S1_ORBIT = getenv("S1_ORBIT", "both").strip().lower()
S1_GAUSSIAN_SIZE = int(getenv("S1_GAUSSIAN_SIZE", "3"))

# JRC Global Surface Water of job.gsw: the layers, one band per layer (occurrence, change, seasonality,
# recurrence, transitions, extent), and the pixel size in metres
GSW_LAYERS = json.loads(getenv("GSW_LAYERS", '["occurrence"]'))
GSW_RESOLUTION = int(getenv("GSW_RESOLUTION", "30"))

# NASA Earthdata login of the Sentinel-1 jobs, free at https://urs.earthdata.nasa.gov/users/new
EARTHDATA_USERNAME = getenv("EARTHDATA_USERNAME")
EARTHDATA_PASSWORD = getenv("EARTHDATA_PASSWORD")

# Every output is written under this local folder, nothing is uploaded
OUTPUT = Path(getenv("OUTPUT_DIR", "output"))
GADM_DIR = OUTPUT / "gadm"
FIRMS_DIR = OUTPUT / "firms"
FIRMS_GRID_DIR = OUTPUT / "firms_grid"
FOREST_TYPOLOGY_DIR = OUTPUT / "forest_typology"
S1_DIR = OUTPUT / "s1"
GSW_DIR = OUTPUT / "gsw"
FIGURE_DIR = OUTPUT / "figures"
