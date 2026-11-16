"""Shared parts of the satellite composite jobs (job.s1_composite, job.opera_water_s1).

Adapted from ``rs_open_source.utils`` of remote-sensing-open-source-docker: the AOI is split into
grids of 0.5 degree on one pixel grid in EPSG:4326, each grid is composited on its own, and the
grids are mosaicked into one Cloud Optimised GeoTIFF clipped to the AOI. The images are found with
the STAC API of NASA Earthdata (CMR) and read by GDAL over https with an Earthdata login.
"""

import atexit
import base64
import json
import math
import os
import tempfile
from calendar import monthrange
from logging import getLogger
from pathlib import Path
from shutil import rmtree, which
from subprocess import check_call
from time import sleep
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import geopandas as gpd
from shapely.geometry import box

from forest_fire.config import EARTHDATA_PASSWORD, EARTHDATA_USERNAME

logger = getLogger(__name__)

# Grids of 0.5 degree, the size of rs_open_source. A degree is taken as 111 km for the pixel size
GRID_SIZE = 0.5
METRES_PER_DEGREE = 111_000
# Grids processed at the same time, and dates per grid at the same time
TASKS = max(1, (os.cpu_count() or 1) // 4)
RETRY = 5
CMR_GRANULES = "https://cmr.earthdata.nasa.gov/search/granules.json"
# Tokens of the user on the Earthdata login server, read to check the login
URS_TOKENS = "https://urs.earthdata.nasa.gov/api/users/tokens"


def gdal(*args: str):
    """Run one ``gdal`` command line, e.g. ``gdal("raster", "pipeline", ...)``."""
    executable = which("gdal")
    if executable is None:
        raise RuntimeError("The gdal command line (GDAL 3.11 or newer) is not installed, use Docker")
    check_call([executable, *args])


def retry(function, *args, **kwargs):
    """Call ``function`` again after a failure, the network and the data servers can fail."""
    for attempt in range(1, RETRY + 1):
        try:
            return function(*args, **kwargs)
        except Exception as e:
            if attempt == RETRY:
                raise
            logger.warning(f"{function.__name__} failed ({e}), retry {attempt} of {RETRY - 1}")
            sleep(5 * attempt)


def write_list(path: Path, paths: list[str]) -> str:
    """List of inputs in a file for ``-i @file``, a command line of many links is too long."""
    path.write_text("\n".join(paths) + "\n", encoding="utf-8")
    return f"@{path}"


def month_range(month: str) -> tuple[str, str]:
    """First and last day of a month written YYYY-MM."""
    year, number = map(int, month.split("-"))
    return f"{month}-01", f"{month}-{monthrange(year, number)[1]:02d}"


def check_earthdata_login(username: str, password: str):
    """Check the Earthdata login before any download, a wrong login then fails at once.

    Lists the tokens of the user on urs.earthdata.nasa.gov with HTTP basic authentication, which
    changes nothing: 200 is a valid login, 401 a wrong user name or password.
    """
    credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
    request = Request(URS_TOKENS, headers={"Authorization": f"Basic {credentials}"})
    try:
        with urlopen(request, timeout=60):
            pass
    except HTTPError as e:
        if e.code == 401:
            raise SystemExit(
                "The Earthdata login of .env is not valid (HTTP 401): check EARTHDATA_USERNAME and "
                "EARTHDATA_PASSWORD, and that you can log in at https://urs.earthdata.nasa.gov"
            ) from None
        raise
    logger.info(f"Earthdata login of {username} is valid")


def earthdata_login():
    """Let GDAL read Earthdata with the login of .env, through a temporary .netrc file.

    The Earthdata servers redirect to urs.earthdata.nasa.gov, where GDAL logs in with the .netrc
    and keeps the session in a cookie file. Both are removed when the job ends.
    """
    if not EARTHDATA_USERNAME or not EARTHDATA_PASSWORD:
        raise SystemExit(
            "EARTHDATA_USERNAME and EARTHDATA_PASSWORD are not set in .env, "
            "register for free at https://urs.earthdata.nasa.gov/users/new"
        )
    if any(c.isspace() for c in EARTHDATA_USERNAME + EARTHDATA_PASSWORD):
        raise SystemExit("EARTHDATA_USERNAME and EARTHDATA_PASSWORD can not contain spaces in a .netrc file")
    check_earthdata_login(EARTHDATA_USERNAME, EARTHDATA_PASSWORD)
    folder = Path(tempfile.mkdtemp(prefix="earthdata_"))
    atexit.register(rmtree, folder, ignore_errors=True)
    netrc = folder / ".netrc"
    netrc.write_text(
        f"machine urs.earthdata.nasa.gov login {EARTHDATA_USERNAME} password {EARTHDATA_PASSWORD}\n",
        encoding="utf-8",
    )
    netrc.chmod(0o600)
    cookies = str(folder / "cookies.txt")
    os.environ.update(
        GDAL_HTTP_NETRC="YES",
        GDAL_HTTP_NETRC_FILE=str(netrc),
        GDAL_HTTP_COOKIEFILE=cookies,
        GDAL_HTTP_COOKIEJAR=cookies,
        # The ASF archive answers 403 to a HEAD request before the login redirect, GDAL starts with GET
        CPL_VSIL_CURL_USE_HEAD="NO",
        # No search for sidecar files (.aux.xml, .ovr) next to a file, each one would go through the
        # login redirect. Set in the Docker image too, here for the runs without Docker
        GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
        GDAL_HTTP_MAX_RETRY="5",
        GDAL_HTTP_RETRY_DELAY="5",
    )


def stac_search(stac_api: str, collection: str, bbox, start: str, end: str) -> list[dict]:
    """STAC items of ``collection`` over ``bbox`` from ``start`` to ``end``, all pages."""
    body = {
        "collections": [collection],
        "bbox": [float(v) for v in bbox],
        "datetime": f"{start}T00:00:00Z/{end}T23:59:59Z",
        "limit": 250,
    }
    headers = {"Content-Type": "application/json"}
    items = []
    request = Request(f"{stac_api}/search", data=json.dumps(body).encode(), headers=headers)
    while request is not None:
        with urlopen(request, timeout=300) as response:
            page = json.load(response)
        items += page["features"]
        following = [link for link in page.get("links", []) if link.get("rel") == "next"]
        request = None
        if following and page["features"]:
            link = following[0]
            data = (
                json.dumps(link["body"]).encode() if link.get("method") == "POST" and "body" in link else None
            )
            request = Request(link["href"], data=data, headers=headers)
    return items


def cmr_orbit_ids(short_name: str, bbox, start: str, end: str, orbit: str) -> set[str]:
    """Granule ids of an orbit direction (ascending or descending), not in the Earthdata STAC."""
    query = urlencode(
        {
            "short_name": short_name,
            "bounding_box": ",".join(str(float(v)) for v in bbox),
            "temporal": f"{start}T00:00:00Z,{end}T23:59:59Z",
            "attribute[]": f"string,ASCENDING_DESCENDING,{orbit.upper()}",
            "page_size": 2000,
        }
    )
    ids, search_after = set(), None
    while True:
        headers = {"CMR-Search-After": search_after} if search_after else {}
        with urlopen(Request(f"{CMR_GRANULES}?{query}", headers=headers), timeout=300) as response:
            entries = json.load(response)["feed"]["entry"]
            search_after = response.headers.get("CMR-Search-After")
        ids.update(entry["title"] for entry in entries)
        if not entries or search_after is None:
            return ids


def pixel_grid(aoi: gpd.GeoDataFrame, resolution: float):
    """Bounds snapped to whole pixels from the top left corner, the shape and the pixel size."""
    pixel = resolution / METRES_PER_DEGREE
    xmin, ymin, xmax, ymax = aoi.total_bounds
    width = max(1, math.ceil(round((xmax - xmin) / pixel, 6)))
    height = max(1, math.ceil(round((ymax - ymin) / pixel, 6)))
    return (xmin, ymax - height * pixel, xmin + width * pixel, ymax), (width, height), pixel


def make_grids(aoi: gpd.GeoDataFrame, bbox, shape, pixel) -> list[dict]:
    """Grids of a whole number of pixels, so every grid edge is on the pixel grid, over the AOI only."""
    size_px = max(1, round(GRID_SIZE / pixel))
    aoi_shape = aoi.union_all()
    grids = []
    for x in range(math.ceil(shape[0] / size_px)):
        col0, col1 = x * size_px, min((x + 1) * size_px, shape[0])
        for y in range(math.ceil(shape[1] / size_px)):
            row0, row1 = y * size_px, min((y + 1) * size_px, shape[1])
            grid = dict(
                grid_id=f"Y{y:03d}_X{x:03d}",
                min_x=bbox[0] + col0 * pixel,
                max_x=bbox[0] + col1 * pixel,
                max_y=bbox[3] - row0 * pixel,
                min_y=bbox[3] - row1 * pixel,
                width=col1 - col0,
                height=row1 - row0,
            )
            if box(grid["min_x"], grid["min_y"], grid["max_x"], grid["max_y"]).intersects(aoi_shape):
                grids.append(grid)
    return grids


def grid_bbox(grid: dict) -> tuple[float, float, float, float]:
    return grid["min_x"], grid["min_y"], grid["max_x"], grid["max_y"]


def reproject_args(bbox, shape, resampling: str | None = None) -> list[str]:
    """Arguments of a ``reproject`` step onto a grid of the pixel grid."""
    args = [
        "-d", "EPSG:4326", f"--bbox={','.join(map(str, bbox))}", "--bbox-crs=EPSG:4326",
        f"--size={shape[0]},{shape[1]}",
    ]  # fmt: skip
    return args + (["-r", resampling] if resampling else [])


def mosaic_grids(
    results: list[str],
    roi_path: Path,
    bbox,
    shape,
    output: Path,
    folder: Path,
    resampling: str,
    nodata: int | None = 0,
):
    """Mosaic the grids, clip them to the AOI and write a Cloud Optimised GeoTIFF.

    The grids are on the pixel grid of the mosaic, so nearest only copies them; ``resampling`` is
    used for the overviews.
    """
    reproject = ["!", "reproject", "-r", "near", *reproject_args(bbox, shape)]
    if nodata is not None:
        reproject.append(f"--output-nodata={nodata}")
    gdal(
        "raster", "pipeline", "--quiet",
        "!", "mosaic", "--resolution=average", "-i", write_list(folder / "grids.txt", results),
        "!", "clip", f"--like={roi_path}", "--allow-bbox-outside-source",
        *reproject,
        "!", "write", "--overwrite", "-f", "COG", "--co=COMPRESS=ZSTD", "--co=PREDICTOR=YES",
        f"--co=RESAMPLING={resampling}", f"--co=OVERVIEW_RESAMPLING={resampling}", "--co=STATISTICS=YES",
        str(output),
    )  # fmt: skip
