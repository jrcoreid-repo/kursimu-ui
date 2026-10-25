"""Regression tests for KursiMu HX711 serial output (no GUI dependencies)."""
import ast
from pathlib import Path
import re
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "kursimu_gui_v2.py"


def load_parser():
    """Exercise the actual application's regex/function without importing DPG."""
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    pattern = next(
        node.value.args[0].value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "FOOT_PATTERN"
                for target in node.targets)
    )
    fn = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "parse_foot"
    )
    module = ast.Module(body=[fn], type_ignores=[])
    namespace = {"FOOT_PATTERN": re.compile(pattern, re.I)}
    exec(compile(module, str(SOURCE), "exec"), namespace)
    return namespace["parse_foot"]


class HX711SerialTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.parse = staticmethod(load_parser())

    def test_real_hx711_four_digit_samples(self):
        for line, expected in [
            ("LEVEL=0000", 0),
            ("LEVEL=0050", 50),
            ("LEVEL=0063", 63),
            ("LEVEL=0100", 100),
            ("LEVEL=0096", 96),
            ("LEVEL=0018", 18),
        ]:
            with self.subTest(line=line):
                self.assertEqual(self.parse(line), expected)

    def test_old_serial_formats(self):
        self.assertEqual(self.parse("LEVEL:65"), 65)
        self.assertEqual(self.parse("65"), 65)
        self.assertEqual(self.parse("sensor=23"), 23)

    def test_ignores_boot_text_and_corrupt_lines(self):
        for line in ["HX711 ready. Output format: LEVEL=xxxx (0000..0100)",
                     "V", "L=0000", "LEVEL=0101", "LEVEL=1000",
                     "LEVEL=00000", "", "garbage"]:
            with self.subTest(line=line):
                self.assertIsNone(self.parse(line))


if __name__ == "__main__":
    unittest.main()
