# Tutorial: make a forest fire map with NASA FIRMS #

This tutorial turns NASA FIRMS satellite fire alerts into forest fire maps, in three steps:

1. **Get the FIRMS data**: download the fire alerts of every province of your area for a period.
2. **Make the FIRMS grid**: count the alerts per cell of about 1 km, with the sea and water masked.
3. **Run the notebook**: make the maps of alerts, forest, fire count and alerts per district.

You choose the area of interest (AOI) as input: a list of provinces from [GADM](https://gadm.org/), the only boundary source of the tutorial. The example covers August to October 2026 in one province of Indonesia, Kalimantan Tengah (Central Kalimantan), so it runs in minutes. You can change the provinces and the period, see [Choose your area and period](#choose-your-area-and-period).

Everything runs and stays on your computer: every step writes to the local `output/` folder and nothing is uploaded to a cloud bucket. The internet is only used to read the public input data.

The only credential you need is a free NASA FIRMS MAP_KEY. Every other dataset is public.

## Contents ##

- [How it works](#how-it-works)
- [What you need](#what-you-need)
- [Step 0: set up the project and credentials](#step-0-set-up-the-project-and-credentials)
- [Step 1: get the FIRMS data](#step-1-get-the-firms-data)
- [Step 2: make the FIRMS grid](#step-2-make-the-firms-grid)
- [Step 2b: get the Forest Typology 2020 land cover](#step-2b-get-the-forest-typology-2020-land-cover)
- [Step 3: run the notebook](#step-3-run-the-notebook)
- [Choose your area and period](#choose-your-area-and-period)
- [Running without Docker](#running-without-docker)
- [Troubleshooting](#troubleshooting)
- [Data, licences and limits](#data-licences-and-limits)

## How it works ##

```
 .env: COUNTRY, PROVINCES ──► GADM provinces and regencies, cached in output/gadm/
        │
 NASA FIRMS area API                 ESA WorldCover (water)        Natural Earth cities                
 (needs FIRMS_MAP_KEY)               public AWS bucket             (public)
        │                                   │                                  │
        ▼                                   ▼                                  ▼
 Step 1  job.firms  ──────────►  Step 2  job.firms_grid  ──────────►  Step 3  notebook/firms.ipynb
 output/firms/                   output/firms_grid/                   output/figures/
 <province>_<period>.fgb         <AOI_NAME>_<period>.tif              *.png
 one point per alert             alerts per ~1 km cell                maps, alerts per land cover
                                                                             ▲
 Forest Typology 2020 (public Space4Good copy in Cloud Storage)              │
        └──────────► Step 2b  job.forest_typology ──────────────────────────┘
                     output/forest_typology/<AOI_NAME>_forest_typology_100m.tif
```

| Path | What it is |
|---|---|
| [src/forest_fire/config.py](src/forest_fire/config.py) | Settings shared by every step: AOI, period, FIRMS sources, output folders. Read from `.env` |
| [src/forest_fire/aoi.py](src/forest_fire/aoi.py) | Reads the GADM provinces and regencies of the AOI |
| [src/forest_fire/firms.py](src/forest_fire/firms.py) | The FIRMS area API client, adapted from `rs_open_source.nasa.firms` of remote-sensing-open-source-docker |
| [job/firms/\_\_main\_\_.py](job/firms/__main__.py) | Step 1 |
| [job/firms_grid/\_\_main\_\_.py](job/firms_grid/__main__.py) | Step 2 |
| [job/forest_typology/\_\_main\_\_.py](job/forest_typology/__main__.py) Step 2b, land cover of the AOI |
| [notebook/firms.ipynb](notebook/firms.ipynb) | Step 3 |
| `output/` | Everything the steps make, including the GADM cache, not in git |

Each step skips the files it already made, so you can run a step again after an error and it picks up where it stopped. To make a file again, delete it first.

## What you need ##

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (Windows, macOS) or Docker Engine with Compose (Linux). Docker brings the GDAL 3.11+ command line that step 2 needs, so you do not have to install GDAL yourself.
- [Git](https://git-scm.com/downloads), or download the repository as a zip.
- An email address for the FIRMS MAP_KEY.
- About 3 GB of disk for the Docker image and a stable internet connection. The outputs of the default example are a few hundred MB.

## Step 0: set up the project and credentials ##

### 0.1 Get the code ###

```sh
git clone <url of this repository> forest-fire-mapping-tutorial
cd forest-fire-mapping-tutorial
```

### 0.2 Get a NASA FIRMS MAP_KEY ###

The FIRMS API needs a MAP_KEY for every request. It is free.

1. Open https://firms.modaps.eosdis.nasa.gov/api/map_key/.
2. Click **Get MAP_KEY**, enter your email address and submit.
3. The key is sent to your email. It is a long string of letters and numbers.
4. Check it works by opening this link with your key in place of `YOUR_KEY`:
   `https://firms.modaps.eosdis.nasa.gov/mapserver/mapkey_status/?MAP_KEY=YOUR_KEY`.
   You should see a small JSON text with `transaction_limit` and `current_transactions`.

A MAP_KEY allows **5,000 transactions per 10 minutes**, and a large request (a big area or several days) can count as several transactions. Step 1 handles this for you, see [Step 1](#step-1-get-the-firms-data). If you lost your key or the email says it is already registered, use the links on the same page to resend it.

Treat the key as a password: do not put it in a notebook, a commit or a screenshot. It is part of every FIRMS request link, which is why the job logs the requests without it.

### 0.3 Create `.env` ###

Copy [.env.example](.env.example) to `.env`:

```sh
cp .env.example .env          # macOS, Linux, Git Bash
copy .env.example .env        # Windows Command Prompt
Copy-Item .env.example .env   # Windows PowerShell
```

Open `.env` and paste your key:

```sh
FIRMS_MAP_KEY="your key here"
```

That is the only credential. No Google Cloud, GitHub, NASA Earthdata or AWS account is needed. `.env` is in `.gitignore`, keep it out of git.

The other settings in `.env` are your inputs. The values of `.env.example` run the full example:

| Setting | Example | Meaning |
|---|---|---|
| `COUNTRY` | `IDN` | Country of the provinces, the 3 letter ISO code that GADM uses |
| `PROVINCES` | `Kalimantan Tengah` | GADM province names, comma separated. Empty for the whole country. See [Choose your area and period](#choose-your-area-and-period) |
| `AOI_NAME` | `kalimantan_tengah` | Name of the AOI in the grid and figure file names, lower case without spaces |
| `AOI_TITLE` | `Kalimantan Tengah` | Title of the AOI on the maps |
| `START_DATE`, `END_DATE` | `2026-08-01`, `2026-10-31` | Period of the alerts, both days included. The file names have both dates, `2026-08-01_2026-10-31` |
| `FIRMS_SOURCES` | the 4 near real time sources | Optional, satellites and processing level, see [Step 1](#step-1-get-the-firms-data) |

Keep the example values for your first run: steps 1 and 2 of the example took about a minute and a half in our test. Change them afterwards to map your own area.

### 0.4 Build the Docker image ###

Start Docker Desktop, then in the project folder:

```sh
docker compose build
```

The first build takes a few minutes. The `src`, `job` and `notebook` folders are mounted into the container, so you only build again when [pyproject.toml](pyproject.toml) or the [Dockerfile](Dockerfile) change.

## Step 1: get the FIRMS data ##

### What FIRMS is ###

[FIRMS](https://firms.modaps.eosdis.nasa.gov/) (Fire Information for Resource Management System) publishes active fire detections from the MODIS and VIIRS instruments. Each alert is the centre of a satellite pixel where a fire or other heat source was detected during an overpass: 375 m pixels for VIIRS (on the S-NPP, NOAA-20 and NOAA-21 satellites) and 1 km pixels for MODIS (Terra and Aqua). It is not a burned area, see [limits](#data-licences-and-limits).

The job uses the [FIRMS area API](https://firms.modaps.eosdis.nasa.gov/api/area/):

```
https://firms.modaps.eosdis.nasa.gov/api/area/csv/[MAP_KEY]/[SOURCE]/[west,south,east,north]/[DAY_RANGE]/[DATE]
```

which returns a CSV of the alerts of one source, in one bounding box, for 1 to 5 days from `DATE`.

The sources (`FIRMS_SOURCES`):

| Source | Satellite | Use for |
|---|---|---|
| `VIIRS_SNPP_NRT`, `VIIRS_NOAA20_NRT`, `VIIRS_NOAA21_NRT`, `MODIS_NRT` | Near real time (NRT) | Recent months, the default |
| `VIIRS_SNPP_SP`, `VIIRS_NOAA20_SP`, `VIIRS_NOAA21_SP`, `MODIS_SP` | Standard processing (SP), the archive | Older periods |

NRT only covers a recent window and SP starts where NRT ends, a few months back. If your period is older, add the SP sources, for example:

```sh
FIRMS_SOURCES='["MODIS_SP", "VIIRS_SNPP_SP", "VIIRS_NOAA20_SP", "MODIS_NRT", "VIIRS_SNPP_NRT", "VIIRS_NOAA20_NRT", "VIIRS_NOAA21_NRT"]'
```

When both the NRT and SP of a satellite are chosen, the NRT starts the day after the last SP day, so no alert is counted twice. You can see the dates of every source at `https://firms.modaps.eosdis.nasa.gov/api/data_availability/csv/YOUR_KEY/all`.

For example, on 4 November 2026 `MODIS_NRT` started on 1 September 2026, so with the default sources the example has no MODIS alerts in August. `MODIS_SP` reached 31 August 2026 on that day, so adding `MODIS_SP` to `FIRMS_SOURCES` fills August. These dates move forward over time, check the availability link for yours.

### What the job does ###

For every province of the AOI, [job/firms](job/firms/__main__.py):

1. Reads the GADM provinces of `COUNTRY` and picks those in `PROVINCES`. The first time, it downloads the full resolution GADM level 1 of the country and keeps it in `output/gadm/`. A name that GADM does not know stops the job at once with the list of the province names of the country.
2. Checks the MAP_KEY and reads the data availability of each source. Days outside the availability of a source are not requested, and the log says so (for example `MODIS_NRT has data from ... only ... is downloaded`).
3. Splits the period into windows of 5 days (the API maximum) per source.
4. Downloads the windows over the bounding box of the province, 4 requests at a time.
5. Checks that every answer is a FIRMS CSV. FIRMS can answer with a text message instead, for example when the transaction limit is reached. Such a request is retried after 1, 2, 4, 8 and 10 minutes, so the limit has time to reset. If it still fails, the province fails instead of being saved with missing days.
6. Keeps only the alerts inside the province polygon (the API works with boxes) and saves them to `output/firms/<province>_<period>.fgb`, for example `output/firms/kalimantan_barat_2026-08-01_2026-10-31.fgb`.

### Run it ###

```sh
docker compose run --rm firms
```

The log lists the provinces, every request, then a line per province:

```
2026-11-04 10:38:23 - job.firms - kalimantan_tengah_2026-08-01_2026-10-31: 536,563 alerts saved to output/firms/kalimantan_tengah_2026-08-01_2026-10-31.fgb
```

The example (1 province, 3 months, 4 sources) sends about 70 requests and took about 1 minute in our test (4 November 2026). Every province adds about 70 requests for 3 months, so a large AOI can take from tens of minutes to a few hours, depending on the FIRMS server and how often the limit is hit. Warnings like `No CSV for ... retrying in 60 s` are normal for large areas. If the job ends with `Failed: ...`, run the same command again: finished provinces are skipped.

### Check the result ###

Open a `.fgb` file in [QGIS](https://qgis.org/) (drag and drop). Each point has the FIRMS columns, among them:

| Column | Meaning |
|---|---|
| `acq_date`, `acq_time`, `acq_datetime` | Date and time of the overpass, UTC |
| `satellite`, `instrument` | For example `N20` and `VIIRS` |
| `confidence` | `l`, `n`, `h` (low, nominal, high) for VIIRS, a percentage for MODIS |
| `frp` | Fire radiative power in MW |
| `daynight` | `D` day or `N` night |
| `source` | The FIRMS source it was downloaded from |
| `province` | Province name used in the file names |

See the [FIRMS attribute descriptions](https://www.earthdata.nasa.gov/data/tools/firms/faq) for the full list.

## Step 2: make the FIRMS grid ##

A map of millions of points is hard to read. Step 2 counts the alerts per grid cell, which shows where fires were frequent.

### What the job does ###

For the whole AOI, [job/firms_grid](job/firms_grid/__main__.py) runs the GDAL command line:

1. `gdal vector concat`: merges the province files of step 1 into one layer. It stops with a clear error if a province file is missing.
2. `gdal vector grid count`: counts the alerts within half a cell from each cell centre, on a grid of 0.009° (about 1 km at the equator) in EPSG:4326, snapped to the cell size so the grids of different periods line up.
3. Reads [ESA WorldCover](https://esa-worldcover.org/) 2021 for the AOI from its public bucket, on the same grid, with the most common class per cell. Only the overview close to 1 km is downloaded, not the 10 m pixels.
4. `gdal vector rasterize`: the AOI provinces as 1 on the same grid. A cell is inside when its centre is inside, so coastal cells that are mostly sea are left out.
5. `gdal raster calc`: keeps the count where the cell is inside the AOI and is not WorldCover permanent water (class 80), nodata (65535) elsewhere.
6. `gdal raster convert`: writes a Cloud Optimised GeoTIFF with ZSTD compression, statistics and overviews to `output/firms_grid/<AOI_NAME>_<period>.tif`, for example `output/firms_grid/kalimantan_tengah_2026-08-01_2026-10-31.tif`.

### Run it ###

After step 1 finished for every province of the AOI:

```sh
docker compose run --rm firms_grid
```

It took about 20 seconds for the example in our test. Intermediate files are in `output/firms_grid/<AOI_NAME>_<period>_work/` while it runs and are removed at the end.

### Check the result ###

Open the `.tif` in QGIS. The value of a cell is the number of alerts in the period; 0 is a land cell without alerts. Use a colour ramp such as *YlOrRd* with a log or cumulative count cut stretch, as a few cells have far more alerts than the rest.

## Step 2b: get the Forest Typology 2020 land cover ##

The notebook draws the land cover under the fire alerts and counts the alerts per land cover type. It comes from [Forest Typology 2020 v1](https://plus.figshare.com/articles/dataset/_i_Forest_Typology_2020_v1_i_/32288616) (ForTy, Google DeepMind, WRI, UMD and IIASA) tells them apart: for every 10 m pixel it gives the probability of primary forest, naturally regenerating forest, planted forest, plantation forest, and tree crops and agroforestry. That shows whether fire alerts fall in old forest, in regrowth, in plantations or outside forest.

### What the job does ###

[job/forest_typology](job/forest_typology/__main__.py) reads the public Space4Good copy of the dataset in Cloud Storage (`gs://s4g-green-finance-bucket/data/cog/forest_typology`), without a login:

1. Finds the UTM grid zones over the AOI, and reads the place of each GeoTIFF of these zones from its header. The first time this takes a few seconds per zone; the result is kept in `output/forest_typology/index/`.
2. Reprojects only the tiles over the AOI provinces onto one EPSG:4326 grid, averaging the probabilities. GDAL reads the overview of each tile closest to the resolution, not the 10 m pixels.
3. Adds `OtherLand` (1 minus the 5 probabilities) and `class`, the most probable of the 6, and sets everything outside the AOI provinces to nodata.
4. Writes a Cloud Optimised GeoTIFF to `output/forest_typology/<AOI_NAME>_forest_typology_<resolution>m.tif`, for example `output/forest_typology/kalimantan_tengah_forest_typology_100m.tif`.

The resolution is `FOREST_TYPOLOGY_RESOLUTION` in `.env`, 100 m by default. The pixels nest in the 1 km cells of the FIRMS grid, and 1000 gives exactly the FIRMS grid. A finer resolution reads more data and makes a larger file: the size grows 100 times from 100 m to 10 m.

### Run it ###

```sh
docker compose run --rm forest_typology
```

It does not need the FIRMS steps, run it before or after them, but before the notebook. For the example it took about one minute in our test (140 tiles of 4 zones) and made a file of 30 MB.

### Check the result ###

Open the `.tif` in QGIS. It has 7 bands, named in the file:

| Band | Name | Values |
|---|---|---|
| 1 to 5 | `PrimaryForest`, `NaturallyRegeneratingForest`, `PlantedForest`, `PlantationForest`, `TreeCropsAndAgroforestry` | Probability from 0 to 250 (250 is 1) |
| 6 | `OtherLand` | 250 minus the 5 probabilities |
| 7 | `class` | 1 primary forest, 2 naturally regenerating forest, 3 planted forest, 4 plantation forest, 5 tree crops and agroforestry, 6 other land |

255 is nodata. In the example, the class of 43.5% of the pixels of Kalimantan Tengah is primary forest, 27.4% naturally regenerating forest, 13.1% tree crops and agroforestry, 0.6% plantation forest and 15.4% other land. It shows 2020, not the land cover during the fires.

## Step 3: run the notebook ##

[notebook/firms.ipynb](notebook/firms.ipynb) reads the outputs of steps 1, 2 and 2b and makes five sets of figures of the AOI:

1. Provinces on a shaded relief basemap.
2. FIRMS alerts by instrument, with the Forest Typology 2020 classes of step 2b and cities above 100,000 people ([Natural Earth](https://www.naturalearthdata.com/)).
3. The fire count grid of step 2, with a log colour scale.
4. Fire alerts per regency or city (kabupaten and kota, GADM level 2), in total and per 100 km².
5. Fire alerts per land cover type: each alert gets the Forest Typology class of the pixel under its point, and each class gets its area in hectares (the pixels of each row times their area on a sphere, as pixels in degrees get smaller away from the equator). A table gives the alerts, their share, the area, its share and the alerts per 1,000 ha, and a bar chart the alerts per 1,000 ha.

In the example (Kalimantan Tengah, 1 August to 31 October 2026, 100 m):

| Land cover 2020 | Alerts | Share of alerts | Area (ha) | Share of area | Alerts per 1,000 ha |
|---|---|---|---|---|---|
| Primary forest | 145,686 | 27.2% | 6,723,327 | 43.5% | 21.7 |
| Naturally regenerating forest | 82,868 | 15.4% | 4,230,810 | 27.4% | 19.6 |
| Planted forest | 0 | 0.0% | 0 | 0.0% | |
| Plantation forest | 183 | 0.0% | 87,402 | 0.6% | 2.1 |
| Tree crops and agroforestry | 19,368 | 3.6% | 2,016,196 | 13.1% | 9.6 |
| Other land | 288,440 | 53.8% | 2,382,250 | 15.4% | 121.1 |

18 alerts fell on pixels without a class (water and coast). Other land, 15% of the area, had more than half of the alerts.

The figures of 3, 4 and 5 are saved in `output/figures/`. The GADM regencies are downloaded once to `output/gadm/`; the cities and basemap tiles are read from public sources when the notebook runs, no credential needed.

### Run it in Docker (recommended) ###

```sh
docker compose up notebook
```

Open http://localhost:8888/lab?token=forest-fire in your browser, open `notebook/firms.ipynb` and choose **Run > Run All Cells**. The whole notebook takes a few minutes, most of it reading the forest and the regencies. Stop JupyterLab with `Ctrl+C` in the terminal, then `docker compose down`.

The server only listens on your own computer (`127.0.0.1`). The token `forest-fire` is set in [docker-compose.yml](docker-compose.yml), change it if other people can use your computer.

### Run it in VS Code ###

Create the local Python environment of [Running without Docker](#running-without-docker), open the notebook in VS Code and select the `.venv` kernel. The notebook does not need the GDAL command line.

### Adjust the figures ###

- Text size: `plt.rcParams["font.size"]` in the first cell, every other size follows it.
- Map width: `MAP_WIDTH` in the first cell.
- Cities shown: `MIN_POPULATION` in the first cell.
- Name of the level 2 units in the titles: `ADM2_UNIT` in the first cell, for a country other than Indonesia.
- The AOI and its title come from `.env`. Restart the kernel after changing `.env`.

## Choose your area and period ##

The AOI is an input in `.env`, no code to change. It is always a set of [GADM 4.1](https://gadm.org/) provinces (level 1) of one country.

### Another set of provinces ###

1. Find the province names. The 34 provinces of Indonesia in GADM 4.1 (before the 2022 split of Papua) are: Aceh, Bali, Bangka Belitung, Banten, Bengkulu, Gorontalo, Jakarta Raya, Jambi, Jawa Barat, Jawa Tengah, Jawa Timur, Kalimantan Barat, Kalimantan Selatan, Kalimantan Tengah, Kalimantan Timur, Kalimantan Utara, Kepulauan Riau, Lampung, Maluku, Maluku Utara, Nusa Tenggara Barat, Nusa Tenggara Timur, Papua, Papua Barat, Riau, Sulawesi Barat, Sulawesi Selatan, Sulawesi Tengah, Sulawesi Tenggara, Sulawesi Utara, Sumatera Barat, Sumatera Selatan, Sumatera Utara and Yogyakarta. If you are not sure of a name, run step 1: it stops at once and lists the names of the country.
2. Set them in `.env`, with a new `AOI_NAME` so the grid and figures of each AOI keep their own files:

   ```sh
   COUNTRY="IDN"
   PROVINCES="Jawa Barat, Jawa Tengah, Jawa Timur"
   AOI_NAME="java"
   AOI_TITLE="Java"
   ```

   The names can be written with or without spaces and in any case: `Jawa Tengah`, `JawaTengah` and `jawa tengah` are the same province. Leave `PROVINCES=""` for the whole country.
3. Run the three steps. Provinces already downloaded for the same period are skipped in step 1.

### Another country ###

Set `COUNTRY` to the 3 letter code of the country used in the GADM file names, see https://gadm.org/download_country.html (for example `MYS` for Malaysia or `BRA` for Brazil), and `PROVINCES` to its GADM level 1 names. The first run downloads GADM levels 1 and 2 of that country to `output/gadm/`. In the notebook, change `ADM2_UNIT` in the first cell to the name of its level 2 units (for example `municipality` in Brazil).

### Another period ###

Set `START_DATE` and `END_DATE` in `.env` and run the three steps again. The file names have the period in them, so the outputs of different periods do not mix. For dates older than a few months, add the SP sources, see [Step 1](#step-1-get-the-firms-data).

## Running without Docker ##

Step 1 and the notebook only need Python 3.11+. Step 2 also needs the GDAL 3.11 or newer command line (`gdal --version`), for example from [OSGeo4W](https://trac.osgeo.org/osgeo4w/) on Windows, conda-forge (`conda install -c conda-forge "gdal>=3.11"`) or a recent Linux distribution.

```sh
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -e ".[notebook]"

python -m job.firms             # Step 1
python -m job.firms_grid        # Step 2
jupyter lab                     # Step 3, open notebook/firms.ipynb
```

Run the commands from the project folder: the jobs read `.env` and write to `output/` from there.

## Troubleshooting ##

| Message | Cause and fix |
|---|---|
| `FIRMS_MAP_KEY is not set` | `.env` is missing or has no key. Check it is named `.env` (not `.env.txt`) and is in the project folder |
| `FIRMS_MAP_KEY is not valid` | Typo in the key, or quotes inside the quotes. Test it with the status link of [0.2](#02-get-a-nasa-firms-map_key) |
| `No CSV for ... retrying in ... s` | Usually the transaction limit. The job waits and retries, nothing to do |
| `Failed: <province>_...` at the end of step 1 | A province failed after all retries. Run step 1 again, finished provinces are skipped |
| `... has data from ... none from ...` | The source has no data in your period, often NRT for an old period. Add the SP sources |
| `No GADM province ... Check PROVINCES in .env, the provinces are: ...` | A name in `PROVINCES` is not a GADM province of `COUNTRY`. Copy the name from the list in the message |
| `Missing output/firms/..., run job.firms first` | Step 1 did not finish every province of the AOI, or `PROVINCES` changed between step 1 and step 2 |
| `The gdal command line ... is not installed` | Step 2 outside Docker without GDAL 3.11+. Use `docker compose run --rm firms_grid` |
| `FileNotFoundError` or `DataSourceError` in the notebook | The outputs of steps 1, 2 or 2b are missing for the AOI and period of `.env`, for example `.env` changed after step 2. Restart the kernel after changing `.env` |
| `port is already allocated` for the notebook | Another JupyterLab uses port 8888. Stop it, or change the first `8888` under `ports` in [docker-compose.yml](docker-compose.yml) |
| A step was stopped half way | Run it again. Files are written under a temporary name and renamed when complete, so no partial file is skipped |

## Data, licences and limits ##

| Data | Source | Licence or terms |
|---|---|---|
| Fire alerts | [NASA FIRMS](https://firms.modaps.eosdis.nasa.gov/), VIIRS and MODIS near real time | NASA open data, cite FIRMS, see the [citation guidance in the FIRMS FAQ](https://www.earthdata.nasa.gov/data/tools/firms/faq) |
| Provinces and regencies | [GADM 4.1](https://gadm.org/), levels 1 and 2 | Free for non-commercial use, other use needs permission, see https://gadm.org/license.html |
| Land cover 2020 | [Forest Typology 2020 v1](https://plus.figshare.com/articles/dataset/_i_Forest_Typology_2020_v1_i_/32288616), Google DeepMind, WRI, UMD and IIASA, read from a Space4Good copy | CC BY 4.0 |
| Water | [ESA WorldCover 2021 v200](https://esa-worldcover.org/) | CC BY 4.0, ESA |
| Cities | [Natural Earth](https://www.naturalearthdata.com/) populated places | Public domain |
| Basemap | Esri World Shaded Relief | Keep the Esri attribution on the maps |

Read the maps with these limits in mind:

- **An alert is not a fire and not a burned area.** It is a hot pixel (375 m for VIIRS, 1 km for MODIS) at the time of an overpass. One fire is counted on every overpass that sees it, a long fire counts many times, and a fire under thick smoke or cloud can be missed. Alerts can also come from other heat sources, such as gas flares or industry. Alert counts show where fire activity was detected, not how much area burned or why.
- **The instruments differ.** VIIRS has smaller pixels and detects smaller fires than MODIS, and the number of satellites changes over time. Compare periods with the same sources.
- **The grid counts fewer alerts than the points.** It counts alerts within half a cell from the cell centre, which leaves out alerts in the cell corners (about a fifth in a 10 day test over Kalimantan). Use the points or the admin level 2 counts for totals.
- **The land cover is of 2020, not of the mapped period.** A forest cleared or burned since 2020 is still forest in Forest Typology, so alerts per land cover type show where alerts fell on the 2020 map, not what burned.
- **An alert gets the class under its point.** The point is the centre of a 375 m VIIRS or 1 km MODIS pixel, larger than the 100 m land cover pixel, so the class of the point can differ from the rest of the satellite pixel, most at forest edges. Forest Typology classes are model predictions with their own errors, the most probable class is used.
- **Cells near the coast and lakes** are masked with WorldCover 2021, read at about 1 km with the most common class, so small islands and narrow coasts can be left out.
- **Cities**: Natural Earth has a few places that share a name and population with another one. The notebook leaves those out.

The FIRMS client in [src/forest_fire/firms.py](src/forest_fire/firms.py) is adapted from `rs_open_source.nasa.firms` of remote-sensing-open-source-docker. Code under the [MIT licence](LICENSE).
