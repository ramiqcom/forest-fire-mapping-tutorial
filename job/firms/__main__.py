"""Step 1: download the FIRMS fire alerts of every province of the AOI.

The AOI is set in ``.env`` with COUNTRY and PROVINCES, see ``forest_fire.aoi``. Writes
``output/firms/<province>_<period>.fgb`` on this computer. A province already downloaded is
skipped, so the job can be run again after a failure. Delete a file to download it again.

Run with ``docker compose run --rm firms`` or ``python -m job.firms``.
"""

from logging import INFO, basicConfig, getLogger

from forest_fire.aoi import read_provinces
from forest_fire.config import (
    COUNTRY,
    DATE_NAME,
    END_DATE,
    FIRMS_DIR,
    FIRMS_MAP_KEY,
    FIRMS_SOURCES,
    START_DATE,
)
from forest_fire.firms import check_map_key, data_availability, download_firms

basicConfig(level=INFO, format="%(asctime)s - %(name)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
logger = getLogger("job.firms")


def main():
    check_map_key(FIRMS_MAP_KEY)
    availability = data_availability(FIRMS_MAP_KEY)
    provinces = read_provinces()
    FIRMS_DIR.mkdir(parents=True, exist_ok=True)
    logger.info(
        f"FIRMS {', '.join(FIRMS_SOURCES)} from {START_DATE} to {END_DATE}, "
        f"provinces of {COUNTRY} ({len(provinces)}): {', '.join(provinces['name'])}"
    )

    failed = []
    for province in provinces.index:
        name = f"{province}_{DATE_NAME}"
        output_path = FIRMS_DIR / f"{name}.fgb"
        if output_path.exists():
            logger.info(f"Skip {name}, {output_path} exists")
            continue

        # One province at a time, the requests of a province already run in parallel and
        # all of them share the transaction limit of the MAP_KEY
        logger.info(f"Download FIRMS {name}")
        try:
            alerts = download_firms(
                provinces.loc[[province]],
                START_DATE,
                END_DATE,
                FIRMS_MAP_KEY,
                FIRMS_SOURCES,
                availability,
            )
        except Exception as e:
            logger.error(f"Error {name}: {e}")
            failed.append(name)
            continue

        alerts["province"] = province
        # Written to a temporary name first, so a stopped job never leaves a partial file
        # that the next run would skip
        temp_path = output_path.with_suffix(".tmp.fgb")
        alerts.to_file(temp_path, driver="FlatGeobuf", layer="FIRMS")
        temp_path.replace(output_path)
        logger.info(f"{name}: {len(alerts):,} alerts saved to {output_path}")

    if failed:
        raise SystemExit(f"Failed: {', '.join(failed)}, run the job again to retry them")


if __name__ == "__main__":
    main()
