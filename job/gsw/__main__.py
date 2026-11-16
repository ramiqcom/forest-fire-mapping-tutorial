"""Optional step: JRC Global Surface Water of the AOI, where and how often there was water from 1984 to 2024.

JRC Global Surface Water v1.5 (Pekel et al. 2016, 1984-2024), mapped from the whole Landsat archive
by the European Commission Joint Research Centre and Google, read without a login from its public
10 x 10 degree Cloud Optimised GeoTIFF tiles in EPSG:4326 (about 30 m), see
https://global-surface-water.appspot.com/download. The method follows ``rs_open_source.jrc.gsw`` of
remote-sensing-open-source-docker:

1. The AOI (the provinces of ``.env``) is split into grids of 0.5 degree on one pixel grid of
   ``GSW_RESOLUTION`` metres (30 m by default) in EPSG:4326.
2. Per grid and layer, the tiles over the grid are mosaicked and reprojected onto the grid: the
   percentages (occurrence, recurrence) with the average, the classes and counts with the most
   common value.
3. The layers are stacked, one band per layer.
4. The grids are mosaicked, clipped to the AOI and written as a Cloud Optimised GeoTIFF.

255 is nodata in every band. Writes ``output/gsw/<AOI_NAME>_gsw_<resolution>m.tif`` on this computer.
A file already made is skipped, delete it to make it again.

Run with ``docker compose run --rm gsw`` or ``python -m job.gsw``.
"""

import math
from concurrent.futures import ThreadPoolExecutor
from functools import cache
from logging import INFO, basicConfig, getLogger
from pathlib import Path
from shutil import rmtree
from urllib.request import urlopen

import rasterio

from forest_fire.aoi import read_provinces
from forest_fire.composite import (
    TASKS,
    gdal,
    grid_bbox,
    make_grids,
    mosaic_grids,
    pixel_grid,
    reproject_args,
    retry,
    write_list,
)
from forest_fire.config import AOI_NAME, GSW_DIR, GSW_LAYERS, GSW_RESOLUTION

basicConfig(level=INFO, format="%(asctime)s - %(name)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
logger = getLogger("job.gsw")

# JRC Global Surface Water v1.5 (1984-2024), 10 x 10 degree tiles at 0.00025 degree
CONTAINER = "https://s3.waw4-1.cloudferro.com/swift/v1/global-surface-water"
FOLDER = "download2024/Aggregated/VER1-5"
VERSION = "v1_5_2024"
LAYERS = ["occurrence", "change", "seasonality", "recurrence", "transitions", "extent"]
# 255 is nodata in occurrence, change, seasonality and extent; recurrence (0-100) and transitions
# (0-10) never use it, so it is the nodata of every band
NODATA = 255
# The percentages are averaged, the classes and the counts take the most common value
RESAMPLING = dict(occurrence="average", recurrence="average")


@cache
def layer_names(layer: str) -> frozenset[str]:
    """Files of a layer in the bucket listing. The slash before the query keeps the prefix."""
    with urlopen(f"{CONTAINER}/?prefix={FOLDER}/{layer}/", timeout=60) as response:
        return frozenset(response.read().decode().split())


def tile_paths(layer: str, bbox) -> list[str]:
    """Tiles of a layer over ``bbox``. A tile is named after its west and north edges, e.g.
    occurrence_110E_0N covers 110 to 120 degree east and 10 degree south to 0. There are no tiles
    over the open ocean, so only the tiles in the listing are kept."""
    min_x, min_y, max_x, max_y = bbox
    wests = range(math.floor(min_x / 10) * 10, math.ceil(max_x / 10) * 10, 10)
    norths = range(math.floor(min_y / 10) * 10 + 10, math.ceil(max_y / 10) * 10 + 1, 10)
    available = layer_names(layer)
    paths = []
    for west in wests:
        for north in norths:
            lon = f"{abs(west)}{'E' if west >= 0 else 'W'}"
            lat = f"{abs(north)}{'N' if north >= 0 else 'S'}"
            name = f"{FOLDER}/{layer}/{layer}_{lon}_{lat}_{VERSION}.tif"
            if name in available:
                paths.append(f"/vsicurl/{CONTAINER}/{name}")
    return paths


def layer_image(layer: str, bbox, shape, output: Path) -> str:
    """One layer on the grid, the tiles mosaicked over the grid and reprojected onto it."""
    paths = tile_paths(layer, bbox)
    if not paths:
        raise ValueError(f"No Global Surface Water tile of {layer} over {bbox}")
    gdal(
        "raster", "pipeline", "--quiet",
        "!", "mosaic", "--resolution=average", f"--input-nodata={NODATA}",
        f"--bbox={','.join(map(str, bbox))}", "-i", write_list(output.with_suffix(".txt"), paths),
        "!", "edit", "--color-interpretation=all=undefined",
        "!", "reproject", *reproject_args(bbox, shape, RESAMPLING.get(layer, "mode")),
        f"--output-nodata={NODATA}",
        "!", "write", "--overwrite", "--co=COMPRESS=ZSTD", str(output),
    )  # fmt: skip
    return str(output)


def grid_image(grid: dict, folder: Path) -> str:
    """The layers of one grid, one band per layer."""
    bbox = grid_bbox(grid)
    shape = (grid["width"], grid["height"])
    work = folder / grid["grid_id"]
    work.mkdir(parents=True, exist_ok=True)
    bands = [retry(layer_image, layer, bbox, shape, work / f"{layer}.tif") for layer in GSW_LAYERS]
    output = folder / f"{grid['grid_id']}.tif"
    gdal(
        "raster", "pipeline", "--quiet",
        "!", "stack", "--resolution=average", "-i", write_list(work / "bands.txt", bands),
        "!", "write", "--overwrite", "--co=COMPRESS=ZSTD", str(output),
    )  # fmt: skip
    rmtree(work)
    logger.info(f"Grid {grid['grid_id']} done")
    return str(output)


def run(output_path: Path):
    provinces = read_provinces()
    bbox, shape, pixel = pixel_grid(provinces, GSW_RESOLUTION)
    folder = GSW_DIR / f"{output_path.stem}_work"
    folder.mkdir(parents=True, exist_ok=True)
    roi_path = folder / "roi.fgb"
    provinces.to_file(roi_path, driver="FlatGeobuf")
    grids = make_grids(provinces, bbox, shape, pixel)
    logger.info(
        f"{', '.join(GSW_LAYERS)}: {len(grids)} grids, {shape[0]} x {shape[1]} pixels of {GSW_RESOLUTION} m"
    )

    with ThreadPoolExecutor(TASKS) as executor:
        jobs = [executor.submit(grid_image, grid, folder) for grid in grids]
        results = []
        for grid, job in zip(grids, jobs):
            try:
                results.append(job.result())
            except Exception as e:
                logger.error(f"Grid {grid['grid_id']} failed: {e}")
    if len(results) < len(grids):
        raise SystemExit(
            f"{len(grids) - len(results)} of {len(grids)} grids failed, run the job again, "
            "the grids already done are made again"
        )

    logger.info("Mosaic the grids, clip to the AOI and write the Cloud Optimised GeoTIFF")
    temp_path = folder / output_path.name
    mosaic_grids(results, roi_path, bbox, shape, temp_path, folder, resampling="MODE", nodata=NODATA)
    with rasterio.open(temp_path, "r+", IGNORE_COG_LAYOUT_BREAK="YES") as dst:
        dst.descriptions = tuple(GSW_LAYERS)
        dst.update_tags(
            source="JRC Global Surface Water v1.5 (1984-2024), EC JRC and Google, "
            "https://global-surface-water.appspot.com",
            nodata=str(NODATA),
        )
    temp_path.replace(output_path)
    rmtree(folder)
    logger.info(f"Saved to {output_path}")


def main():
    unknown = [layer for layer in GSW_LAYERS if layer not in LAYERS]
    if unknown or not GSW_LAYERS:
        raise SystemExit(f"GSW_LAYERS must be some of {LAYERS}, got {GSW_LAYERS}")
    GSW_DIR.mkdir(parents=True, exist_ok=True)
    output_path = GSW_DIR / f"{AOI_NAME}_gsw_{GSW_RESOLUTION}m.tif"
    if output_path.exists():
        logger.info(f"Skip, {output_path} exists, delete it to make it again")
        return
    run(output_path)


if __name__ == "__main__":
    main()
