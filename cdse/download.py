import argparse
import json
import os
import requests
import glob
import time
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from urllib.parse import urlparse
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

STAC_BASE_URL = "https://stac.dataspace.copernicus.eu/v1"


def read_json_results(jsonFile):
    with open(jsonFile, "r") as file:
        data = json.load(file)
    if len(data) < 1:
        raise TypeError("JSON file is empty or not in the correct format. Process ends!")
    if not isinstance(data, list):
        raise TypeError("JSON file is not in the expected list format. Process ends!")
    return data


def extract_product_ids_names(data):
    try:
        images_id = [data[i]["Id"].split(".")[0] for i in range(len(data))]
        images_name = [data[i]["Name"].split(".")[0] for i in range(len(data))]
        return images_id, images_name
    except Exception:
        raise TypeError("JSON file is empty or not in the correct format. Process ends!")


def filtering_dir(image_id, image_name, downloadDir):
    image_id_new = []
    image_name_new = []
    for i in range(len(image_name)):
        files = glob.glob(os.path.join(downloadDir, f"{image_name[i]}.*"))
        if files:
            continue
        else:
            image_id_new.append(image_id[i])
            image_name_new.append(image_name[i])
    if len(image_id_new) < 1:
        print('All images have been downloaded!')
        sys.exit()
    return image_id_new, image_name_new


def get_secret_from_file(file_dir):
    with open(file_dir, "r") as file:
        data = [line.strip() for line in file]
    if len(data) < 2:
        print(
            f"{file_dir} does not have the right format, please check again!\n"
            "First line:<username>\nSecond line:<password>"
        )
        sys.exit()
    user_name = data[0]
    password = data[1]
    return user_name, password


def get_secret_from_text(string_in):
    user_name, password = string_in.split(",")
    return user_name, password


def get_token(username, password):
    data = {
        "client_id": "cdse-public",
        "username": f"{username}",
        "password": f"{password}",
        "grant_type": "password",
    }

    r = requests.post(
        "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token",
        data=data,
        timeout=60,
    )
    r.raise_for_status()
    token = r.json()["access_token"]
    return token


def make_retry_session(
    total=8,
    backoff_factor=1.0,
    status_forcelist=(429, 500, 502, 503, 504),
):
    retry = Retry(
        total=total,
        connect=total,
        read=total,
        backoff_factor=backoff_factor,
        status_forcelist=status_forcelist,
        allowed_methods=frozenset(["GET", "POST"]),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


class TokenProvider:
    def __init__(self, username, password):
        self.username = username
        self.password = password
        self.lock = Lock()
        self.token = None
        self.token_time = 0.0

    def get(self, force_refresh=False):
        with self.lock:
            now = time.time()
            if self.token is None or force_refresh or (now - self.token_time) > 550:
                self.token = get_token(self.username, self.password)
                self.token_time = now
            return self.token


def download_product_archive(image_id, image_name, download_dir, token_provider):
    session = make_retry_session(total=5, backoff_factor=1.0)
    base_url = f"https://download.dataspace.copernicus.eu/odata/v1/Products({image_id})"
    output_path = os.path.join(download_dir, f"{image_name}.zip")
    tmp_path = f"{output_path}.part"

    for endpoint in ("/$value", "/$zip"):
        url = f"{base_url}{endpoint}"
        for auth_try in range(2):
            token = token_provider.get(force_refresh=(auth_try > 0))
            headers = {"Authorization": f"Bearer {token}"}
            response = session.get(url, headers=headers, stream=True, timeout=180)

            if response.status_code == 401 and auth_try == 0:
                continue

            if response.status_code == 200:
                try:
                    with open(tmp_path, "wb") as f:
                        for chunk in response.iter_content(chunk_size=1024 * 1024):
                            if chunk:
                                f.write(chunk)
                    os.replace(tmp_path, output_path)
                    return
                except Exception:
                    if os.path.isfile(tmp_path):
                        os.remove(tmp_path)
                    raise

            if response.status_code in (400, 404):
                break

            response.raise_for_status()
            break

    raise RuntimeError(f"Neither /$value nor /$zip is available for {image_name}")


def download_products_parallel(images_id, image_names, download_dir, token_provider, workers):
    workers = max(1, workers)
    if workers > 4:
        print("Product mode is capped to 4 workers to respect CDSE concurrent-connection limits.")
        workers = 4

    tasks = list(zip(images_id, image_names))
    n_total = len(tasks)
    print(f"Downloading {n_total} products with {workers} workers...", flush=True)
    done = 0
    failed = 0

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {
            executor.submit(
                download_product_archive,
                image_id,
                image_name,
                download_dir,
                token_provider,
            ): image_name
            for image_id, image_name in tasks
        }

        for i, future in enumerate(as_completed(future_map), start=1):
            image_name = future_map[future]
            try:
                future.result()
                done += 1
                print(f"Done {i} / {n_total} : {image_name}", flush=True)
            except Exception as e:
                failed += 1
                print(f"Failed to download {image_name}: {e}", flush=True)

    print(f"Product download finished. Succeeded: {done}, Failed: {failed}", flush=True)


def infer_stac_collection(product_name):
    name = product_name.upper()
    if name.startswith("S1") and "_GRD" in name:
        return "sentinel-1-grd"
    if name.startswith("S2") and "_MSIL2A_" in name:
        return "sentinel-2-l2a"
    if name.startswith("S2") and "_MSIL1C_" in name:
        return "sentinel-2-l1c"
    return None


def stac_item_id_from_name(product_name):
    item_id = product_name
    for ext in (".SAFE", ".SEN3", ".ZIP"):
        if item_id.upper().endswith(ext):
            return item_id[: -len(ext)]
    return item_id


def fetch_stac_items_by_ids(stac_session, collection_id, item_ids, batch_size=100):
    items_map = {}
    if len(item_ids) < 1:
        return items_map

    search_url = f"{STAC_BASE_URL}/search"
    for i in range(0, len(item_ids), batch_size):
        batch = item_ids[i : i + batch_size]
        payload = {
            "collections": [collection_id],
            "ids": batch,
            "limit": len(batch),
        }
        response = stac_session.post(search_url, json=payload, timeout=120)
        response.raise_for_status()
        data = response.json()
        features = data.get("features", [])
        for feature in features:
            item_id = feature.get("id")
            if item_id:
                items_map[item_id] = feature
    return items_map


def _infer_extension_from_asset(asset, asset_url):
    local_path = asset.get("file:local_path")
    if isinstance(local_path, str):
        ext = os.path.splitext(local_path)[1]
        if ext:
            return ext

    asset_type = (asset.get("type") or "").lower()
    if "zip" in asset_type:
        return ".zip"
    if "tiff" in asset_type or "geotiff" in asset_type:
        return ".tiff"

    ext = os.path.splitext(urlparse(asset_url).path)[1]
    return ext or ".bin"


def _canonical_asset_key_map(assets):
    return {k.lower(): k for k in assets.keys()}


def select_asset_links(item, asset_names):
    assets = item.get("assets", {})
    selected = []
    canonical_map = _canonical_asset_key_map(assets)

    if asset_names == ["all"]:
        keys = []
        for key, value in assets.items():
            if key == "Product":
                continue
            if "data" in value.get("roles", []):
                keys.append(key)
    else:
        keys = []
        for key in asset_names:
            canonical_key = canonical_map.get(key.lower())
            if canonical_key is not None:
                keys.append(canonical_key)

    for key in keys:
        asset = assets.get(key)
        if not asset:
            continue
        alt_https = asset.get("alternate", {}).get("https", {}).get("href")
        href = alt_https or asset.get("href")
        if isinstance(href, str) and href.startswith("http"):
            selected.append((key, href, asset))
    return selected


def build_asset_output_path(download_dir, product_name, asset_name, asset_url, asset):
    local_path = asset.get("file:local_path")
    if isinstance(local_path, str):
        filename = os.path.basename(local_path)
        if filename:
            return os.path.join(download_dir, filename)

    ext = _infer_extension_from_asset(asset, asset_url)
    if asset_name.lower() == "product":
        base = product_name
        if not base.upper().endswith(".ZIP"):
            filename = f"{base}.zip"
        else:
            filename = base
        return os.path.join(download_dir, filename)

    base = product_name.split(".")[0]
    filename = f"{base}_{asset_name}{ext}"
    return os.path.join(download_dir, filename)


def download_asset_http(url, output_path, token_provider):
    tmp_path = f"{output_path}.part"
    session = make_retry_session(total=5, backoff_factor=1.0)
    for i_try in range(2):
        token = token_provider.get(force_refresh=(i_try > 0))
        headers = {"Authorization": f"Bearer {token}"}
        response = session.get(url, headers=headers, stream=True, timeout=180)

        if response.status_code == 401 and i_try == 0:
            continue

        response.raise_for_status()
        with open(tmp_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)
        os.replace(tmp_path, output_path)
        return

    raise RuntimeError(f"Failed to download asset: {url}")


def download_cog_http(data, download_dir, token_provider, asset_names, workers):
    stac_session = make_retry_session(total=8, backoff_factor=1.0)
    tasks = []
    skipped_products = 0
    skipped_missing_items = 0
    products_by_collection = {}

    for product in data:
        product_name = product.get("Name", "")
        if not product_name:
            continue

        collection_id = infer_stac_collection(product_name)
        if collection_id is None:
            skipped_products += 1
            continue

        item_id = stac_item_id_from_name(product_name)
        products_by_collection.setdefault(collection_id, []).append((product_name, item_id))

    for collection_id, entries in products_by_collection.items():
        unique_item_ids = sorted(set(item_id for _, item_id in entries))
        items_map = fetch_stac_items_by_ids(
            stac_session=stac_session,
            collection_id=collection_id,
            item_ids=unique_item_ids,
            batch_size=100,
        )

        for product_name, item_id in entries:
            item = items_map.get(item_id)
            if item is None:
                skipped_missing_items += 1
                print(f"STAC item not found for product: {product_name}")
                continue

            links = select_asset_links(item, asset_names)
            if len(links) < 1:
                print(f"No matching assets for {product_name}")
                continue

            for asset_name, asset_url, asset in links:
                output_path = build_asset_output_path(
                    download_dir=download_dir,
                    product_name=product_name,
                    asset_name=asset_name,
                    asset_url=asset_url,
                    asset=asset,
                )
                if os.path.isfile(output_path):
                    continue
                tasks.append((product_name, asset_name, asset_url, output_path))

    if skipped_products > 0:
        print(f"Skipped {skipped_products} products with unsupported collection mapping.")
    if skipped_missing_items > 0:
        print(f"Skipped {skipped_missing_items} products missing from STAC.")

    if len(tasks) < 1:
        print("No COG HTTP assets to download (all done or no matching assets found).")
        return

    print(f"Downloading {len(tasks)} COG assets with {workers} workers...", flush=True)
    done = 0
    failed = 0

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {
            executor.submit(download_asset_http, url, out_path, token_provider): (
                product_name,
                asset_name,
                out_path,
            )
            for (product_name, asset_name, url, out_path) in tasks
        }

        for future in as_completed(future_map):
            product_name, asset_name, out_path = future_map[future]
            try:
                future.result()
                done += 1
                print(f"Done {done}/{len(tasks)}: {product_name} [{asset_name}]")
            except Exception as e:
                failed += 1
                print(f"Failed: {product_name} [{asset_name}] -> {e}")

    print(f"COG HTTP download finished. Succeeded: {done}, Failed: {failed}")


def main():
    parser = argparse.ArgumentParser(
        prog="download",
        description="This tool downloads Sentinel products from CDSE using product IDs from a query JSON file.",
        add_help=True,
    )
    parser.add_argument(
        "jsonFile",
        help="Path to the JSON file generated from the search tool",
    )
    parser.add_argument(
        "downloadDir",
        help="The directory where the downloaded products will be stored.",
    )
    parser.add_argument(
        "secret",
        help="""
        String <username>,<password> separated by ",".
        Or path to the file containing the <username> (first line) and <password> (second line) from https://dataspace.copernicus.eu/
        """,
    )
    parser.add_argument(
        "--mode",
        choices=["product", "cog-http"],
        default="product",
        help="Download mode: product (.zip/.nc) or direct COG HTTP assets.",
    )
    parser.add_argument(
        "--assets",
        default="vv,vh",
        help="For --mode cog-http: comma-separated STAC asset names (for example vv,vh or B02,B03,B04). Use 'all' for all data assets.",
    )
    parser.add_argument(
        "--safe-product",
        action="store_true",
        help="For --mode cog-http: download full SAFE product archives (equivalent to --assets Product).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="Number of parallel download workers (product mode is capped at 4).",
    )
    args = parser.parse_args()

    jsonFile = args.jsonFile
    downloadDir = args.downloadDir
    secret = args.secret

    if not os.path.isfile(jsonFile):
        print(f"{jsonFile} does not exist!")
        sys.exit()

    if not os.path.isdir(downloadDir):
        print(f"{downloadDir} does not exist!")
        sys.exit()

    if os.path.isfile(secret):
        get_secret = get_secret_from_file
    else:
        get_secret = get_secret_from_text

    data = read_json_results(jsonFile)
    username, password = get_secret(secret)
    token_provider = TokenProvider(username=username, password=password)

    if args.mode == "cog-http":
        if args.safe_product:
            asset_names = ["Product"]
        else:
            asset_names = [x.strip() for x in args.assets.split(",") if x.strip()]
        if len(asset_names) < 1:
            print("No valid asset names provided.")
            sys.exit()
        download_cog_http(
            data=data,
            download_dir=downloadDir,
            token_provider=token_provider,
            asset_names=asset_names,
            workers=max(1, args.workers),
        )
        return

    images_id, image_names = extract_product_ids_names(data)
    images_id, image_names = filtering_dir(images_id, image_names, downloadDir)
    try:
        token_provider.get(force_refresh=True)
    except Exception:
        print("Getting token failed! Check username and password!")
        sys.exit()

    download_products_parallel(
        images_id=images_id,
        image_names=image_names,
        download_dir=downloadDir,
        token_provider=token_provider,
        workers=args.workers,
    )


if __name__ == "__main__":
    main()
