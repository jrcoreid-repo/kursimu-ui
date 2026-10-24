"""Core protocol checks that run without a GUI or a physical device."""
import sys
import struct
import types
import unittest

try:
    import dearpygui.dearpygui
except ImportError:
    pkg = types.ModuleType("dearpygui")
    gui = types.ModuleType("dearpygui.dearpygui")
    pkg.dearpygui = gui
    sys.modules["dearpygui"] = pkg
    sys.modules["dearpygui.dearpygui"] = gui

from kursimu_gui_v2 import (
    crc16, command_packet, check_packet,
    parsed_result, parsed_status, parse_foot, OP_START
)


class ProtocolTests(unittest.TestCase):
    def test_crc_reference(self):
        self.assertEqual(crc16(b"123456789"), 0x29B1)

    def test_command_frame(self):
        frame = command_packet(OP_START, 0x12345678)
        self.assertEqual(len(frame), 14)
        self.assertTrue(check_packet(frame, 14, OP_START))

    def test_result_and_derived_metrics(self):
        data = bytearray(16)
        data[0:4] = bytes([0xA5, 1, 0x82, 1])
        struct.pack_into("<I", data, 4, 21)
        data[8:12] = bytes([120, 80, 72, 0])
        struct.pack_into("<H", data, 14, crc16(data[:14]))
        result = parsed_result(bytes(data))
        self.assertEqual(result["session"], 21)
        self.assertEqual(result["pp"], 40)
        self.assertAlmostEqual(result["map"], 93.3)
        data[8] ^= 1
        self.assertIsNone(parsed_result(bytes(data)))

    def test_status_and_serial(self):
        data = bytearray(20)
        data[0:6] = bytes([0xA5, 1, 0x83, 5, 1, 0x03])
        struct.pack_into("<III", data, 6, 20, 20, 10)
        struct.pack_into("<H", data, 18, crc16(data[:18]))
        status = parsed_status(bytes(data))
        self.assertEqual(status["total"], 20)
        self.assertTrue(status["pending"])
        self.assertEqual(parse_foot("LEVEL:65"), 65)
        self.assertIsNone(parse_foot("101"))


if __name__ == "__main__":
    unittest.main()
