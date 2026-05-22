import os
import tempfile
import unittest
from pathlib import Path

from cdse.search import (
    build_filter_expression,
    deduplicate_products,
    read_list_id,
    search_force_logs,
)


ROOT = Path(__file__).parents[1]
DIR_TESTDATA = ROOT / "test_data"
DIR_FORCELOGS = DIR_TESTDATA / "force_logs"


class SearchHelpersTestCase(unittest.TestCase):
    def test_build_filter_expression_joins_with_and(self):
        filters = ["Collection/Name eq 'SENTINEL-1'", "Online eq true"]
        self.assertEqual(
            build_filter_expression(filters),
            "Collection/Name eq 'SENTINEL-1' and Online eq true",
        )

    def test_read_list_id_accepts_prefixed_and_plain_s2_tiles(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as tmp:
            tmp.write("T32UNC\n")
            tmp.write("32UPD\n")
            tmp.write("bad\n")
            tmp_path = tmp.name

        try:
            self.assertEqual(read_list_id(tmp_path), ["32UNC", "32UPD"])
        finally:
            os.remove(tmp_path)

    def test_deduplicate_products_prefers_id_then_name(self):
        products = [
            {"Id": "1", "Name": "A"},
            {"Id": "1", "Name": "A duplicate"},
            {"Name": "B"},
            {"Name": "B"},
        ]

        self.assertEqual(
            deduplicate_products(products),
            [{"Id": "1", "Name": "A"}, {"Name": "B"}],
        )

    def test_search_force_logs(self):
        examples = [
            (8, {}),
            (5, {"recursive": False}),
            (5, {"rx": r"^S2[ABCD]_MSIL1C.*\.log$"}),
            (3, {"rx": r"^S2[ABCD]_MSIL1C.*\.log$", "recursive": False}),
            (3, {"rx": r"^L(T04|T05|E07|C08|C09).*\.log$"}),
            (2, {"rx": r"^L(T04|T05|E07|C08|C09).*\.log$", "recursive": False}),
        ]
        for n_expected, kwargs in examples:
            with self.subTest(kwargs=kwargs):
                logs = list(search_force_logs(DIR_FORCELOGS, **kwargs))
                self.assertEqual(n_expected, len(logs))


if __name__ == "__main__":
    unittest.main()
