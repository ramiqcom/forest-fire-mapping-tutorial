"""Settings shared by the jobs and the notebook, read from the environment or ``.env``."""

import json
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
# The default is Kalimantan and mainland Sumatera
DEFAULT_PROVINCES = (
    "Kalimantan Barat, Kalimantan Selatan, Kalimantan Tengah, Kalimantan Timur, Kalimantan Utara, "
    "Aceh, Sumatera Utara, Sumatera Barat, Riau, Jambi, Sumatera Selatan, Bengkulu, Lampung"
)
PROVINCES = [p.strip() for p in getenv("PROVINCES", DEFAULT_PROVINCES).split(",") if p.strip()]
# Name of the AOI in the file names of the grid and figures, and its title on the maps
AOI_NAME = getenv("AOI_NAME", "kalimantan_sumatera")
AOI_TITLE = getenv("AOI_TITLE", "Kalimantan and Sumatera")

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

# Every output is written under this local folder, nothing is uploaded
OUTPUT = Path(getenv("OUTPUT_DIR", "output"))
GADM_DIR = OUTPUT / "gadm"
FIRMS_DIR = OUTPUT / "firms"
FIRMS_GRID_DIR = OUTPUT / "firms_grid"
FIGURE_DIR = OUTPUT / "figures"
