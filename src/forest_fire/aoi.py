"""Area of interest (AOI): GADM provinces chosen in ``.env`` with COUNTRY and PROVINCES.

GADM 4.1 (https://gadm.org) is the only source of boundaries: level 1 for the provinces, level 2
for the regencies and cities of the notebook. GADM is free for non-commercial use only without
permission, see https://gadm.org/license.html.

The full resolution GADM shapefiles are read once from the GADM server and kept as FlatGeobuf in
``output/gadm/``, so later runs read them from this computer.
"""

import unicodedata
from logging import getLogger

import geopandas as gpd

from forest_fire.config import COUNTRY, GADM_DIR, PROVINCES

logger = getLogger(__name__)

# One zip per country with the shapefile of every level
GADM_URL = "https://geodata.ucdavis.edu/gadm/gadm4.1/shp/gadm41_{country}_shp.zip"


def gadm_path(level: int, country: str = COUNTRY) -> str:
    """Path of the GADM ``level`` of ``country``, downloaded to ``output/gadm/`` the first time."""
    path = GADM_DIR / f"gadm41_{country}_{level}.fgb"
    if not path.exists():
        url = GADM_URL.format(country=country)
        logger.info(f"Download GADM level {level} of {country} to {path}")
        # Only this level is read from the zip, not the whole file
        gdf = gpd.read_file(f"/vsizip//vsicurl/{url}/gadm41_{country}_{level}.shp")
        GADM_DIR.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(".tmp.fgb")
        gdf.to_file(temp_path, driver="FlatGeobuf")
        temp_path.replace(path)
    return str(path)


def match_key(name: str) -> str:
    """Name compared without accents, case, spaces or punctuation: Kalimantan Barat = kalimantanbarat."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    return "".join(c for c in ascii_name.lower() if c.isalnum())


def file_name(name: str) -> str:
    """Name used in file names, lower case ASCII with ``_``: Kalimantan Barat = kalimantan_barat."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    return "_".join("".join(c if c.isalnum() else " " for c in ascii_name.lower()).split())


def select_provinces(gdf: gpd.GeoDataFrame, provinces: list[str] = PROVINCES) -> gpd.GeoDataFrame:
    """Rows of ``gdf`` whose NAME_1 is in ``provinces``, every row when ``provinces`` is empty."""
    if not provinces:
        return gdf
    wanted = {match_key(name): name for name in provinces}
    selected = gdf[gdf["NAME_1"].map(match_key).isin(wanted)]
    missing = set(wanted) - set(selected["NAME_1"].map(match_key))
    if missing:
        available = ", ".join(sorted(gdf["NAME_1"].unique()))
        raise ValueError(
            f"No GADM province {', '.join(wanted[key] for key in missing)} in {COUNTRY}. "
            f"Check PROVINCES in .env, the provinces are: {available}"
        )
    return selected


def read_provinces() -> gpd.GeoDataFrame:
    """The provinces of the AOI, one feature per province in EPSG:4326.

    The index is the province name used in file names, for example ``kalimantan_barat``.
    The ``name`` column is the GADM name, for example ``Kalimantan Barat``.
    """
    gdf = select_provinces(gpd.read_file(gadm_path(1), columns=["NAME_1"]))
    gdf = gdf.dissolve(by="NAME_1").reset_index().to_crs("EPSG:4326")
    gdf = gdf.rename(columns={"NAME_1": "name"})
    gdf.index = gdf["name"].map(file_name).rename("province")
    return gdf[["name", "geometry"]]


def read_adm2() -> gpd.GeoDataFrame:
    """The admin level 2 units (regencies and cities in Indonesia) of the AOI provinces.

    Columns: ``id`` (GID_2), ``name`` (NAME_2), ``type`` (TYPE_2, e.g. Kabupaten or Kota) and
    ``province`` (NAME_1).
    """
    gdf = select_provinces(gpd.read_file(gadm_path(2), columns=["GID_2", "NAME_2", "TYPE_2", "NAME_1"]))
    gdf = gdf.rename(columns={"GID_2": "id", "NAME_2": "name", "TYPE_2": "type", "NAME_1": "province"})
    return gdf.to_crs("EPSG:4326")
