from pathlib import Path
from typing import Union, Pattern, Generator, List, Optional
from importlib.resources import files
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import json
import argparse
import re
from datetime import datetime
import os
import sys

CATALOGUE_URL = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products"


class argparseCondition:
    def dateRangeInput(self, value):
        def is_valid_date(date_str):
            try:
                datetime.strptime(date_str, "%Y%m%d")
                return True
            except ValueError:
                return False

        def format_date(date_str):
            return f"{date_str[0:4]}-{date_str[4:6]}-{date_str[6:8]}"

        pattern = r"^\d{8},\d{8}$"
        if not re.match(pattern, value):
            raise argparse.ArgumentTypeError(
                f"Invalid date range format: {value}. Expected format is YYYYMMDD,YYYYMMDD."
            )

        start_date, end_date = value.split(",")
        if not is_valid_date(start_date):
            raise argparse.ArgumentTypeError(f"Invalid date {start_date}")
        if not is_valid_date(end_date):
            raise argparse.ArgumentTypeError(f"Invalid date {end_date}")

        return format_date(start_date), format_date(end_date)

    def cloudCoverInput(self, value):
        try:
            x_str, y_str = value.split(",")
            x = int(x_str)
            y = int(y_str)
            if 0 <= x <= 100 and 0 <= y <= 100:
                return x, y
            raise ValueError
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"Invalid input: {value}. Expected format is 'x,y' with x and y between 0 and 100."
            )

    def relativeOrbitInput(self, value):
        try:
            orbit = int(value)
            if orbit < 1:
                raise ValueError
            return orbit
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"Invalid relative orbit value: {value}. Expected a positive integer."
            )


def _simplify_geometry_for_query(geometry, max_wkt_len: int = 12000):
    for tolerance in (0.01, 0.05, 0.1, 0.2, 0.5):
        candidate = geometry.simplify(tolerance=tolerance, preserve_topology=True)
        if len(candidate.wkt) <= max_wkt_len:
            return candidate
    return geometry.simplify(tolerance=0.5, preserve_topology=True)


def convert_aoi_to_wkt_tiles(
    aoi_path: str, max_wkt_len: int = 12000, simplify_geometries: bool = True
) -> List[str]:
    import geopandas as gpd
    from shapely.ops import transform

    def drop_z(geometry):
        if geometry is None:
            return None
        if getattr(geometry, "has_z", False):
            return transform(lambda x, y, z=None: (x, y), geometry)
        return geometry

    aoi = gpd.read_file(aoi_path)
    aoi = aoi[aoi.geometry.notnull()]
    if len(aoi) < 1:
        raise ValueError(f"{aoi_path} does not contain valid polygon geometries.")

    if aoi.crs is None:
        raise ValueError(f"{aoi_path} has undefined CRS and cannot be reprojected to EPSG:4326.")

    aoi = aoi.to_crs("EPSG:4326")
    aoi["geometry"] = aoi["geometry"].apply(drop_z)

    # Keep per-tile geometry separate for S1 requests, this avoids huge single AOI queries.
    try:
        aoi = aoi.explode(index_parts=False, ignore_index=True)
    except TypeError:
        aoi = aoi.explode(index_parts=False).reset_index(drop=True)

    wkts = []
    for geometry in aoi.geometry.values:
        if geometry is None or geometry.is_empty:
            continue
        if not geometry.is_valid:
            geometry = geometry.buffer(0)
        if geometry.is_empty:
            continue
        if simplify_geometries:
            geometry = _simplify_geometry_for_query(geometry, max_wkt_len=max_wkt_len)
        wkts.append(geometry.wkt)

    if len(wkts) < 1:
        raise ValueError(f"{aoi_path} does not contain usable polygon geometries.")

    return wkts


def convert_aoi_to_s2idlist(aoi_path: str) -> List[str]:
    import geopandas as gpd

    aoi = gpd.read_file(aoi_path)
    s2_path = files("cdse.aux_data").joinpath("sentinel2_grid.gpkg")
    s2_grid = gpd.read_file(s2_path)

    if aoi.crs != s2_grid.crs:
        aoi = aoi.to_crs(s2_grid.crs)

    intersection = gpd.overlay(aoi, s2_grid, how="intersection")
    s2_id_list = intersection["PRFID"].unique()
    s2_id_list = [x.strip() for x in s2_id_list]
    s2_id_list = [x[-5:] for x in s2_id_list]
    return s2_id_list


def read_list_id(aoi_path: str) -> List[str]:
    lines = []
    with open(aoi_path, "r") as file:
        for line in file:
            line = line.strip()
            if len(line) < 5:
                continue
            if len(line) == 5:
                lines.append(line)
            else:
                lines.append(line[-5:])
    return lines


def make_retry_session() -> requests.Session:
    retry = Retry(
        total=5,
        connect=5,
        read=5,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def fetch_all_data(filter_expression: str) -> List[dict]:
    all_data = []
    session = make_retry_session()
    params = {"$filter": filter_expression, "$top": 1000}
    next_url = CATALOGUE_URL

    while next_url:
        if next_url == CATALOGUE_URL:
            response = session.get(next_url, params=params, timeout=120)
        else:
            response = session.get(next_url, timeout=120)
        response.raise_for_status()
        json_return = response.json()
        all_data.extend(json_return.get("value", []))
        next_url = json_return.get("@odata.nextLink")
    return all_data


def make_string_attribute_filter(name: str, value: str) -> str:
    return (
        "Attributes/OData.CSC.StringAttribute/any("
        f"att:att/Name eq '{name}' and att/OData.CSC.StringAttribute/Value eq '{value}')"
    )


def make_integer_attribute_filter(name: str, value: int) -> str:
    return (
        "Attributes/OData.CSC.IntegerAttribute/any("
        f"att:att/Name eq '{name}' and att/OData.CSC.IntegerAttribute/Value eq {value})"
    )


def make_double_attribute_filter(name: str, value: float, op: str) -> str:
    return (
        "Attributes/OData.CSC.DoubleAttribute/any("
        f"att:att/Name eq '{name}' and att/OData.CSC.DoubleAttribute/Value {op} {value:.2f})"
    )


def build_filter_expression(filter_parts: List[str]) -> str:
    return " and ".join(filter_parts)


def search_s2_by_list(
    start_date: str,
    end_date: str,
    cloud_min: int,
    cloud_max: int,
    list_ids: List[str],
    product_type: str,
) -> List[dict]:
    data_return = []

    for tile_id in list_ids:
        filters = [
            "Collection/Name eq 'SENTINEL-2'",
            make_string_attribute_filter("productType", product_type),
            make_string_attribute_filter("tileId", tile_id),
            f"ContentDate/Start ge {start_date}T00:00:00.000Z",
            f"ContentDate/Start le {end_date}T23:59:59.999Z",
            make_double_attribute_filter("cloudCover", cloud_min, "ge"),
            make_double_attribute_filter("cloudCover", cloud_max, "le"),
        ]
        filter_expression = build_filter_expression(filters)
        data_temp = fetch_all_data(filter_expression)
        if len(data_temp) > 0:
            data_return.extend(data_temp)

    return data_return


def search_s1_by_aoi(
    start_date: str,
    end_date: str,
    aoi_wkt: str,
    product_type: str,
    orbit_direction: Optional[str] = None,
    relative_orbit: Optional[int] = None,
    polarisation: Optional[str] = None,
) -> List[dict]:
    filters = [
        "Collection/Name eq 'SENTINEL-1'",
        make_string_attribute_filter("productType", product_type),
        f"ContentDate/Start ge {start_date}T00:00:00.000Z",
        f"ContentDate/Start le {end_date}T23:59:59.999Z",
        f"OData.CSC.Intersects(area=geography'SRID=4326;{aoi_wkt}')",
    ]

    if orbit_direction:
        filters.append(make_string_attribute_filter("orbitDirection", orbit_direction))
    if relative_orbit:
        filters.append(make_integer_attribute_filter("relativeOrbitNumber", relative_orbit))
    if polarisation:
        filters.append(make_string_attribute_filter("polarisationChannels", polarisation))

    filter_expression = build_filter_expression(filters)
    return fetch_all_data(filter_expression)


def deduplicate_products(products: List[dict]) -> List[dict]:
    seen = set()
    unique = []
    for product in products:
        key = product.get("Id") or product.get("Name")
        if key in seen:
            continue
        seen.add(key)
        unique.append(product)
    return unique


def search_s1_by_aoi_tiles(
    start_date: str,
    end_date: str,
    aoi_wkts: List[str],
    product_type: str,
    orbit_direction: Optional[str] = None,
    relative_orbit: Optional[int] = None,
    polarisation: Optional[str] = None,
) -> List[dict]:
    all_results = []
    n_tiles = len(aoi_wkts)

    for i, aoi_wkt in enumerate(aoi_wkts, start=1):
        records = search_s1_by_aoi(
            start_date=start_date,
            end_date=end_date,
            aoi_wkt=aoi_wkt,
            product_type=product_type,
            orbit_direction=orbit_direction,
            relative_orbit=relative_orbit,
            polarisation=polarisation,
        )
        if len(records) > 0:
            all_results.extend(records)

        if n_tiles <= 20 or i == 1 or i == n_tiles or i % 10 == 0:
            print(f"S1 AOI tile {i}/{n_tiles}: {len(records)} records", flush=True)

    unique_results = deduplicate_products(all_results)
    if len(unique_results) != len(all_results):
        print(
            f"Deduplicated S1 results: {len(all_results)} -> {len(unique_results)}",
            flush=True,
        )
    return unique_results


def search_force_logs(
    dir_logs: Union[str, Path], rx: Union[Pattern, str] = None, recursive: bool = True
) -> Generator[Path, None, None]:
    if rx is None:
        rx = re.compile(r"(S1|S2|LT|LE|LC).*\.log")
    elif isinstance(rx, str):
        rx = re.compile(rx)

    assert isinstance(rx, Pattern)

    dir_logs = Path(dir_logs)
    assert dir_logs.is_dir(), f"Not a directory: {dir_logs}"
    with os.scandir(dir_logs) as search:
        for entry in search:
            if entry.is_dir() and recursive:
                for result in search_force_logs(entry.path, rx, recursive=recursive):
                    yield result
            elif entry.is_file() and rx.match(entry.name):
                yield Path(entry.path)


def main():
    check = argparseCondition()

    parser = argparse.ArgumentParser(
        prog="search",
        description="Search Sentinel products from the Copernicus Data Space Ecosystem (CDSE).",
        add_help=True,
    )

    parser.add_argument(
        "-m",
        "--mission",
        default="S2",
        choices=["S1", "S2"],
        help="Mission to query. S2 keeps the original tile-based workflow, S1 uses AOI polygon query.",
    )
    parser.add_argument(
        "-d",
        "--daterange",
        help="Start date and end date to be considered. Valid values: YYYYMMDD,YYYYMMDD",
        default=f"20150101,{datetime.now().strftime('%Y%m%d')}",
        metavar="",
        type=check.dateRangeInput,
    )
    parser.add_argument(
        "-c",
        "--cloudcover",
        help="Cloud cover range for Sentinel-2. Valid values: 0,100",
        default="0,100",
        metavar="",
        type=check.cloudCoverInput,
    )
    parser.add_argument(
        "-p",
        "--product-type",
        default=None,
        help="Override product type. Defaults: S2MSI1C for S2 and IW_GRDH_1S for S1.",
    )
    parser.add_argument(
        "--orbit-direction",
        default=None,
        help="Sentinel-1 optional filter. Example values: ASCENDING or DESCENDING.",
    )
    parser.add_argument(
        "--relative-orbit",
        default=None,
        type=check.relativeOrbitInput,
        help="Sentinel-1 optional filter by relative orbit number.",
    )
    parser.add_argument(
        "--polarisation",
        default=None,
        help="Sentinel-1 optional filter by polarisation channels (for example VV&VH or HH&HV).",
    )
    parser.set_defaults(simplify_aoi=True)
    parser.add_argument(
        "--simplify-aoi",
        dest="simplify_aoi",
        action="store_true",
        help="Sentinel-1 only: simplify AOI geometries before querying (default).",
    )
    parser.add_argument(
        "--no-simplify-aoi",
        dest="simplify_aoi",
        action="store_false",
        help="Sentinel-1 only: keep AOI geometries as-is (can create very large queries).",
    )
    parser.add_argument(
        "-n",
        "--no-action",
        help="Dry search without saving JSON file. output_dir is not required.",
        const=True,
        default=False,
        metavar="",
        action="store_const",
    )
    parser.add_argument(
        "-f",
        "--forcelogs",
        help=(
            "Path to FORCE log file directory (searched recursively). "
            "Results will only include products not present in log names."
        ),
        default=None,
    )
    parser.add_argument(
        "aoi",
        help=(
            "AOI input path. For S2: .txt tile list or vector (.shp/.gpkg/.geojson). "
            "For S1: vector only (.shp/.gpkg/.geojson), each geometry is queried separately and merged."
        ),
    )
    parser.add_argument(
        "output_dir",
        nargs="?",
        help="Output directory or JSON file path for search metadata.",
    )

    args = parser.parse_args()
    if not args.no_action and not args.output_dir:
        parser.error("the following argument is required: output_dir")

    mission = args.mission
    start_date, end_date = args.daterange
    cloud_min, cloud_max = args.cloudcover
    aoi = os.path.normpath(args.aoi)

    if not os.path.isfile(aoi):
        print(f"{aoi} does not exist!")
        sys.exit()

    product_type = args.product_type
    search_results = []
    info = [f"Search mission: {mission}", f" - From {start_date} to {end_date}", f" - AOI: {aoi}"]

    if mission == "S2":
        if product_type is None:
            product_type = "S2MSI1C"
        info.extend(
            [
                f" - Product type: {product_type}",
                f" - {cloud_min} =< Cloud cover <= {cloud_max}",
            ]
        )

        if aoi.endswith((".gpkg", ".shp", ".geojson")):
            s2_list = convert_aoi_to_s2idlist(aoi)
            if len(s2_list) < 1:
                print("No Sentinel-2 tiles intersect the AOI.")
                sys.exit()
        elif aoi.endswith(".txt"):
            s2_list = read_list_id(aoi)
            if len(s2_list) < 1:
                print("No valid Sentinel-2 tile IDs found in the input file.")
                sys.exit()
        else:
            print(f"{aoi} has an invalid extension for S2 search.")
            sys.exit()

        search_results = search_s2_by_list(
            start_date=start_date,
            end_date=end_date,
            cloud_min=cloud_min,
            cloud_max=cloud_max,
            list_ids=s2_list,
            product_type=product_type,
        )
    else:
        if product_type is None:
            product_type = "IW_GRDH_1S"
        info.append(f" - Product type: {product_type}")

        if args.orbit_direction:
            orbit_direction = args.orbit_direction.upper()
            info.append(f" - Orbit direction: {orbit_direction}")
        else:
            orbit_direction = None

        if args.relative_orbit:
            info.append(f" - Relative orbit: {args.relative_orbit}")
        if args.polarisation:
            info.append(f" - Polarisation: {args.polarisation}")

        if aoi.endswith((".gpkg", ".shp", ".geojson")):
            aoi_wkts = convert_aoi_to_wkt_tiles(
                aoi, simplify_geometries=args.simplify_aoi
            )
            info.append(f" - AOI tiles: {len(aoi_wkts)}")
            info.append(
                f" - AOI simplify: {'enabled' if args.simplify_aoi else 'disabled'}"
            )
        else:
            print(f"{aoi} has an invalid extension for S1 search. Use .gpkg, .shp, or .geojson.")
            sys.exit()

        if args.cloudcover != (0, 100):
            print("Note: --cloudcover is ignored for Sentinel-1 searches.", flush=True)

        search_results = search_s1_by_aoi_tiles(
            start_date=start_date,
            end_date=end_date,
            aoi_wkts=aoi_wkts,
            product_type=product_type,
            orbit_direction=orbit_direction,
            relative_orbit=args.relative_orbit,
            polarisation=args.polarisation,
        )

    if args.no_action:
        out_json = None
    else:
        output_dir = os.path.normpath(args.output_dir)
        if output_dir.endswith(".json"):
            out_json = output_dir
        else:
            if not os.path.isdir(output_dir):
                print(f"{output_dir} does not exist!")
                sys.exit()
            json_file_name = f"query_{datetime.now().strftime('%Y%m%dT%H%M%S')}.json"
            out_json = os.path.join(output_dir, json_file_name)

    if args.forcelogs:
        info.append(f" - FORCE logs: {args.forcelogs}")

    print("\n".join(info), flush=True)

    if args.forcelogs:
        if mission == "S2":
            rx = re.compile(r"S2[ABCD]_MSIL1C.*\.log")
        else:
            rx = re.compile(r"S1[ABCD]_.*\.log")
        mission_logs = list(search_force_logs(args.forcelogs, rx, recursive=True))
        mission_logs = [os.path.splitext(f.name)[0] for f in mission_logs]
        search_results_new = [r for r in search_results if r.get("Name") not in mission_logs]
        print(f"Already processed by FORCE: {len(search_results) - len(search_results_new)}")
        search_results = search_results_new
        print(f"Total records left: {len(search_results)}")

    if len(search_results) > 0:
        print(f"Total records: {len(search_results)}")
        images_size = [r.get("ContentLength", 0) for r in search_results]
        images_size_gb = sum(images_size) / (1024 ** 3)
        print(f"Total data volume: {images_size_gb:.2f} GB")
    else:
        print("No record found")

    if out_json is not None:
        with open(out_json, "w") as jsonfile:
            json.dump(search_results, jsonfile, indent=4)
        print(f"Saved to {out_json}")


if __name__ == "__main__":
    main()
