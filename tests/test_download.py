import os
import tempfile
import unittest

from cdse.download import (
    build_asset_output_path,
    extract_product_ids_names,
    infer_stac_collection,
    select_asset_links,
    stac_item_id_from_name,
)


class DownloadHelpersTestCase(unittest.TestCase):
    def test_extract_product_ids_names(self):
        data = [
            {"Id": "abc.def", "Name": "S1A_TEST.SAFE"},
            {"Id": "xyz", "Name": "S2A_TEST.SAFE"},
        ]

        self.assertEqual(
            extract_product_ids_names(data),
            (["abc", "xyz"], ["S1A_TEST", "S2A_TEST"]),
        )

    def test_infer_stac_collection(self):
        self.assertEqual(
            infer_stac_collection("S1A_IW_GRDH_1SDV_20250101_COG.SAFE"),
            "sentinel-1-grd",
        )
        self.assertEqual(
            infer_stac_collection("S2A_MSIL2A_20250101T000000.SAFE"),
            "sentinel-2-l2a",
        )
        self.assertEqual(
            infer_stac_collection("S2A_MSIL1C_20250101T000000.SAFE"),
            "sentinel-2-l1c",
        )
        self.assertIsNone(infer_stac_collection("UNKNOWN_PRODUCT"))

    def test_stac_item_id_from_name_strips_known_archive_suffixes(self):
        self.assertEqual(
            stac_item_id_from_name("S1A_PRODUCT_COG.SAFE"),
            "S1A_PRODUCT_COG",
        )
        self.assertEqual(stac_item_id_from_name("S3_PRODUCT.SEN3"), "S3_PRODUCT")
        self.assertEqual(stac_item_id_from_name("PRODUCT.ZIP"), "PRODUCT")

    def test_select_asset_links_is_case_insensitive_and_prefers_https_alternate(self):
        item = {
            "assets": {
                "vv": {
                    "href": "s3://bucket/vv.tiff",
                    "alternate": {"https": {"href": "https://example.test/vv"}},
                },
                "Product": {
                    "href": "https://example.test/product.zip",
                    "roles": ["data", "archive"],
                },
            }
        }

        self.assertEqual(
            select_asset_links(item, ["VV", "product"]),
            [
                ("vv", "https://example.test/vv", item["assets"]["vv"]),
                ("Product", "https://example.test/product.zip", item["assets"]["Product"]),
            ],
        )

    def test_select_asset_links_all_omits_product_archive(self):
        item = {
            "assets": {
                "vv": {"href": "https://example.test/vv.tiff", "roles": ["data"]},
                "Product": {"href": "https://example.test/product.zip", "roles": ["data"]},
            }
        }

        self.assertEqual(
            select_asset_links(item, ["all"]),
            [("vv", "https://example.test/vv.tiff", item["assets"]["vv"])],
        )

    def test_build_asset_output_path_uses_stac_local_path(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            out_path = build_asset_output_path(
                download_dir=tmp_dir,
                product_name="S1A_PRODUCT_COG.SAFE",
                asset_name="vv",
                asset_url="https://example.test/$value",
                asset={"file:local_path": "S1A_PRODUCT_COG.SAFE/measurement/vv.tiff"},
            )

            self.assertEqual(out_path, os.path.join(tmp_dir, "vv.tiff"))

    def test_build_asset_output_path_names_product_archives_as_zip(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            out_path = build_asset_output_path(
                download_dir=tmp_dir,
                product_name="S1A_PRODUCT_COG.SAFE",
                asset_name="Product",
                asset_url="https://example.test/$value",
                asset={},
            )

            self.assertEqual(out_path, os.path.join(tmp_dir, "S1A_PRODUCT_COG.SAFE.zip"))


if __name__ == "__main__":
    unittest.main()
