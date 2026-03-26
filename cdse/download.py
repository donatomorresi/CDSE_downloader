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


def download(session, image_id, image_name, downloadDir):
    base_url = f"https://download.dataspace.copernicus.eu/odata/v1/Products({image_id})"
    endpoints = ["/$value", "/$zip"]
    for endpoint in endpoints:
        response = session.get(f"{base_url}{endpoint}", stream=True, timeout=120)
        if response.status_code == 200:
            with open(f"{downloadDir}/{image_name}.zip", "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            return

        # Retry with the next endpoint if not found or unsupported
        if response.status_code not in (400, 404):
            print(f"Failed to download {image_name}: HTTP {response.status_code}")
            print(response.text)
            return

    print(f"Failed to download {image_name}: neither /$value nor /$zip is available.")
    return


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


def fetch_stac_item(stac_session, collection_id, item_id):
    url = f"{STAC_BASE_URL}/collections/{collection_id}/items/{item_id}"
    response = stac_session.get(url, timeout=60)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()


def select_asset_links(item, asset_names):
    assets = item.get("assets", {})
    selected = []

    if asset_names == ["all"]:
        keys = []
        for key, value in assets.items():
            if key == "Product":
                continue
            if "data" in value.get("roles", []):
                keys.append(key)
    else:
        keys = asset_names

    for key in keys:
        asset = assets.get(key)
        if not asset:
            continue
        alt_https = asset.get("alternate", {}).get("https", {}).get("href")
        href = alt_https or asset.get("href")
        if isinstance(href, str) and href.startswith("http"):
            selected.append((key, href))
    return selected


def build_asset_output_path(download_dir, product_name, asset_name, asset_url):
    ext = os.path.splitext(urlparse(asset_url).path)[1] or ".bin"
    base = product_name.split(".")[0]
    filename = f"{base}_{asset_name}{ext}"
    return os.path.join(download_dir, filename)


def download_asset_http(url, output_path, token_provider):
    tmp_path = f"{output_path}.part"
    for i_try in range(2):
        token = token_provider.get(force_refresh=(i_try > 0))
        headers = {"Authorization": f"Bearer {token}"}
        response = requests.get(url, headers=headers, stream=True, timeout=180)

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
    stac_session = requests.Session()
    tasks = []
    skipped_products = 0

    for product in data:
        product_name = product.get("Name", "")
        if not product_name:
            continue

        collection_id = infer_stac_collection(product_name)
        if collection_id is None:
            skipped_products += 1
            continue

        item_id = stac_item_id_from_name(product_name)
        item = fetch_stac_item(stac_session, collection_id, item_id)
        if item is None:
            print(f"STAC item not found for product: {product_name}")
            continue

        links = select_asset_links(item, asset_names)
        if len(links) < 1:
            print(f"No matching assets for {product_name}")
            continue

        for asset_name, asset_url in links:
            output_path = build_asset_output_path(
                download_dir=download_dir,
                product_name=product_name,
                asset_name=asset_name,
                asset_url=asset_url,
            )
            if os.path.isfile(output_path):
                continue
            tasks.append((product_name, asset_name, asset_url, output_path))

    if skipped_products > 0:
        print(f"Skipped {skipped_products} products with unsupported collection mapping.")

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
        "--workers",
        type=int,
        default=8,
        help="For --mode cog-http: number of parallel download workers.",
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

    start_time = time.time()

    try:
        token = token_provider.get(force_refresh=True)
    except Exception:
        print("Getting token failed! Check username and password!")
        sys.exit()

    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {token}"})

    print(f"Downloading {len(image_names)} products...", flush=True)
    for i in range(len(images_id)):
        current_time = time.time()
        durations = current_time - start_time

        if durations > 550:
            token = token_provider.get(force_refresh=True)
            session.headers.update({"Authorization": f"Bearer {token}"})
            start_time = time.time()
        try:
            download(session, images_id[i], image_names[i], downloadDir)
            print(f"Done {i + 1} / {len(image_names)} : {image_names[i]}", flush=True)
        except Exception as e:
            print(f"Failed to download: {image_names[i]}")
        except KeyboardInterrupt:
            print("Process interrupted by user. Exiting...")
            break


if __name__ == "__main__":
    main()
