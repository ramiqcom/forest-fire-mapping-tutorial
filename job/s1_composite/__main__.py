"""Optional step: Sentinel-1 radar composites of the AOI, one per period of two weeks, from NASA Earthdata.

Radar sees through the smoke and cloud of the fire season, so a composite before the fires (1 to 14 June)
and one after (1 to 14 November) show changes of the land surface that the FIRMS alerts can not show.

OPERA Radiometric Terrain Corrected backscatter from Sentinel-1 (RTC-S1, OPERA_L2_RTC-S1_V1, 30 m
bursts), found with the STAC API of NASA Earthdata (ASF) and read with an Earthdata login
(EARTHDATA_USERNAME and EARTHDATA_PASSWORD in .env). The method follows
``rs_open_source.sentinel.s1_rtc`` of remote-sensing-open-source-docker, source earthdata:

1. The AOI (the provinces of ``.env``, as for the FIRMS jobs) is split into grids of 0.5 degree on
   one pixel grid of ``S1_RESOLUTION`` metres (30 m by default) in EPSG:4326.
2. Per grid and band (VV and VH), the bursts of each date are mosaicked and reprojected (lanczos),
   scaled by 10,000 to UInt16 (gamma0 x 10,000, 0 is nodata) and smoothed with a 3 x 3 gaussian
   filter against speckle.
3. The median of the dates of the period is taken per band, and the bands are stacked.
4. The grids are mosaicked, clipped to the AOI and written as a Cloud Optimised GeoTIFF.

Writes ``output/s1/<AOI_NAME>_s1_rtc_<start>_<end>_<resolution>m.tif`` on this computer, 2 bands (vv, vh),
UInt16, gamma0 x 10,000 in linear power, 0 is nodata. A period already done is skipped, delete its
file to make it again. The periods are S1_PERIODS of .env, START/END comma separated.

Needs the GDAL 3.11 or newer command line, which is in the Docker image.
Run with ``docker compose run --rm s1_composite`` or ``python -m job.s1_composite``.
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
    cmr_orbit_ids,
    earthdata_login,
    gdal,
    grid_bbox,
    make_grids,
    mosaic_grids,
    mosaic_to_grid,
    pixel_grid,
    reproject_args,
    retry,
    stac_search,
    write_list,
)
from forest_fire.config import AOI_NAME, S1_DIR, S1_GAUSSIAN_SIZE, S1_ORBIT, S1_PERIODS, S1_RESOLUTION

basicConfig(level=INFO, format="%(asctime)s - %(name)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
logger = getLogger("job.s1_composite")

# OPERA RTC-S1 in the STAC API of NASA Earthdata (ASF), and its CMR short name for the orbit filter
STAC_API = "https://cmr.earthdata.nasa.gov/stac/ASF"
COLLECTION = "OPERA_L2_RTC-S1_V1_1"
CMR_SHORT_NAME = "OPERA_L2_RTC-S1_V1"
BANDS = ["vv", "vh"]
ORBITS = ["both", "ascending", "descending"]


def asset_key(band: str) -> str:
    return f"0_{band.upper()}"


def date_image(links: list[str], bbox, shape, folder: Path, name: str) -> str:
    """One date and band on the grid: mosaic, reproject, scale to UInt16 and gaussian filter."""
    mosaic = Path(mosaic_to_grid(links, bbox, shape, "lanczos", folder / f"{name}_mosaic.tif", folder, name))
    scaled = folder / f"{name}.tif"
    steps = [
        "raster", "pipeline", "--quiet",
        "!", "calc", "-i", f"A={mosaic}", "--calc=A * 10000", "--propagate-nodata", "--nodata=0", "--ot=UInt16",
    ]  # fmt: skip
    if S1_GAUSSIAN_SIZE:
        steps += [
            "!",
            "neighbors",
            "--kernel=gaussian",
            f"--size={S1_GAUSSIAN_SIZE}",
            "--nodata=0",
            "--ot=UInt16",
        ]
    gdal(*steps, "!", "write", "--overwrite", "--co=COMPRESS=ZSTD", str(scaled))
    mosaic.unlink()
    return str(scaled)


def grid_composite(grid: dict, items: list[dict], folder: Path) -> str | None:
    """Median of the dates of the period per band, the bands stacked, for one grid."""
    bbox = grid_bbox(grid)
    shape = (grid["width"], grid["height"])
    grid_box = box(*bbox)
    # Only the bursts whose footprint covers part of the grid
    items = [item for item in items if grid_box.intersects(box(*item["bbox"]))]
    dates = sorted({item["properties"]["datetime"][:10] for item in items})
    if not dates:
        logger.warning(f"Grid {grid['grid_id']}: no Sentinel-1 image")
        return None

    work = folder / grid["grid_id"]
    work.mkdir(parents=True, exist_ok=True)
    medians = []
    for band in BANDS:
        with ThreadPoolExecutor(TASKS) as executor:
            jobs = [
                executor.submit(
                    retry,
                    date_image,
                    [
                        f"/vsicurl/{item['assets'][asset_key(band)]['href']}"
                        for item in items
                        if item["properties"]["datetime"][:10] == date
                    ],
                    bbox,
                    shape,
                    work,
                    f"{band}_{date}",
                )
                for date in dates
            ]
            images = []
            for date, job in zip(dates, jobs):
                try:
                    images.append(job.result())
                except Exception as e:
                    logger.warning(f"Grid {grid['grid_id']} {band} {date} left out: {e}")
        if not images:
            raise RuntimeError(f"Grid {grid['grid_id']}: no {band} image could be read")

        median = work / f"{band}_median.tif"
        gdal(
            "raster", "pipeline", "--quiet",
            "!", "mosaic", "-i", write_list(work / f"{band}_dates.txt", images),
            f"--bbox={','.join(map(str, bbox))}", "--pixel-function=median", "--resolution=average",
            "!", "reproject", *reproject_args(bbox, shape),
            "!", "write", "--overwrite", "--co=COMPRESS=ZSTD", "--co=PREDICTOR=2", str(median),
        )  # fmt: skip
        for image in images:
            Path(image).unlink()
        medians.append(str(median))

    stack = folder / f"{grid['grid_id']}.tif"
    gdal(
        "raster", "pipeline", "--quiet",
        "!", "stack", "--resolution=average", "-i", write_list(work / "bands.txt", medians),
        "!", "reproject", *reproject_args(bbox, shape), "--output-nodata=0",
        "!", "write", "--overwrite", "--co=COMPRESS=ZSTD", "--co=PREDICTOR=2", str(stack),
    )  # fmt: skip
    rmtree(work)
    logger.info(f"Grid {grid['grid_id']} done, {len(dates)} dates")
    return str(stack)


def run(start: str, end: str, output_path: Path):
    period = f"{start} to {end}"
    folder = S1_DIR / f"{output_path.stem}_work"
    folder.mkdir(parents=True, exist_ok=True)

    provinces = read_provinces()
    roi_path = folder / "roi.fgb"
    provinces.to_file(roi_path, driver="FlatGeobuf")
    bbox, shape, pixel = pixel_grid(provinces, S1_RESOLUTION)
    grids = make_grids(provinces, bbox, shape, pixel)

    items = retry(stac_search, STAC_API, COLLECTION, bbox, start, end)
    if S1_ORBIT != "both":
        ids = retry(cmr_orbit_ids, CMR_SHORT_NAME, bbox, start, end, S1_ORBIT)
        items = [item for item in items if item["id"] in ids]
    dates = sorted({item["properties"]["datetime"][:10] for item in items})
    logger.info(
        f"{period}: {len(items)} OPERA RTC-S1 bursts on {len(dates)} dates ({S1_ORBIT} orbits), "
        f"{len(grids)} grids, {shape[0]} x {shape[1]} pixels of {S1_RESOLUTION} m"
    )
    if not items:
        logger.warning(f"No OPERA RTC-S1 burst from {start} to {end}, skipped")
        rmtree(folder)
        return

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
        raise SystemExit(f"{period}: no grid could be made")
    if len(results) < len(grids):
        logger.warning(f"{period}: {len(grids) - len(results)} of {len(grids)} grids missing in the mosaic")

    logger.info("Mosaic the grids, clip to the AOI and write the Cloud Optimised GeoTIFF")
    temp_path = folder / output_path.name
    mosaic_grids(results, roi_path, bbox, shape, temp_path, folder, resampling="LANCZOS", nodata=0)
    with rasterio.open(temp_path, "r+", IGNORE_COG_LAYOUT_BREAK="YES") as dst:
        dst.descriptions = tuple(BANDS)
        dst.update_tags(
            source="OPERA RTC-S1 (OPERA_L2_RTC-S1_V1), NASA JPL OPERA, from ESA Copernicus Sentinel-1, via ASF DAAC",
            period=f"{start} to {end}",
            dates=", ".join(dates),
            orbit=S1_ORBIT,
            values="gamma0 x 10000, linear power, median of the dates, 0 is nodata",
            gaussian_filter=f"{S1_GAUSSIAN_SIZE} x {S1_GAUSSIAN_SIZE}" if S1_GAUSSIAN_SIZE else "none",
        )
    temp_path.replace(output_path)
    rmtree(folder)
    logger.info(f"{period} saved to {output_path}")


def main():
    if S1_ORBIT not in ORBITS:
        raise SystemExit(f"S1_ORBIT must be one of {ORBITS}, got {S1_ORBIT}")
    if S1_GAUSSIAN_SIZE not in (0, 3, 5):
        raise SystemExit(f"S1_GAUSSIAN_SIZE must be 0 (no filter), 3 or 5, got {S1_GAUSSIAN_SIZE}")
    earthdata_login()
    S1_DIR.mkdir(parents=True, exist_ok=True)
    for start, end in S1_PERIODS:
        output_path = S1_DIR / f"{AOI_NAME}_s1_rtc_{start}_{end}_{S1_RESOLUTION}m.tif"
        if output_path.exists():
            logger.info(f"Skip {start} to {end}, {output_path} exists, delete it to make it again")
            continue
        logger.info(f"Composite of {start} to {end}")
        run(start, end, output_path)


if __name__ == "__main__":
    main()
