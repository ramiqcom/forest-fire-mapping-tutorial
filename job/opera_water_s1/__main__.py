"""Optional step: OPERA surface water from Sentinel-1 of the AOI, one composite per month.

OPERA Dynamic Surface Water Extent from Sentinel-1 (DSWx-S1, OPERA_L3_DSWX-S1_V1, 30 m MGRS tiles),
found with the STAC API of NASA Earthdata (PO.DAAC) and read with an Earthdata login
(EARTHDATA_USERNAME and EARTHDATA_PASSWORD in .env). Radar sees water through cloud and smoke, so
the composites show how the rivers, lakes and flooded peat changed from before the fire season to
its end. The method follows ``rs_open_source.opera.surface_water.s1`` of
remote-sensing-open-source-docker:

1. The AOI (the provinces of ``.env``) is split into grids of 0.5 degree on one pixel grid of
   ``OPERA_WATER_RESOLUTION`` metres (30 m by default) in EPSG:4326.
2. Per grid, the water classification (WTR layer) of each date is mosaicked and reprojected with
   the most common class.
3. The most common class of the dates of the month is kept.
4. The grids are mosaicked, clipped to the AOI and written as a Cloud Optimised GeoTIFF with the
   colour table of DSWx-S1.

Classes of the WTR layer: 0 not water, 1 open water, 3 inundated vegetation, 250 masked by the
height above nearest drainage (HAND), 251 masked for radar layover or shadow, 255 nodata.

Writes ``output/opera_water_s1/<AOI_NAME>_opera_water_s1_<month>_<resolution>m.tif`` on this
computer. A month without any DSWx-S1 granule over the AOI is skipped with a warning; a month
already done is skipped, delete its file to make it again.

Run with ``docker compose run --rm opera_water_s1`` or ``python -m job.opera_water_s1``.
"""

from concurrent.futures import ThreadPoolExecutor
from logging import INFO, basicConfig, getLogger
from pathlib import Path
from shutil import rmtree

import rasterio
from shapely.geometry import box

from forest_fire.aoi import read_provinces
from forest_fire.composite import (
    TASKS,
    earthdata_login,
    gdal,
    grid_bbox,
    make_grids,
    month_range,
    mosaic_grids,
    pixel_grid,
    reproject_args,
    retry,
    stac_search,
    write_list,
)
from forest_fire.config import AOI_NAME, OPERA_WATER_DIR, OPERA_WATER_MONTHS, OPERA_WATER_RESOLUTION

basicConfig(level=INFO, format="%(asctime)s - %(name)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
logger = getLogger("job.opera_water_s1")

# OPERA DSWx-S1 in the STAC API of NASA Earthdata (PO.DAAC), the water classification layer
STAC_API = "https://cmr.earthdata.nasa.gov/cloudstac/POCLOUD"
COLLECTION = "OPERA_L3_DSWX-S1_V1_1.0"
ASSET = "0_B01_WTR"
NODATA = 255

# Colours of the WTR layer, the palette of the DSWx-S1 product specification (JPL D-108761, table 4-1),
# as in rs_open_source
COLORS = {
    0: "ffffff", 1: "0000ff", 2: "7878f0", 3: "00ff00", 5: "00ff00", 6: "f01414", 7: "c8c832", 8: "00ffff",
    30: "006400", 31: "00327f", 32: "3c6e78", 35: "32b132", 36: "783c14", 37: "649619",
    250: "c8c8c8", 251: "808080", 254: "3232f0",
}  # fmt: skip
CLASSES = {
    0: "Not water",
    1: "Open water",
    3: "Inundated vegetation",
    250: "Height Above Nearest Drainage (HAND) masked",
    251: "Layover/shadow masked",
}


def date_image(links: list[str], bbox, shape, folder: Path, name: str) -> str:
    """The water classes of one date on the grid, the most common class per pixel."""
    output = folder / f"{name}.tif"
    gdal(
        "raster", "pipeline", "--quiet",
        "!", "mosaic", "--resolution=average", "-i", write_list(folder / f"{name}.txt", links),
        "!", "reproject", *reproject_args(bbox, shape, "mode"),
        "!", "write", "--overwrite", "--co=COMPRESS=ZSTD", str(output),
    )  # fmt: skip
    return str(output)


def grid_composite(grid: dict, items: list[dict], folder: Path) -> str | None:
    """The most common water class of the dates of the month, for one grid."""
    bbox = grid_bbox(grid)
    shape = (grid["width"], grid["height"])
    grid_box = box(*bbox)
    items = [item for item in items if grid_box.intersects(box(*item["bbox"]))]
    # Sorted, so the ties of the most common class are the same in every run
    dates = sorted({item["properties"]["datetime"][:10] for item in items})
    if not dates:
        logger.warning(f"Grid {grid['grid_id']}: no DSWx-S1 granule")
        return None

    work = folder / grid["grid_id"]
    work.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(TASKS) as executor:
        jobs = [
            executor.submit(
                retry,
                date_image,
                [
                    f"/vsicurl/{item['assets'][ASSET]['href']}"
                    for item in items
                    if item["properties"]["datetime"][:10] == date
                ],
                bbox,
                shape,
                work,
                date,
            )
            for date in dates
        ]
        images = []
        for date, job in zip(dates, jobs):
            try:
                images.append(job.result())
            except Exception as e:
                logger.warning(f"Grid {grid['grid_id']} {date} left out: {e}")
    if not images:
        raise RuntimeError(f"Grid {grid['grid_id']}: no DSWx-S1 granule could be read")

    output = folder / f"{grid['grid_id']}.tif"
    gdal(
        "raster", "pipeline", "--quiet",
        "!", "mosaic", "-i", write_list(work / "dates.txt", images),
        f"--bbox={','.join(map(str, bbox))}", "--pixel-function=mode", "--resolution=average",
        "!", "reproject", *reproject_args(bbox, shape),
        "!", "write", "--overwrite", "--co=COMPRESS=ZSTD", str(output),
    )  # fmt: skip
    rmtree(work)
    logger.info(f"Grid {grid['grid_id']} done, {len(dates)} dates")
    return str(output)


def run(month: str, output_path: Path):
    start, end = month_range(month)
    provinces = read_provinces()
    bbox, shape, pixel = pixel_grid(provinces, OPERA_WATER_RESOLUTION)

    items = retry(stac_search, STAC_API, COLLECTION, bbox, start, end)
    dates = sorted({item["properties"]["datetime"][:10] for item in items})
    if not items:
        logger.warning(
            f"No DSWx-S1 granule over the AOI from {start} to {end}, {month} skipped. "
            "OPERA does not produce every month everywhere, try a month before or after"
        )
        return

    folder = OPERA_WATER_DIR / f"{output_path.stem}_work"
    folder.mkdir(parents=True, exist_ok=True)
    roi_path = folder / "roi.fgb"
    provinces.to_file(roi_path, driver="FlatGeobuf")
    grids = make_grids(provinces, bbox, shape, pixel)
    logger.info(
        f"{month}: {len(items)} DSWx-S1 granules on {len(dates)} dates, {len(grids)} grids, "
        f"{shape[0]} x {shape[1]} pixels of {OPERA_WATER_RESOLUTION} m"
    )

    with ThreadPoolExecutor(TASKS) as executor:
        jobs = [executor.submit(grid_composite, grid, items, folder) for grid in grids]
        results = []
        for grid, job in zip(grids, jobs):
            try:
                result = job.result()
            except Exception as e:
                logger.error(f"Grid {grid['grid_id']} failed: {e}")
                continue
            if result is not None:
                results.append(result)
    if not results:
        raise SystemExit(f"{month}: no grid could be made")
    if len(results) < len(grids):
        logger.warning(
            f"{month}: {len(grids) - len(results)} of {len(grids)} grids without data in the mosaic"
        )

    logger.info("Mosaic the grids, clip to the AOI and write the Cloud Optimised GeoTIFF")
    temp_path = folder / output_path.name
    mosaic_grids(results, roi_path, bbox, shape, temp_path, folder, resampling="MODE", nodata=NODATA)
    with rasterio.open(temp_path, "r+", IGNORE_COG_LAYOUT_BREAK="YES") as dst:
        dst.descriptions = ("WTR",)
        dst.write_colormap(
            1,
            {value: tuple(int(c[k : k + 2], 16) for k in (0, 2, 4)) + (255,) for value, c in COLORS.items()},
        )
        dst.update_tags(
            source="OPERA DSWx-S1 (OPERA_L3_DSWX-S1_V1), NASA JPL OPERA, from ESA Copernicus Sentinel-1, via PO.DAAC",
            period=f"{start} to {end}",
            dates=", ".join(dates),
            values="most common class of the dates: " + ", ".join(f"{k} {v}" for k, v in CLASSES.items()),
        )
    temp_path.replace(output_path)
    rmtree(folder)
    logger.info(f"{month} saved to {output_path}")


def main():
    earthdata_login()
    OPERA_WATER_DIR.mkdir(parents=True, exist_ok=True)
    for month in OPERA_WATER_MONTHS:
        output_path = OPERA_WATER_DIR / f"{AOI_NAME}_opera_water_s1_{month}_{OPERA_WATER_RESOLUTION}m.tif"
        if output_path.exists():
            logger.info(f"Skip {month}, {output_path} exists, delete it to make it again")
            continue
        logger.info(f"Composite of {month}")
        run(month, output_path)


if __name__ == "__main__":
    main()
