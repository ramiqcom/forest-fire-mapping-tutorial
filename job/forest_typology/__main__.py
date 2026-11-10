"""Step 2b: Forest Typology 2020 land cover of the AOI, the type of forest of every pixel.

Forest Typology 2020 v1 (ForTy) of Google DeepMind, WRI, UMD and IIASA, CC BY 4.0, see
https://plus.figshare.com/articles/dataset/_i_Forest_Typology_2020_v1_i_/32288616
It gives, per 10 m pixel, the probability (0 to 250, 250 is 1) of 5 classes: primary forest,
naturally regenerating forest, planted forest, plantation forest, and tree crops and agroforestry.

The data is read from the public Space4Good copy of the figshare GeoTIFFs in Cloud Storage,
gs://s4g-green-finance-bucket/data/cog/forest_typology/, one folder per UTM grid zone (6 x 8
degree), e.g. 49M/raster_49M_20_14_0.tif, as in ``rs_open_source.forest_typology`` of
remote-sensing-open-source-docker. The copy is faster and more reliable than figshare, which
limits the requests. It is read without a login, no Google Cloud account is needed. Only the
tiles over the AOI and the overview level closest to the resolution are read.

Writes ``output/forest_typology/<AOI_NAME>_forest_typology_<resolution>m.tif`` on this computer, a
Cloud Optimised GeoTIFF of 7 bands: the 5 probabilities, OtherLand (250 minus the 5) and class
(1 to 6, the most probable of the 6, in that order). 255 is nodata, also outside the AOI provinces.
Skipped when the file exists, delete it to make it again.

Run with ``docker compose run --rm forest_typology`` or ``python -m job.forest_typology``.
"""

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from logging import INFO, basicConfig, getLogger
from math import ceil, floor
from pathlib import Path
from shutil import rmtree, which
from subprocess import check_call, check_output

import numpy as np
import rasterio
from rasterio.features import geometry_mask
from rasterio.transform import from_origin
from rasterio.warp import transform_bounds
from shapely.geometry import box

from forest_fire.aoi import read_provinces
from forest_fire.config import AOI_NAME, FOREST_TYPOLOGY_DIR, FOREST_TYPOLOGY_RESOLUTION

basicConfig(level=INFO, format="%(asctime)s - %(name)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
logger = getLogger("job.forest_typology")

# Public copy of the GeoTIFFs in Cloud Storage, gs://s4g-green-finance-bucket/data/cog/forest_typology/,
# one folder per UTM grid zone, e.g. 49M/
SOURCE_PREFIX = "/vsigs/s4g-green-finance-bucket/data/cog/forest_typology"
# The copy is public, GDAL reads it without a Google login. Set before any GDAL call, the
# gdal commands of the job get it from the environment
os.environ["GS_NO_SIGN_REQUEST"] = "YES"
# Folder name of a grid zone, e.g. 49M, the other entries of the prefix are README.txt and _mirror
ZONE_PATTERN = re.compile(r"^[0-9]{1,2}[C-X]$")

# Bands of the GeoTIFFs, the probability 0 to 250 (250 is 1) of each class, 255 is nodata
BANDS = [
    "PrimaryForest",
    "NaturallyRegeneratingForest",
    "PlantedForest",
    "PlantationForest",
    "TreeCropsAndAgroforestry",
]
OUTPUT_BANDS = [*BANDS, "OtherLand", "class"]
CLASSES = [
    "Primary forest",
    "Naturally regenerating forest",
    "Planted forest",
    "Plantation forest",
    "Tree crops and agroforestry",
    "Other land",
]
NODATA = 255
CRS = "EPSG:4326"
# The FIRMS grid cell (about 1 km), the output cells nest in it: 100 m is 0.0009 degree
FIRMS_CELL = 0.009
RESOLUTION = FIRMS_CELL * FOREST_TYPOLOGY_RESOLUTION / 1000

# Latitude bands of the UTM grid zones from 80 degree south, X is 12 degree high to 84 degree north
LATITUDE_BANDS = "CDEFGHJKLMNPQRSTUVWX"

# Tile headers read and tiles reprojected at the same time
INDEX_WORKERS = 16
WARP_WORKERS = 8


def gdal(*args: str):
    """Run one ``gdal`` command line, e.g. ``gdal("raster", "reproject", ...)``."""
    executable = which("gdal")
    if executable is None:
        raise RuntimeError("The gdal command line (GDAL 3.11 or newer) is not installed, use Docker")
    check_call([executable, *args])


def zone_box(zone: str) -> tuple[float, float, float, float]:
    """Longitude and latitude box of a UTM grid zone such as 49M, with the wider zones of Norway and Svalbard."""
    number, band = int(zone[:-1]), zone[-1]
    south = -80 + 8 * LATITUDE_BANDS.index(band)
    north = 84 if band == "X" else south + 8
    west, east = -180 + 6 * (number - 1), -180 + 6 * number
    if band == "V" and number == 31:
        east = 3
    if band == "V" and number == 32:
        west = 3
    if band == "X" and number in (31, 33, 35, 37):
        west, east = {31: (0, 9), 33: (9, 21), 35: (21, 33), 37: (33, 42)}[number]
    return west, south, east, north


def intersects(a: tuple[float, ...], b: tuple[float, ...]) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def list_folder(path: str) -> list[str]:
    """Names in a folder of the Cloud Storage copy, e.g. the zones or the tiles of a zone."""
    return [
        name.rstrip("/") for name in check_output([which("gdal"), "vsi", "list", path], text=True).split()
    ]


def list_zones() -> list[str]:
    """The UTM grid zones of the copy, there is none over the ocean."""
    zones = [name for name in list_folder(SOURCE_PREFIX) if ZONE_PATTERN.match(name)]
    if not zones:
        raise SystemExit(f"No Forest Typology zone in {SOURCE_PREFIX}")
    return zones


def get_extent(aoi) -> tuple[float, float, float, float]:
    """Bounds of ``aoi`` snapped outward to the FIRMS cell, so the pixels line up with the FIRMS grid."""
    xmin, ymin, xmax, ymax = aoi.to_crs(CRS).total_bounds
    return (
        round(floor(xmin / FIRMS_CELL) * FIRMS_CELL, 4),
        round(floor(ymin / FIRMS_CELL) * FIRMS_CELL, 4),
        round(ceil(xmax / FIRMS_CELL) * FIRMS_CELL, 4),
        round(ceil(ymax / FIRMS_CELL) * FIRMS_CELL, 4),
    )


def zone_index(zone: str) -> list[dict]:
    """Path and longitude and latitude box of every GeoTIFF of a zone.

    Read from the headers of the GeoTIFFs and kept in ``output/forest_typology/index/``, so the
    next runs do not read them again. The tiles are on a grid of the UTM extent of the zone, so
    their place can not be computed from their name.
    """
    index_path = FOREST_TYPOLOGY_DIR / "index" / f"{zone}.json"
    if index_path.exists():
        return json.loads(index_path.read_text(encoding="utf-8"))

    folder = f"{SOURCE_PREFIX}/{zone}"
    paths = [f"{folder}/{name}" for name in list_folder(folder) if name.endswith(".tif")]
    logger.info(f"Index the {len(paths)} tiles of zone {zone}")

    def bounds(path: str) -> dict:
        with rasterio.open(path) as src:
            tile_box = transform_bounds(src.crs, CRS, *src.bounds, densify_pts=21)
        return {"path": path, "bbox": tile_box}

    with ThreadPoolExecutor(INDEX_WORKERS) as executor:
        index = list(executor.map(bounds, paths))
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(json.dumps(index), encoding="utf-8")
    return index


def tile_window(bbox, extent, width: int, height: int) -> tuple[int, int, int, int] | None:
    """Window (column, row, width, height) of the output grid covered by a tile box, None outside."""
    col0 = max(0, floor((bbox[0] - extent[0]) / RESOLUTION))
    col1 = min(width, ceil((bbox[2] - extent[0]) / RESOLUTION))
    row0 = max(0, floor((extent[3] - bbox[3]) / RESOLUTION))
    row1 = min(height, ceil((extent[3] - bbox[1]) / RESOLUTION))
    if col1 <= col0 or row1 <= row0:
        return None
    return col0, row0, col1 - col0, row1 - row0


def warp_tile(path: str, window: tuple[int, int, int, int], extent, folder) -> Path:
    """Reproject one tile onto its window of the output grid, the probabilities averaged.

    ``window`` is (column, row, width, height) in the output grid. GDAL reads the overview of the
    tile closest to the output resolution. Tiles of the dataset can be stored bottom up, which
    ``gdal raster reproject`` reads, unlike a virtual mosaic of gdalbuildvrt.
    """
    col, row, width, height = window
    west = extent[0] + col * RESOLUTION
    north = extent[3] - row * RESOLUTION
    bbox = (west, north - height * RESOLUTION, west + width * RESOLUTION, north)
    output = folder / path.rsplit("/", 1)[-1]
    gdal(
        "raster", "reproject",
        "--quiet",
        "--overwrite",
        f"--output-crs={CRS}",
        f"--bbox={','.join(f'{v:.7f}' for v in bbox)}",
        f"--bbox-crs={CRS}",
        f"--size={width},{height}",
        "--resampling=average",
        f"--output-nodata={NODATA}",
        "--co=COMPRESS=ZSTD",
        path,
        str(output),
    )  # fmt: skip
    return output


def run(output_path: Path):
    folder = FOREST_TYPOLOGY_DIR / f"{output_path.stem}_work"
    folder.mkdir(parents=True, exist_ok=True)

    provinces = read_provinces()
    extent = get_extent(provinces)
    width = round((extent[2] - extent[0]) / RESOLUTION)
    height = round((extent[3] - extent[1]) / RESOLUTION)
    transform = from_origin(extent[0], extent[3], RESOLUTION, RESOLUTION)
    logger.info(f"Grid of {width} x {height} pixels of {FOREST_TYPOLOGY_RESOLUTION} m over {extent}")

    zones = [zone for zone in list_zones() if intersects(zone_box(zone), extent)]
    if not zones:
        raise SystemExit(f"No Forest Typology zone over {extent}")
    logger.info(f"Zones over the AOI: {', '.join(zones)}")

    # Only the tiles over the AOI provinces, not over the rest of their bounding box
    aoi_shape = provinces.union_all()
    tiles = []
    for zone in zones:
        for tile in zone_index(zone):
            window = tile_window(tile["bbox"], extent, width, height)
            if window is not None and box(*tile["bbox"]).intersects(aoi_shape):
                tiles.append((tile["path"], window))
    if not tiles:
        raise SystemExit("No Forest Typology tile over the AOI")
    logger.info(f"Reproject {len(tiles)} tiles")

    probabilities = np.full((len(BANDS), height, width), NODATA, dtype=np.uint8)
    with ThreadPoolExecutor(WARP_WORKERS) as executor:
        futures = {executor.submit(warp_tile, path, window, extent, folder): window for path, window in tiles}
        for done, future in enumerate(as_completed(futures), start=1):
            col, row, w, h = futures[future]
            tile_path = future.result()
            with rasterio.open(tile_path) as src:
                data = src.read()
            tile_path.unlink()
            target = probabilities[:, row : row + h, col : col + w]
            # The tiles overlap a little and the zones meet, the first valid value of a pixel is kept
            fill = np.all(target == NODATA, axis=0) & np.all(data != NODATA, axis=0)
            target[:, fill] = data[:, fill]
            if done % 25 == 0 or done == len(tiles):
                logger.info(f"{done} of {len(tiles)} tiles")

    logger.info("Add OtherLand and class")
    inside = geometry_mask(provinces.geometry, out_shape=(height, width), transform=transform, invert=True)
    valid = np.all(probabilities != NODATA, axis=0) & inside
    other = np.clip(250 - probabilities.astype(np.int16).sum(axis=0), 0, 250).astype(np.uint8)
    classes = np.argmax(np.concatenate([probabilities, other[None]]), axis=0).astype(np.uint8) + 1
    output = np.concatenate([probabilities, other[None], classes[None]])
    output[:, ~valid] = NODATA

    combined = folder / "forest_typology.tif"
    profile = dict(
        driver="GTiff",
        width=width,
        height=height,
        count=len(OUTPUT_BANDS),
        dtype="uint8",
        crs=CRS,
        transform=transform,
        nodata=NODATA,
        compress="ZSTD",
        tiled=True,
    )
    with rasterio.open(combined, "w", **profile) as dst:
        dst.write(output)
        for index, name in enumerate(OUTPUT_BANDS, start=1):
            dst.set_band_description(index, name)
        dst.update_tags(
            source="Forest Typology 2020 v1 (ForTy), Google DeepMind, WRI, UMD and IIASA, CC BY 4.0",
            url="https://plus.figshare.com/articles/dataset/_i_Forest_Typology_2020_v1_i_/32288616",
            probabilities="0 to 250, 250 is a probability of 1",
            classes=", ".join(f"{i} {name}" for i, name in enumerate(CLASSES, start=1)),
        )

    logger.info("Write the Cloud Optimised GeoTIFF")
    temp_path = folder / output_path.name
    gdal(
        "raster", "convert",
        "--of=COG",
        "--co=COMPRESS=ZSTD",
        # Nearest overviews, the class band can not be averaged
        "--co=OVERVIEW_RESAMPLING=NEAREST",
        "--co=STATISTICS=YES",
        "--overwrite",
        str(combined),
        str(temp_path),
    )  # fmt: skip
    temp_path.replace(output_path)
    rmtree(folder)

    counts = np.bincount(classes[valid], minlength=len(CLASSES) + 1)[1:]
    for name, count in zip(CLASSES, counts):
        logger.info(f"{name}: {count / max(valid.sum(), 1):.1%} of the pixels")
    logger.info(f"Saved to {output_path}")


def main():
    FOREST_TYPOLOGY_DIR.mkdir(parents=True, exist_ok=True)
    output_path = FOREST_TYPOLOGY_DIR / f"{AOI_NAME}_forest_typology_{FOREST_TYPOLOGY_RESOLUTION}m.tif"
    if output_path.exists():
        logger.info(f"Skip, {output_path} exists, delete it to make it again")
        return
    run(output_path)


if __name__ == "__main__":
    main()
