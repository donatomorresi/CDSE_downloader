# CDSE Sentinel Downloader (S2 + S1)

Search and download Sentinel products from https://dataspace.copernicus.eu/ using OData query.

## Install with Python

With Git:

```bash
python -m pip install git+https://github.com/vudongpham/CDSE_Sentinel2_downloader.git
```

Without Git:

```bash
python -m pip install https://github.com/vudongpham/CDSE_Sentinel2_downloader/archive/refs/heads/main.zip
```

## Build local Docker image

```bash
docker build -t cdse-s2-s1:local .
```

## Run

There are two modes: `cdse-search` and `cdse-download`.

### 1. Search (no credential required)

Required arguments:
- `aoi`
  - Sentinel-2 (`--mission S2`, default): `.txt` tile list (`TXXXXX` or `XXXXX`) or vector (`.shp`, `.gpkg`, `.geojson`)
  - Sentinel-1 (`--mission S1`): vector only (`.shp`, `.gpkg`, `.geojson`)
- `output_dir` (optional when `--no-action` is used)

Optional arguments:
- `-m | --mission`: `S2` (default) or `S1`
- `-d | --daterange`: `YYYYMMDD,YYYYMMDD`
- `-p | --product-type`: override product type
  - S2 default: `S2MSI1C`
  - S1 default: `IW_GRDH_1S`
- `-c | --cloudcover`: S2 only, valid values `0,100`
- `--orbit-direction`: S1 optional filter (for example `ASCENDING`, `DESCENDING`)
- `--relative-orbit`: S1 optional integer filter
- `--polarisation`: S1 optional filter (for example `VV&VH`)
- `--simplify-aoi` / `--no-simplify-aoi`: S1 only, enable/disable geometry simplification before querying
- `-f | --forcelogs`: path to FORCE logs, matched product names will be excluded
- `-n | --no-action`: dry search without writing JSON

Sentinel-1 search behavior:
- each polygon geometry from the input vector is queried separately
- results from all geometry tiles are merged and deduplicated by product `Id`

Sentinel-2 example:

```bash
cdse-search \
  --mission S2 \
  --daterange 20240101,20240630 \
  --cloudcover 0,75 \
  ./test_data/berlin_boundary.gpkg \
  ./test_data
```

Sentinel-1 example:

```bash
cdse-search \
  --mission S1 \
  --product-type IW_GRDH_1S \
  --daterange 20240101,20240131 \
  ./test_data/berlin_boundary.gpkg \
  ./test_data
```

### 2. Download (credential required)

Prepare a credential file with:

```text
username
password
```

Then run:

```bash
cdse-download \
  ./test_data/query.json \
  ./download_dir \
  ./test_data/secret.txt
```

`secret` can be either:
- `username,password` (single string)
- path to a 2-line credentials file

Downloader behavior:
- tries `/$value` first
- falls back to `/$zip` (useful for some Sentinel-1 products)

Direct COG HTTP mode (parallel assets):

```bash
cdse-download \
  ./test_data/query.json \
  ./download_dir \
  ./test_data/secret.txt \
  --mode cog-http \
  --assets vv,vh \
  --workers 16
```

Notes for direct COG HTTP mode:
- reads each product from your query JSON and resolves STAC asset links
- downloads selected assets in parallel using HTTPS links (`zipper.dataspace.copernicus.eu`)
- useful for Sentinel-1 COG products and large-volume workflows
- use `--assets all` to download all STAC data assets
