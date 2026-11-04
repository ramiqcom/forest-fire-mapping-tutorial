"""Step 2: count the FIRMS alerts of the AOI per grid cell of about 1 km.

Reads ``output/firms/`` from ``job.firms`` and writes ``output/firms_grid/<AOI_NAME>_<period>.tif``
on this computer, a Cloud Optimised GeoTIFF with the number of alerts per 0.009° cell. Cells
outside the AOI provinces or on ESA WorldCover permanent water are nodata. Skipped when the grid
already exists, delete it to make it again.

Needs the GDAL 3.11 or newer command line (``gdal vector grid``, ``gdal raster calc``), which is in
the Docker image. Run with ``docker compose run --rm firms_grid`` or ``python -m job.firms_grid``.
"""

from logging import INFO, basicConfig, getLogger
from math import ceil, floor
from shutil import rmtree, which
from subprocess import check_call

import geopandas as gpd

from forest_fire.aoi import read_provinces
from forest_fire.config import AOI_NAME, DATE_NAME, FIRMS_DIR, FIRMS_GRID_DIR

basicConfig(level=INFO, format="%(asctime)s - %(name)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
logger = getLogger("job.firms_grid")

CRS = "EPSG:4326"
# About 1 km at the equator, cells are narrower away from it (about 0.95 km at 18° S)
RESOLUTION = 0.009
# Count the fire points within half a cell from the cell centre
RADIUS = RESOLUTION / 2
NODATA = 65535
COMPRESS = "--co=COMPRESS=ZSTD"

# ESA WorldCover 2021 v200 (10 m, CC BY 4.0) on the public AWS bucket, one 3° tile per file
WORLDCOVER_BUCKET = "https://esa-worldcover.s3.eu-central-1.amazonaws.com"
WORLDCOVER_TILES = f"/vsicurl/{WORLDCOVER_BUCKET}/esa_worldcover_grid.fgb"
WORLDCOVER_TILE = f"/vsicurl/{WORLDCOVER_BUCKET}/v200/2021/map/ESA_WorldCover_10m_2021_v200_{{}}_Map.tif"
# WorldCover class of permanent water bodies
WORLDCOVER_WATER = 80


def gdal(*args: str):
    """Run one ``gdal`` command line, e.g. ``gdal("vector", "concat", ...)``."""
    executable = which("gdal")
    if executable is None:
        raise RuntimeError("The gdal command line (GDAL 3.11 or newer) is not installed, use Docker")
    check_call([executable, *args])


def get_extent(aoi: gpd.GeoDataFrame) -> tuple[float, float, float, float]:
    """Bounds of ``aoi`` snapped outward to the cell size, so the cells of all files line up."""
    xmin, ymin, xmax, ymax = aoi.to_crs(CRS).total_bounds
    return (
        round(floor(xmin / RESOLUTION) * RESOLUTION, 4),
        round(floor(ymin / RESOLUTION) * RESOLUTION, 4),
        round(ceil(xmax / RESOLUTION) * RESOLUTION, 4),
        round(ceil(ymax / RESOLUTION) * RESOLUTION, 4),
    )


def worldcover_on_grid(extent: tuple[float, float, float, float], like: str, folder, output: str):
    """WorldCover of ``extent`` on the grid of ``like``, the most common class per cell.

    The WorldCover files have overviews made of class values, so GDAL reads the overview
    closest to 1 km instead of every 10 m pixel.
    """
    tiles = gpd.read_file(WORLDCOVER_TILES, bbox=extent, columns=["ll_tile"])
    if tiles.empty:
        raise RuntimeError(f"No WorldCover tile over {extent}")
    logger.info(f"Mosaic {len(tiles)} WorldCover tiles")
    mosaic = str(folder / "worldcover.vrt")
    gdal(
        "raster", "mosaic",
        "--of=VRT",
        "--overwrite",
        *[WORLDCOVER_TILE.format(tile) for tile in tiles["ll_tile"]],
        mosaic,
    )  # fmt: skip
    gdal(
        "raster", "reproject",
        f"--like={like}",
        "--resampling=mode",
        COMPRESS,
        "--overwrite",
        mosaic,
        output,
    )  # fmt: skip


def run(name: str, output_path):
    provinces = read_provinces()

    # One FIRMS file per province from job.firms, all needed for a complete grid
    firms_paths = [FIRMS_DIR / f"{province}_{DATE_NAME}.fgb" for province in provinces.index]
    missing = [str(path) for path in firms_paths if not path.exists()]
    if missing:
        raise SystemExit(f"Missing {', '.join(missing)}, run job.firms first")

    # Intermediate files, removed at the end
    folder = FIRMS_GRID_DIR / f"{name}_work"
    folder.mkdir(parents=True, exist_ok=True)
    extent = get_extent(provinces)
    extent_arg = f"--extent={','.join(map(str, extent))}"
    roi_path = folder / "roi.fgb"
    provinces.to_file(roi_path, driver="FlatGeobuf")

    logger.info(f"Merge the fire alerts of {len(firms_paths)} provinces")
    merged_path = folder / "firms_merged.fgb"
    gdal(
        "vector", "concat",
        "--mode=single",
        "--output-layer=FIRMS",
        f"--output-crs={CRS}",
        "--overwrite",
        *map(str, firms_paths),
        str(merged_path),
    )  # fmt: skip

    logger.info("Count the fire alerts per cell")
    grid_path = folder / "firms_grid.tif"
    gdal(
        "vector", "grid", "count",
        extent_arg,
        f"--resolution={RESOLUTION},{RESOLUTION}",
        f"--radius={RADIUS}",
        "--ot=UInt16",
        f"--nodata={NODATA}",
        COMPRESS,
        "--co=TILED=YES",
        "--overwrite",
        str(merged_path),
        str(grid_path),
    )  # fmt: skip

    logger.info("WorldCover on the same grid")
    worldcover_grid_path = folder / "worldcover_grid.tif"
    worldcover_on_grid(extent, str(grid_path), folder, str(worldcover_grid_path))

    # The AOI as 1 on the same grid. A cell is inside when its centre is inside, so coastal
    # cells that are mostly sea are left out
    logger.info("Rasterize the AOI")
    roi_grid_path = folder / "roi_grid.tif"
    gdal(
        "vector", "rasterize",
        "--burn=1",
        "--init=0",
        "--ot=UInt8",
        extent_arg,
        f"--resolution={RESOLUTION},{RESOLUTION}",
        COMPRESS,
        "--overwrite",
        str(roi_path),
        str(roi_grid_path),
    )  # fmt: skip

    logger.info("Mask the sea, outside the AOI and water")
    land_path = folder / "firms_grid_land.tif"
    gdal(
        "raster", "calc",
        f"--input=A={grid_path}",
        f"--input=B={worldcover_grid_path}",
        f"--input=R={roi_grid_path}",
        f"--calc=R == 1 && B != {WORLDCOVER_WATER} ? A : {NODATA}",
        "--ot=UInt16",
        f"--nodata={NODATA}",
        COMPRESS,
        "--co=TILED=YES",
        "--overwrite",
        str(land_path),
    )  # fmt: skip

    logger.info("Write the Cloud Optimised GeoTIFF")
    temp_path = folder / output_path.name
    gdal(
        "raster", "convert",
        "--of=COG",
        COMPRESS,
        # Statistics and overviews to stretch and view it in a GIS app such as QGIS
        "--co=STATISTICS=YES",
        "--co=RESAMPLING=LANCZOS",
        "--co=OVERVIEW_RESAMPLING=LANCZOS",
        "--overwrite",
        str(land_path),
        str(temp_path),
    )  # fmt: skip
    temp_path.replace(output_path)
    rmtree(folder)
    logger.info(f"{name} saved to {output_path}")


def main():
    FIRMS_GRID_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{AOI_NAME}_{DATE_NAME}"
    output_path = FIRMS_GRID_DIR / f"{name}.tif"
    if output_path.exists():
        logger.info(f"Skip {name}, {output_path} exists, delete it to make it again")
        return
    logger.info(f"Grid {name}")
    run(name, output_path)


if __name__ == "__main__":
    main()
