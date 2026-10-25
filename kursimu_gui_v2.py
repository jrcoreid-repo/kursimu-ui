"""
KursiMu GUI v2.11.1 - reliable emergency shutdown during interrupted BP.
Windows 11 | Python 3.10+

BP:  Seeed XIAO ESP32-C3, firmware KursiMu BLE V13.2
     BLE 5 characteristic GATT protocol with CRC16 CCITT-FALSE.
Foot: Arduino serial UART 115200; send a 0-100 integer, or LEVEL:NN.

Launch:
  pip install dearpygui bleak pyserial
  python kursimu_gui_v2.py
  python kursimu_gui_v2.py --demo     # UI preview, no physical devices

BP values are prototype device readings; MAP is an estimate, not a direct sensor measurement.
No automatic START command is sent on BLE reconnect.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
from datetime import datetime
import json
import os
from pathlib import Path
import queue
import random
import re
import secrets
import struct
import threading
import time
import sys
import ctypes
import warnings
from typing import TextIO

from session_rules import evaluate_session, requires_exit_confirmation

try:
    import pygame
except ImportError:
    pygame = None

import dearpygui.dearpygui as dpg

try:
    from bleak import BleakScanner, BleakClient
except ImportError:
    BleakScanner = None
    BleakClient = None

# Windows WinRT deprecation from Bleak's own internals. Scope the filter to
# this exact legacy API warning; genuine BLE errors remain fully visible.
warnings.filterwarnings(
    "ignore",
    message=r"GattCharacteristic\.write_value_with_result_and_option_async\(\) is deprecated.*",
    category=DeprecationWarning,
    module=r"bleak\.backends\.winrt\.client",
)

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    serial = None
    list_ports = None

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "kursimu_data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
RAW_RESULTS = DATA_DIR / "kursimu_results.jsonl"
EXPORT_DIR = DATA_DIR / "exports"
EXPORT_DIR.mkdir(exist_ok=True)
SESSION_DIR = DATA_DIR / "sessions"
SESSION_DIR.mkdir(exist_ok=True)
LOGO_DIR = ROOT / "logos"
LOGO_DIR.mkdir(exist_ok=True)
MUSIC_DIR = ROOT / "music"
MUSIC_DIR.mkdir(exist_ok=True)
DEFAULT_MUSIC = MUSIC_DIR / "relaksasi.mp3"
MUSIC_EXTENSIONS = {".mp3", ".wav", ".ogg"}
USER_SETTINGS_FILE = DATA_DIR / "ui_settings.json"


def discover_music() -> Path | None:
    """Discover any playable-extension file under music/, regardless of filename."""
    try:
        songs = sorted(
            (p.resolve() for p in MUSIC_DIR.rglob("*")
             if p.is_file() and p.suffix.lower() in MUSIC_EXTENSIONS
             and not p.name.startswith(".")),
            key=lambda p: (p.name.casefold() != "relaksasi.mp3",
                           p.suffix.lower() != ".mp3", p.name.casefold()),
        )
    except OSError:
        songs = []
    return songs[0] if songs else None


def remembered_music() -> Path | None:
    """Restore a manually selected track; otherwise use music/ auto-discovery."""
    try:
        with USER_SETTINGS_FILE.open("r", encoding="utf-8") as f:
            entry = json.load(f)
        selected = Path(entry.get("music_path", ""))
        if selected.is_file() and selected.suffix.lower() in MUSIC_EXTENSIONS:
            return selected.resolve()
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return discover_music()


def save_music_choice(path: Path) -> None:
    # Only a local music path, no patient data.
    data = {"music_path": str(path.resolve())}
    temp = USER_SETTINGS_FILE.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    temp.replace(USER_SETTINGS_FILE)
FOOT_LOG_MIN_PERIOD = 0.20   # 5 samples/s maximum written from Arduino readings


BLE_NAME = "KURSIMU-BP-C3"
SERVICE_UUID = "7b973ce0-73f1-4c92-879f-91a395f6e101"
COMMAND_UUID = "7b973ce0-73f1-4c92-879f-91a395f6e102"
RESPONSE_UUID = "7b973ce0-73f1-4c92-879f-91a395f6e103"
RESULT_UUID = "7b973ce0-73f1-4c92-879f-91a395f6e104"
STATUS_UUID = "7b973ce0-73f1-4c92-879f-91a395f6e105"

OP_PING = 1
OP_START = 2
OP_STATUS = 3
OP_GET_RESULT = 4
OP_ACK_RESULT = 5
ACK_NAMES = ["OK", "BUSY", "DUPLICATE", "INVALID", "NOT_FOUND", "WRONG_SESSION", "NOT_READY", "RESULT_PENDING"]
STATE_NAMES = ["IDLE", "STARTING", "INFLATING", "HOLDING", "WAIT_RESULT", "COMPLETE", "ERROR"]
POWER_NAMES = ["UNKNOWN", "ON", "OFF"]

BG = (243, 249, 245, 255)
WHITE = (255, 255, 255, 255)
INK = (25, 59, 43, 255)
MUTED = (105, 135, 119, 255)
GREEN = (28, 139, 78, 255)
DARK_GREEN = (16, 100, 57, 255)
PALE = (229, 245, 235, 255)
LINE = (217, 233, 222, 255)
ORANGE = (202, 131, 35, 255)
RED = (191, 65, 64, 255)
BLUE = (44, 119, 171, 255)
TEAL = (22, 112, 109, 255)
GRAY = (104, 131, 118, 255)
MED_BLUE = (41, 106, 177, 255)
MED_TEAL = (20, 136, 125, 255)
MED_PURPLE = (108, 85, 173, 255)
MED_AMBER = (169, 109, 26, 255)
SKY_TINT = (234, 244, 255, 255)
MINT_TINT = (231, 247, 242, 255)
LILAC_TINT = (245, 241, 255, 255)
AMBER_TINT = (255, 246, 230, 255)


def crc16(data: bytes) -> int:
    value = 0xFFFF
    for byte in data:
        value ^= byte << 8
        for _ in range(8):
            value = ((value << 1) ^ 0x1021) & 0xFFFF if value & 0x8000 else (value << 1) & 0xFFFF
    return value


def check_packet(frame: bytes, size: int, opcode: int) -> bool:
    return (len(frame) == size and frame[0] == 0xA5 and frame[1] == 1
            and frame[2] == opcode and struct.unpack_from("<H", frame, size - 2)[0] == crc16(frame[:-2]))


def command_packet(opcode: int, request_id: int, argument: int = 0) -> bytes:
    body = struct.pack("<BBBBII", 0xA5, 1, opcode, 0, request_id, argument)
    return body + struct.pack("<H", crc16(body))


def parsed_status(raw: bytes) -> dict | None:
    if not check_packet(raw, 20, 0x83):
        return None
    return {
        "state": raw[3], "power": raw[4], "flags": raw[5],
        "session": struct.unpack_from("<I", raw, 6)[0],
        "total": struct.unpack_from("<I", raw, 10)[0],
        "success": struct.unpack_from("<I", raw, 14)[0],
        "pending": bool(raw[5] & 2),
    }


def parsed_result(raw: bytes) -> dict | None:
    if not check_packet(raw, 16, 0x82) or not (raw[3] & 1):
        return None
    result = {
        "session": struct.unpack_from("<I", raw, 4)[0],
        "sys": raw[8], "dia": raw[9], "pulse": raw[10], "err": raw[11],
        "flags": raw[3], "timestamp": datetime.now().isoformat(timespec="seconds")
    }
    if not (result["session"] > 0 and result["err"] == 0 and
            40 <= result["sys"] <= 255 and 20 <= result["dia"] <= 200 and
            25 <= result["pulse"] <= 240 and result["sys"] > result["dia"]):
        return None
    result["pp"] = result["sys"] - result["dia"]
    result["map"] = round(result["dia"] + result["pp"] / 3, 1)
    return result


def load_saved_ids() -> set[int]:
    ids: set[int] = set()
    try:
        with RAW_RESULTS.open("r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    entry = json.loads(line)
                    ids.add(int(entry["session"]))
                except (json.JSONDecodeError, KeyError, ValueError, TypeError):
                    pass
    except OSError:
        pass
    return ids


class BPWorker:
    """Background BLE task. All DPG calls must stay on the main GUI thread."""

    def __init__(self, events: queue.Queue, requests: queue.Queue):
        self.events = events
        self.requests = requests
        self.stop = threading.Event()
        self.saved_ids = load_saved_ids()
        self.pending_futures: dict[int, asyncio.Future] = {}
        self.handling_result: set[int] = set()
        self.client = None
        self.last_status = 0.0
        self.cmd_lock: asyncio.Lock | None = None
        self.thread = threading.Thread(target=self.thread_main, name="KursiMu-BLE", daemon=True)

    def start(self):
        self.thread.start()

    def thread_main(self):
        try:
            asyncio.run(self.run())
        except Exception as exc:
            self.events.put({"type": "log", "message": f"BLE thread: {exc}"})

    async def send_command(self, op: int, arg: int = 0, timeout: float = 6.0) -> dict:
        if not self.client or not self.client.is_connected:
            raise ConnectionError("BLE belum terhubung")
        assert self.cmd_lock is not None
        async with self.cmd_lock:
            request_id = secrets.randbelow(0xFFFFFFFE) + 1
            future = asyncio.get_running_loop().create_future()
            self.pending_futures[request_id] = future
            try:
                await self.client.write_gatt_char(COMMAND_UUID, command_packet(op, request_id, arg), response=True)
                return await asyncio.wait_for(future, timeout=timeout)
            finally:
                self.pending_futures.pop(request_id, None)

    def notification(self, uuid: str, data: bytearray):
        raw = bytes(data)
        if uuid == RESPONSE_UUID:
            if not check_packet(raw, 16, 0x81):
                return
            request_id = struct.unpack_from("<I", raw, 4)[0]
            future = self.pending_futures.get(request_id)
            if future is not None and not future.done():
                future.set_result({
                    "ack": raw[3], "request_id": request_id,
                    "session": struct.unpack_from("<I", raw, 8)[0],
                    "state": raw[12], "power": raw[13]
                })
        elif uuid == STATUS_UUID:
            status = parsed_status(raw)
            if status is not None:
                self.last_status = time.monotonic()
                self.events.put({"type": "status", "value": status})
        elif uuid == RESULT_UUID:
            result = parsed_result(raw)
            if result is not None and result["session"] not in self.handling_result:
                self.handling_result.add(result["session"])
                asyncio.create_task(self.save_and_ack(result))

    async def save_and_ack(self, result: dict):
        sid = result["session"]
        try:
            is_new = sid not in self.saved_ids
            if is_new:
                try:
                    # Persist first, then ACK device. On reconnect, recovery is idempotent.
                    await asyncio.to_thread(self.persist_result, result)
                    self.saved_ids.add(sid)
                except OSError as exc:
                    self.events.put({"type": "log", "message": f"GAGAL simpan sesi {sid}: {exc}; tanpa ACK"})
                    return
                self.events.put({"type": "result", "value": result})
            try:
                answer = await self.send_command(OP_ACK_RESULT, sid)
                if answer["ack"] == 0:
                    self.events.put({"type": "log", "message": f"BLE hasil sesi {sid} disimpan dan ACK"})
                else:
                    self.events.put({"type": "log", "message": f"ACK sesi {sid}: {ACK_NAMES[answer['ack']] if answer['ack'] < len(ACK_NAMES) else answer['ack']}"})
            except Exception as exc:
                self.events.put({"type": "log", "message": f"ACK sesi {sid} tertunda: {exc}"})
        finally:
            self.handling_result.discard(sid)

    @staticmethod
    def persist_result(result: dict):
        with RAW_RESULTS.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(result, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    async def run(self):
        if BleakScanner is None or BleakClient is None:
            self.events.put({"type": "log", "message": "Pasang Bleak: pip install bleak"})
            return
        self.cmd_lock = asyncio.Lock()
        attempt = 0
        while not self.stop.is_set():
            attempt += 1
            self.events.put({"type": "ble", "connected": False, "text": f"Mencari perangkat... ({attempt})"})
            try:
                def match(dev, adv):
                    name = (getattr(adv, "local_name", None) or dev.name or "").upper()
                    services = [s.lower() for s in (getattr(adv, "service_uuids", None) or [])]
                    return BLE_NAME.upper() in name or SERVICE_UUID.lower() in services

                device = await BleakScanner.find_device_by_filter(match, timeout=8.0)
                if device is None:
                    self.events.put({"type": "log", "message": "BP belum ditemukan; scan ulang"})
                    await asyncio.sleep(1.5)
                    continue
                self.events.put({"type": "ble", "connected": False, "text": "Menghubungkan..."})
                async with BleakClient(device, timeout=15.0) as client:
                    self.client = client
                    self.last_status = time.monotonic()
                    await client.start_notify(RESPONSE_UUID, lambda _, raw: self.notification(RESPONSE_UUID, raw))
                    await client.start_notify(STATUS_UUID, lambda _, raw: self.notification(STATUS_UUID, raw))
                    await client.start_notify(RESULT_UUID, lambda _, raw: self.notification(RESULT_UUID, raw))
                    answer = await self.send_command(OP_PING)
                    if answer["ack"] != 0:
                        raise RuntimeError("PING BLE ditolak")
                    await self.send_command(OP_STATUS)
                    await self.send_command(OP_GET_RESULT)  # pending unacked result recovery
                    self.events.put({"type": "ble", "connected": True, "text": "BP BLE Terhubung"})
                    self.events.put({"type": "log", "message": "BLE siap: PING, STATUS, RESULT, ACK"})
                    while client.is_connected and not self.stop.is_set():
                        if time.monotonic() - self.last_status > 9.0:
                            raise RuntimeError("Heartbeat BP hilang > 9 detik")
                        try:
                            request = self.requests.get_nowait()
                        except queue.Empty:
                            await asyncio.sleep(0.09)
                            continue
                        op = request.get("op")
                        try:
                            answer = await self.send_command(op, request.get("arg", 0))
                            self.events.put({"type": "command_ack", "op": op,
                                             "target": request.get("target"), "answer": answer})
                        except Exception as exc:
                            self.events.put({"type": "command_error", "target": request.get("target"),
                                             "op": op, "message": str(exc)})
            except Exception as exc:
                self.events.put({"type": "log", "message": f"BLE reconnect: {exc}"})
            finally:
                self.client = None
                self.events.put({"type": "ble", "connected": False, "text": "BP BLE Terputus"})
                # No START is ever replayed after reconnect.
                for future in self.pending_futures.values():
                    if not future.done():
                        future.cancel()
                self.pending_futures.clear()
            if not self.stop.is_set():
                await asyncio.sleep(2.0)


FOOT_PATTERN = re.compile(r"(?:level|pressure|pijakan|nilai|sensor)\s*[:=]\s*(\d{1,3})(?!\d)", re.I)


def parse_foot(line: str) -> int | None:
    line = line.strip()
    if line.isdecimal():
        level = int(line)
    else:
        match = FOOT_PATTERN.search(line)
        if match is None:
            return None
        level = int(match.group(1))
    return level if 0 <= level <= 100 else None


class FootWorker:
    def __init__(self, events: queue.Queue):
        self.events = events
        self.stop = threading.Event()
        self.port_lock = threading.Lock()
        self.selected_port: str | None = None
        self.thread = threading.Thread(target=self.run, name="KursiMu-Serial", daemon=True)

    def start(self):
        self.thread.start()

    def set_port(self, port: str | None):
        with self.port_lock:
            self.selected_port = port

    def run(self):
        if serial is None:
            self.events.put({"type": "log", "message": "Pasang pyserial: pip install pyserial"})
            return
        while not self.stop.is_set():
            with self.port_lock:
                port = self.selected_port
            if not port:
                time.sleep(0.25)
                continue
            try:
                with serial.Serial(port, 115200, timeout=0.45) as ser:
                    self.events.put({"type": "serial", "connected": True, "text": f"Arduino {port}"})
                    while not self.stop.is_set():
                        with self.port_lock:
                            if self.selected_port != port:
                                break
                        data = ser.readline()
                        if not data:
                            continue
                        value = parse_foot(data.decode("utf-8", "replace"))
                        if value is not None:
                            self.events.put({"type": "foot", "level": value})
            except (OSError, Exception) as exc:
                self.events.put({"type": "log", "message": f"Serial {port}: {exc}"})
            self.events.put({"type": "serial", "connected": False, "text": "Arduino terputus"})
            time.sleep(1.5)



class KursiMuApp:
    """Three-panel desktop app. Only main GUI thread may call DPG APIs."""

    def __init__(self, demo: bool):
        self.demo = demo
        self.events: queue.Queue = queue.Queue()
        self.ble_requests: queue.Queue = queue.Queue()
        self.bp_worker = None if demo else BPWorker(self.events, self.ble_requests)
        self.foot_worker = None if demo else FootWorker(self.events)
        self.ble_connected = demo
        self.serial_connected = demo
        self.bp_status: dict | None = None
        self.pending_target: dict | None = None
        self.bp_popup_hide_at = None
        self.bp_popup_stage = None
        self.unassigned: dict | None = None
        self.readings = {"awal": None, "akhir": None}
        self.last_foot = 0
        self.foot_count = 0
        self.foot_sum = 0.0
        self.foot_min = None
        self.foot_max = None
        self.last_foot_logged = 0.0
        self.last_foot_event = 0.0
        self.session_state = "IDLE"  # IDLE/RUNNING/PAUSED/FINISHED/SAVE_FAILED
        self.baseline_identity: tuple[str, str] | None = None
        self.session_uid = None
        self.session_name = ""
        self.session_user_id = ""
        self.session_started = None
        self.session_finished = None
        self.elapsed_before_pause = 0.0
        self.running_since = None
        self.session_files: dict[str, Path] = {}
        self.foot_file: TextIO | None = None
        self.foot_writer = None
        self.music_path = remembered_music()
        self.music_choices: dict[str, Path] = {}
        self.audio_ready = False
        self.audio_playing = False
        self.audio_error = ""
        self.last_layout_size = None
        self.logos = []
        self.foot_history_x = []
        self.foot_history_y = []
        self.foot_tick = 0
        self.last_demo_tick = 0.0
        self.demo_due = None
        self.demo_session = 100
        self.demo_target = None
        self.fonts = {}
        self.closing = False
        self.messages = []

    def setup_fonts(self):
        paths = [
            (Path('C:/Windows/Fonts/segoeui.ttf'), Path('C:/Windows/Fonts/segoeuib.ttf')),
            (Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'), Path('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'))
        ]
        font, bold = next(((a,b) for a,b in paths if a.exists()), (None,None))
        if font is None:
            return
        with dpg.font_registry():
            # Recent DearPyGui versions build character ranges automatically.
            # add_font_chars() is deprecated and now a no-op.
            self.fonts['base'] = dpg.add_font(str(font), 16)
            self.fonts['small'] = dpg.add_font(str(font), 14)
            self.fonts['title'] = dpg.add_font(str(bold if bold.exists() else font), 35)
            self.fonts['hero'] = dpg.add_font(str(bold if bold.exists() else font), 40)
            self.fonts['section'] = dpg.add_font(str(bold if bold.exists() else font), 19)
            self.fonts['metric'] = dpg.add_font(str(bold if bold.exists() else font), 23)
            self.fonts['compare'] = dpg.add_font(str(bold if bold.exists() else font), 28)
            # Windows Segoe UI Symbol covers the icons more reliably than Segoe UI.
            # Keep Latin glyphs as well so full button labels remain readable.
            icon_font = Path('C:/Windows/Fonts/seguisym.ttf')
            button_font = icon_font if icon_font.is_file() else font
            self.fonts['action'] = dpg.add_font(str(button_font), 19)
        dpg.bind_font(self.fonts['base'])

    def bind_font(self, tag, name):
        if name in self.fonts:
            dpg.bind_item_font(tag, self.fonts[name])

    def create_themes(self):
        with dpg.theme(tag='theme_global'):
            with dpg.theme_component(dpg.mvAll):
                for idx, color in (
                    (dpg.mvThemeCol_WindowBg,BG), (dpg.mvThemeCol_ChildBg,WHITE),
                    (dpg.mvThemeCol_PopupBg,WHITE),
                    (dpg.mvThemeCol_Text,INK), (dpg.mvThemeCol_Border,LINE),
                    (dpg.mvThemeCol_FrameBg,(246,250,247,255)),
                    (dpg.mvThemeCol_FrameBgHovered,PALE),
                    (dpg.mvThemeCol_FrameBgActive,PALE),
                    (dpg.mvThemeCol_Button,GREEN),
                    (dpg.mvThemeCol_ButtonHovered,DARK_GREEN),
                    (dpg.mvThemeCol_ButtonActive,DARK_GREEN),
                    (dpg.mvThemeCol_Header,PALE),
                    (dpg.mvThemeCol_HeaderHovered,(208,237,220,255)),
                    (dpg.mvThemeCol_HeaderActive,(184,224,201,255)),
                    (dpg.mvThemeCol_NavHighlight,(66,155,106,255)),
                    (dpg.mvThemeCol_CheckMark,GREEN),
                ):
                    dpg.add_theme_color(idx,color)
                dpg.add_theme_style(dpg.mvStyleVar_WindowPadding,10,8)
                dpg.add_theme_style(dpg.mvStyleVar_FramePadding,8,5)
                dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing,6,5)
                dpg.add_theme_style(dpg.mvStyleVar_FrameRounding,7)
                dpg.add_theme_style(dpg.mvStyleVar_PopupRounding,8)
                dpg.add_theme_style(dpg.mvStyleVar_PopupBorderSize,1)
                dpg.add_theme_style(dpg.mvStyleVar_ChildRounding,12)
                dpg.add_theme_style(dpg.mvStyleVar_WindowBorderSize,0)
        with dpg.theme(tag='theme_com_dropdown'):
            with dpg.theme_component(dpg.mvCombo):
                dpg.add_theme_color(dpg.mvThemeCol_PopupBg,WHITE)
                dpg.add_theme_color(dpg.mvThemeCol_Text,INK)
                dpg.add_theme_color(dpg.mvThemeCol_Border,LINE)
                dpg.add_theme_color(dpg.mvThemeCol_FrameBg,(243,249,246,255))
                dpg.add_theme_color(dpg.mvThemeCol_FrameBgHovered,(226,244,232,255))
                dpg.add_theme_color(dpg.mvThemeCol_FrameBgActive,(226,244,232,255))
                dpg.add_theme_color(dpg.mvThemeCol_Header,PALE)
                dpg.add_theme_color(dpg.mvThemeCol_HeaderHovered,(208,237,220,255))
                dpg.add_theme_color(dpg.mvThemeCol_HeaderActive,(184,224,201,255))
                dpg.add_theme_style(dpg.mvStyleVar_PopupRounding,8)
                dpg.add_theme_style(dpg.mvStyleVar_PopupBorderSize,1)
        with dpg.theme(tag='theme_header'):
            with dpg.theme_component(dpg.mvChildWindow):
                dpg.add_theme_color(dpg.mvThemeCol_ChildBg, (249, 253, 250, 255))
                dpg.add_theme_color(dpg.mvThemeCol_Border, LINE)
                dpg.add_theme_style(dpg.mvStyleVar_ChildRounding, 12)
                dpg.add_theme_style(dpg.mvStyleVar_ChildBorderSize, 1)
                dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, 18, 8)
                dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, 7, 6)
        with dpg.theme(tag='theme_music_dialog'):
            with dpg.theme_component(dpg.mvAll):
                dpg.add_theme_color(dpg.mvThemeCol_WindowBg, WHITE)
                dpg.add_theme_color(dpg.mvThemeCol_ChildBg, WHITE)
                dpg.add_theme_color(dpg.mvThemeCol_PopupBg, WHITE)
                dpg.add_theme_color(dpg.mvThemeCol_FrameBg, (245, 250, 247, 255))
                dpg.add_theme_color(dpg.mvThemeCol_TitleBg, DARK_GREEN)
                dpg.add_theme_color(dpg.mvThemeCol_TitleBgActive, GREEN)
                dpg.add_theme_color(dpg.mvThemeCol_ModalWindowDimBg, (23, 43, 33, 130))
                dpg.add_theme_color(dpg.mvThemeCol_Text, INK)
                dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, 22, 18)
                dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, 10, 9)
                dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 8)
        with dpg.theme(tag='theme_foot_progress'):
            with dpg.theme_component(dpg.mvProgressBar):
                dpg.add_theme_color(dpg.mvThemeCol_PlotHistogram, GREEN)
                dpg.add_theme_color(dpg.mvThemeCol_FrameBg, MINT_TINT)
        with dpg.theme(tag='theme_card'):
            with dpg.theme_component(dpg.mvChildWindow):
                dpg.add_theme_color(dpg.mvThemeCol_ChildBg,WHITE)
                dpg.add_theme_color(dpg.mvThemeCol_Border,LINE)
                dpg.add_theme_style(dpg.mvStyleVar_ChildRounding,12)
                dpg.add_theme_style(dpg.mvStyleVar_ChildBorderSize,1)
        with dpg.theme(tag='theme_measure'):
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Button,GREEN)
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered,DARK_GREEN)
                dpg.add_theme_color(dpg.mvThemeCol_Text,WHITE)
            with dpg.theme_component(dpg.mvButton, enabled_state=False):
                dpg.add_theme_color(dpg.mvThemeCol_Button,(120,184,145,255))
                dpg.add_theme_color(dpg.mvThemeCol_Text,(241,252,245,255))
                dpg.add_theme_color(dpg.mvThemeCol_TextDisabled,(241,252,245,255))
        with dpg.theme(tag='theme_pause'):
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Button,(165,105,18,255))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered,(136,82,11,255))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive,(117,70,9,255))
                dpg.add_theme_color(dpg.mvThemeCol_Text,WHITE)
            with dpg.theme_component(dpg.mvButton, enabled_state=False):
                dpg.add_theme_color(dpg.mvThemeCol_Button,(207,169,100,255))
                dpg.add_theme_color(dpg.mvThemeCol_Text,(255,249,236,255))
                dpg.add_theme_color(dpg.mvThemeCol_TextDisabled,(255,249,236,255))
        with dpg.theme(tag='theme_finish'):
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Button,(188,76,78,255))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered,(155,56,59,255))
                dpg.add_theme_color(dpg.mvThemeCol_Text,WHITE)
            with dpg.theme_component(dpg.mvButton, enabled_state=False):
                dpg.add_theme_color(dpg.mvThemeCol_Button,(204,143,144,255))
                dpg.add_theme_color(dpg.mvThemeCol_Text,(255,243,243,255))
                dpg.add_theme_color(dpg.mvThemeCol_TextDisabled,(255,243,243,255))
        with dpg.theme(tag='theme_music'):
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Button,BLUE)
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered,(28,96,151,255))
                dpg.add_theme_color(dpg.mvThemeCol_Text,WHITE)
        with dpg.theme(tag='theme_connect'):
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Button,TEAL)
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered,(16,91,87,255))
                dpg.add_theme_color(dpg.mvThemeCol_Text,WHITE)
        with dpg.theme(tag='theme_disconnect'):
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Button,(188,76,78,255))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered,(155,56,59,255))
                dpg.add_theme_color(dpg.mvThemeCol_Text,WHITE)
            with dpg.theme_component(dpg.mvButton, enabled_state=False):
                dpg.add_theme_color(dpg.mvThemeCol_Button,(205,145,146,255))
                dpg.add_theme_color(dpg.mvThemeCol_TextDisabled,WHITE)
        with dpg.theme(tag='theme_neutral'):
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Button,(225,236,229,255))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered,(202,223,209,255))
                dpg.add_theme_color(dpg.mvThemeCol_Text,DARK_GREEN)
        with dpg.theme(tag='theme_akhir'):
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Button,(212,126,37,255))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered,(181,99,23,255))
                dpg.add_theme_color(dpg.mvThemeCol_Text,WHITE)
            with dpg.theme_component(dpg.mvButton, enabled_state=False):
                dpg.add_theme_color(dpg.mvThemeCol_Button,(217,165,113,255))
                dpg.add_theme_color(dpg.mvThemeCol_Text,(255,250,244,255))
                dpg.add_theme_color(dpg.mvThemeCol_TextDisabled,(255,250,244,255))
        with dpg.theme(tag='theme_close'):
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Button,(230,238,232,255))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered,(246,222,220,255))
                dpg.add_theme_color(dpg.mvThemeCol_Text,(137,47,48,255))
        # Tiles use gentle medical-style colors, not diagnostic categories.
        for tag, bg, border in (
            ('tile_systolic', SKY_TINT, (207, 224, 243, 255)),
            ('tile_diastolic', MINT_TINT, (208, 234, 224, 255)),
            ('tile_hr', (237, 248, 255, 255), (216, 234, 246, 255)),
            ('tile_pp', AMBER_TINT, (247, 230, 200, 255)),
            ('tile_map', LILAC_TINT, (229, 220, 247, 255)),
            ('tile_comparison', (248, 252, 250, 255), (204, 228, 215, 255)),
        ):
            with dpg.theme(tag=tag):
                with dpg.theme_component(dpg.mvChildWindow):
                    dpg.add_theme_color(dpg.mvThemeCol_ChildBg, bg)
                    dpg.add_theme_color(dpg.mvThemeCol_Border, border)
                    dpg.add_theme_style(dpg.mvStyleVar_ChildRounding, 10)
                    dpg.add_theme_style(dpg.mvStyleVar_ChildBorderSize, 1)
                    dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, 10, 8)
        dpg.bind_theme('theme_global')

    @staticmethod
    def screen_size():
        if sys.platform == 'win32':
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except (AttributeError, OSError):
                pass
            try:
                return (ctypes.windll.user32.GetSystemMetrics(0),
                        ctypes.windll.user32.GetSystemMetrics(1))
            except (AttributeError, OSError):
                pass
        return 1600, 900

    def load_logos(self):
        """Load up to 6 user-provided PNG/JPG logos from logos folder."""
        paths = [p for p in sorted(LOGO_DIR.iterdir())
                 if p.suffix.lower() in ('.png','.jpg','.jpeg','.bmp')][:6]
        with dpg.texture_registry(tag='logo_textures'):
            for index, path in enumerate(paths):
                try:
                    loaded = dpg.load_image(str(path))
                    if loaded is None:
                        continue
                    w,h,channels,data = loaded
                    tag = f'logo_tex_{index}'
                    dpg.add_static_texture(width=w,height=h,default_value=data,tag=tag)
                    self.logos.append((tag,w,h,path.name))
                except Exception as exc:
                    print(f'[LOGO] Cannot load {path}: {exc}')




    def add_metric(self, target, label, unit):
        field = 'pulse' if label == 'HR' else label.lower()
        tile, accent = {
            'pulse': ('tile_hr', MED_BLUE),
            'pp': ('tile_pp', MED_AMBER),
            'map': ('tile_map', MED_PURPLE),
        }[field]
        box = f'metric_box_{target}_{field}'
        with dpg.child_window(tag=box, width=115, height=89,
                              border=True, no_scrollbar=True):
            dpg.add_text(label, color=MUTED)
            with dpg.group(horizontal=True):
                tag = f'{target}_{field}'
                dpg.add_text('--', tag=tag, color=accent)
                self.bind_font(tag, 'metric')
                dpg.add_text(unit, color=MUTED)
        dpg.bind_item_theme(box, tile)


    def add_bp_panel(self, target, number, label):
        tag = f'panel_{target}'
        with dpg.child_window(tag=tag, width=400, height=490,
                              border=True, no_scrollbar=True):
            dpg.add_text(f'{number}  /  TEKANAN DARAH {label}',
                         tag=f'{target}_title', color=DARK_GREEN)
            self.bind_font(f'{target}_title', 'section')
            dpg.add_spacer(height=8)
            with dpg.group(horizontal=True):
                for field, title, tile, accent in (
                    ('sys', 'SISTOL', 'tile_systolic', MED_BLUE),
                    ('dia', 'DIASTOL', 'tile_diastolic', MED_TEAL),
                ):
                    box = f'bp_box_{target}_{field}'
                    with dpg.child_window(tag=box, width=170, height=132,
                                          border=True, no_scrollbar=True):
                        dpg.add_text(title, color=MUTED)
                        dpg.add_text('--', tag=f'{target}_{field}', color=accent)
                        self.bind_font(f'{target}_{field}', 'hero')
                        dpg.add_text('mmHg', color=MUTED)
                    dpg.bind_item_theme(box, tile)
            dpg.add_spacer(height=10)
            with dpg.group(horizontal=True):
                self.add_metric(target, 'HR', 'BPM')
                self.add_metric(target, 'PP', 'mmHg')
                self.add_metric(target, 'MAP', 'mmHg')
            dpg.add_spacer(height=13)
            dpg.add_button(label=f'UKUR {label}', tag=f'btn_{target}',
                           width=-1, height=60,
                           callback=lambda s,a,u: self.start_bp(u),
                           user_data=target)
            dpg.bind_item_theme(f'btn_{target}',
                                'theme_akhir' if target=='akhir' else 'theme_measure')
            dpg.add_spacer(height=10)
            dpg.add_text('Belum ada pengukuran', tag=f'{target}_detail', color=MUTED)
            with dpg.group(horizontal=True):
                dpg.add_text('Sesi', color=MUTED)
                dpg.add_text('--', tag=f'{target}_session', color=DARK_GREEN)
                dpg.add_spacer(width=14)
                dpg.add_text('Waktu', color=MUTED)
                dpg.add_text('--', tag=f'{target}_time', color=DARK_GREEN)
        dpg.bind_item_theme(tag, 'theme_card')



    def add_foot_panel(self):
        # Three aligned metric cards replace scattered gauge and text.
        # Single wide progress bar and full-width graph remain the focus.
        with dpg.child_window(tag='panel_foot', width=400, height=490,
                              border=True, no_scrollbar=True):
            dpg.add_text('01  /  PIJAKAN SENSOR', tag='foot_title',
                         color=DARK_GREEN)
            self.bind_font('foot_title','section')
            dpg.add_spacer(height=10)
            with dpg.group(horizontal=True):
                for field, title, default, theme, accent, font in (
                    ('foot_level','LEVEL SAAT INI','0%', 'tile_diastolic',MED_TEAL,'hero'),
                    ('foot_avg','RATA-RATA','--', 'tile_hr',MED_BLUE,'metric'),
                    ('foot_count','SAMPEL','0', 'tile_pp',MED_AMBER,'metric'),
                ):
                    box='foot_box_'+field
                    with dpg.child_window(tag=box, width=150, height=108,
                                          border=True, no_scrollbar=True):
                        dpg.add_text(title,color=MUTED)
                        dpg.add_spacer(height=4)
                        dpg.add_text(default,tag=field,color=accent)
                        self.bind_font(field,font)
                    dpg.bind_item_theme(box,theme)
            dpg.add_spacer(height=8)
            dpg.add_progress_bar(tag='foot_progress',default_value=0,
                                 overlay='0%',width=-1,height=24)
            dpg.bind_item_theme('foot_progress','theme_foot_progress')
            dpg.add_spacer(height=8)
            dpg.add_separator()
            dpg.add_text('GRAFIK PIJAKAN REAL-TIME',color=DARK_GREEN)
            with dpg.plot(tag='foot_plot',height=190,width=-1,
                          no_menus=True,no_box_select=True):
                dpg.add_plot_axis(dpg.mvXAxis,tag='foot_axis_x',no_tick_labels=True)
                with dpg.plot_axis(dpg.mvYAxis,tag='foot_axis_y'):
                    dpg.add_line_series([],[],label='Pijakan',tag='foot_series')
            dpg.set_axis_limits('foot_axis_y',0,100)
            dpg.set_axis_limits('foot_axis_x',0,60)
        dpg.bind_item_theme('panel_foot','theme_card')

    def add_foot_metric(self,name,tag):
        with dpg.child_window(width=170,height=70,border=False,no_scrollbar=True):
            dpg.add_text(name,color=MUTED)
            dpg.add_text('--',tag=tag,color=DARK_GREEN)
            self.bind_font(tag,'metric')


    def add_comparison_panel(self):
        with dpg.child_window(tag='panel_comparison',width=-1,height=148,
                              border=True,no_scrollbar=True):
            with dpg.group(horizontal=True):
                dpg.add_text('HASIL PERUBAHAN SETELAH RELAKSASI',
                             tag='comparison_title',color=DARK_GREEN)
                self.bind_font('comparison_title','section')
                dpg.add_spacer(width=16)
                dpg.add_text('Menunggu hasil AWAL dan AKHIR',
                             tag='comparison_hint',color=MUTED)
            dpg.add_spacer(height=9)
            with dpg.group(horizontal=True):
                for field,title,unit,tile,accent in (
                    ('sys','SISTOL','mmHg','tile_systolic',MED_BLUE),
                    ('dia','DIASTOL','mmHg','tile_diastolic',MED_TEAL),
                    ('pulse','HR','BPM','tile_hr',MED_BLUE),
                    ('pp','PP','mmHg','tile_pp',MED_AMBER),
                    ('map','MAP','mmHg','tile_map',MED_PURPLE),
                ):
                    box=f'delta_box_{field}'
                    with dpg.child_window(tag=box,width=160,height=91,
                                          border=True,no_scrollbar=True):
                        dpg.add_text(title,color=MUTED)
                        with dpg.group(horizontal=True):
                            dpg.add_text('--',tag=f'delta_{field}',color=accent)
                            self.bind_font(f'delta_{field}','compare')
                            dpg.add_text(unit,color=MUTED)
                    dpg.bind_item_theme(box,tile)
        dpg.bind_item_theme('panel_comparison','tile_comparison')

    def add_image_footer(self):
        with dpg.child_window(tag='footer_logos',height=105,width=-1,border=True,no_scrollbar=True):
            with dpg.group(horizontal=True,tag='logo_row'):
                if self.logos:
                    for index,(tex,w,h,name) in enumerate(self.logos):
                        dpg.add_image(tex,tag=f'logo_img_{index}',width=135,height=72)
                else:
                    # The bottom panel contains only an image placeholder;
                    # replace logos/*.png and restart to display actual logos.
                    dpg.add_text('Letakkan file logo PNG/JPG pada folder logos, lalu buka ulang aplikasi.',
                                 tag='logo_empty',color=MUTED)
        dpg.bind_item_theme('footer_logos','theme_card')

    def create_ui(self):
        dpg.create_context()
        self.setup_fonts()
        self.create_themes()
        self.load_logos()
        width,height = self.screen_size()
        dpg.create_viewport(title='KursiMu - Smart Relaxation Monitoring',
                            width=width,height=height,x_pos=0,y_pos=0,
                            decorated=False,always_on_top=True,resizable=False,
                            clear_color=BG)
        with dpg.window(tag='main',no_title_bar=True,no_move=True,no_resize=True,
                        no_collapse=True,no_scrollbar=True):
            with dpg.child_window(tag='topbar', width=-1, height=134,
                                  border=True, no_scrollbar=True):
                # Brand is intentionally the only headline; system information
                # lives on a quieter second row.
                with dpg.group(horizontal=True, tag='header'):
                    dpg.add_text('KursiMu', tag='app_title', color=DARK_GREEN)
                    self.bind_font('app_title', 'title')
                    if self.demo:
                        dpg.add_spacer(width=12)
                        dpg.add_text('MODE SIMULASI', color=ORANGE)
                dpg.add_spacer(height=2)
                with dpg.group(horizontal=True, tag='state_row'):
                    dpg.add_text('BP KursiMu', color=MUTED)
                    dpg.add_text('DEMO' if self.demo else 'Mencari...',
                                 tag='ble_state', color=ORANGE)
                    dpg.add_spacer(width=32)
                    dpg.add_text('Pijakan Sensor', color=MUTED)
                    dpg.add_text('DEMO' if self.demo else 'Belum terhubung',
                                 tag='serial_state', color=ORANGE)
                    dpg.add_spacer(width=32)
                    dpg.add_text('Status BP', color=MUTED)
                    dpg.add_text('DEMO' if self.demo else '--',
                                 tag='bp_state', color=DARK_GREEN)
                dpg.add_spacer(height=6)
                dpg.add_text('Ukur AWAL  >  Mulai relaksasi  >  Ukur AKHIR  >  Selesai',
                             tag='activity_line', color=MUTED)
                # Clock and Close are explicitly aligned to the upper right.
                dpg.add_text(datetime.now().strftime('%d %b %Y  %H:%M'),
                             tag='clock', color=MUTED)
                dpg.add_button(label='TUTUP APLIKASI   X', tag='btn_close',
                               callback=self.request_close, height=38, width=185)
                dpg.bind_item_theme('btn_close', 'theme_close')
            dpg.bind_item_theme('topbar', 'theme_header')
            with dpg.child_window(tag='toolbar',height=204,width=-1,border=True,no_scrollbar=True):
                with dpg.group(horizontal=True,tag='identity_row'):
                    dpg.add_text('NAMA PENGGUNA',color=MUTED)
                    dpg.add_input_text(tag='patient_name',hint='Nama pengguna',width=235,
                                       callback=self.on_identity_changed)
                    dpg.add_text('ID PENGGUNA',color=MUTED)
                    dpg.add_input_text(tag='patient_id',hint='ID unik pengguna',width=190,
                                       callback=self.on_identity_changed)
                    dpg.add_spacer(width=15)
                    dpg.add_text('DURASI',color=MUTED)
                    dpg.add_text('00:00:00',tag='elapsed',color=DARK_GREEN)
                    dpg.add_spacer(width=8)
                    dpg.add_text('STATUS',color=MUTED)
                    dpg.add_text('BELUM TERHUBUNG',tag='session_state',color=RED)
                    dpg.add_spacer(width=24)  # keep session status distinct from the next action
                    dpg.add_button(label='SESI BARU',tag='btn_new',width=140,callback=self.new_session)
                    dpg.bind_item_theme('btn_new','theme_neutral')
                dpg.add_separator()
                with dpg.group(horizontal=True,tag='action_row'):
                    dpg.add_button(label='▶  MULAI',tag='btn_start',height=47,width=250,
                                   callback=self.begin_relaxation)
                    dpg.bind_item_theme('btn_start','theme_measure')
                    dpg.add_button(label='⏸  PAUSE',tag='btn_pause',height=47,width=250,
                                   callback=self.pause_relaxation)
                    dpg.bind_item_theme('btn_pause','theme_pause')
                    dpg.add_button(label='■  SELESAI',tag='btn_finish',height=47,width=250,
                                   callback=self.finish_relaxation)
                    dpg.bind_item_theme('btn_finish','theme_finish')
                    dpg.add_button(label='♫  PILIH MUSIK',tag='btn_music',height=47,width=250,
                                   callback=self.open_music_picker)
                    dpg.bind_item_theme('btn_music','theme_music')
                    for action_tag in ('btn_start','btn_pause','btn_finish','btn_music'):
                        self.bind_font(action_tag,'action')
                with dpg.group(horizontal=True,tag='connect_row'):
                    dpg.add_text('COM PIJAKAN',color=MUTED)
                    dpg.add_combo(items=['Pilih COM'],default_value='Pilih COM',
                                  tag='port_combo',width=155)
                    dpg.bind_item_theme('port_combo','theme_com_dropdown')
                    dpg.add_button(label='REFRESH',tag='btn_refresh',callback=self.refresh_ports,width=150)
                    dpg.bind_item_theme('btn_refresh','theme_neutral')
                    dpg.add_button(label='⊕  SAMBUNG COM',tag='btn_connect',
                                   callback=self.toggle_serial,width=185)
                    dpg.bind_item_theme('btn_connect','theme_connect')
                    self.bind_font('btn_connect','action')
                    dpg.add_spacer(width=20,tag='connection_spacer')
                    dpg.add_text('LAGU:',color=MUTED)
                    dpg.add_text(self.music_path.name if self.music_path else 'MP3 belum ditemukan - pilih musik',
                                 tag='music_label',color=DARK_GREEN)
                with dpg.group(horizontal=True,tag='recovery_row'):
                    dpg.add_text('HASIL BP:',color=MUTED)
                    dpg.add_text('Tidak ada',tag='unassigned_label',color=DARK_GREEN)
                    dpg.add_button(label='KE AWAL',tag='assign_awal',show=False,
                                   callback=lambda *args: self.assign_unassigned('awal'))
                    dpg.add_button(label='KE AKHIR',tag='assign_akhir',show=False,
                                   callback=lambda *args: self.assign_unassigned('akhir'))
            with dpg.group(horizontal=True,tag='panel_row'):
                self.add_foot_panel()
                self.add_bp_panel('awal','02','AWAL')
                self.add_bp_panel('akhir','03','AKHIR')
            self.add_comparison_panel()
            self.add_image_footer()
        # Custom white/green picker, replacing DearPyGui's dark file_dialog.
        with dpg.window(tag='music_dialog',
                        label='Pilih Musik Relaksasi',
                        modal=True, show=False,
                        no_resize=True, no_scrollbar=True,
                        width=800, height=395):
            dpg.add_text('MUSIK RELAKSASI', color=DARK_GREEN)
            dpg.add_text('Pilih satu lagu MP3, WAV, atau OGG dari folder music atau komputer.', color=MUTED)
            dpg.add_spacer(height=7)
            dpg.add_text('LAGU DALAM FOLDER MUSIC', color=DARK_GREEN)
            with dpg.group(horizontal=True):
                dpg.add_combo(tag='music_file_choices',
                              items=['Belum ada musik'],
                              default_value='Belum ada musik',
                              width=570,
                              callback=self.music_dropdown_changed)
                dpg.add_button(label='SCAN ULANG', width=150,
                               callback=self.scan_music_folder)
            dpg.add_spacer(height=7)
            dpg.add_text('LOKASI FILE MUSIK', color=DARK_GREEN)
            dpg.add_input_text(tag='music_path_entry',
                               hint='Contoh: C:\\Users\\...\\Downloads\\musik.mp3',
                               width=-1)
            dpg.add_spacer(height=8)
            with dpg.group(horizontal=True):
                dpg.add_button(label='PILIH DARI WINDOWS', tag='btn_browse_music',
                               width=235, height=43,
                               callback=self.browse_music_windows)
                dpg.bind_item_theme('btn_browse_music','theme_music')
                dpg.add_button(label='GUNAKAN MUSIK', tag='btn_use_music',
                               width=205, height=43, callback=self.use_music)
                dpg.bind_item_theme('btn_use_music','theme_measure')
                dpg.add_button(label='BATAL', width=100, height=43,
                               callback=lambda *args: dpg.hide_item('music_dialog'))
            dpg.add_separator()
            dpg.add_text('Belum ada file dipilih.',
                         tag='music_picker_status', color=MUTED, wrap=735)
        dpg.bind_item_theme('music_dialog','theme_music_dialog')
        with dpg.window(tag='confirm_missing_foot',label='Pijakan Tidak Terhubung',
                        modal=True,show=False,no_resize=True,width=670,height=190):
            dpg.add_text('Sensor pijakan (Serial COM) belum terhubung.',color=ORANGE)
            dpg.add_text('Pengukuran BP dan relaksasi tetap dapat berjalan tanpa data pijakan.')
            dpg.add_text('File CSV pijakan akan kosong selama tidak ada sampel yang diterima.',
                         color=MUTED)
            dpg.add_spacer(height=12)
            with dpg.group(horizontal=True):
                dpg.add_button(label='LANJUTKAN TANPA PIJAKAN',width=285,height=40,
                               callback=self.start_relaxation_confirmed)
                dpg.add_button(label='BATAL / SAMBUNG COM',width=240,height=40,
                               callback=lambda *args:dpg.hide_item('confirm_missing_foot'))
        dpg.bind_item_theme('confirm_missing_foot','theme_music_dialog')
        with dpg.window(tag='bp_progress',label='Proses Pengukuran BP',
                        show=False,modal=False,no_resize=True,no_move=False,
                        width=535,height=170,no_close=True):
            dpg.add_text('PERSIAPAN',tag='bp_progress_title',color=DARK_GREEN)
            dpg.add_spacer(height=8)
            dpg.add_text('Menunggu respons perangkat BP...',tag='bp_progress_detail',
                         color=MUTED,wrap=500)
            dpg.add_spacer(height=12)
            dpg.add_text('Jangan melepas manset selama pengukuran.',color=ORANGE)
        dpg.bind_item_theme('bp_progress','theme_music_dialog')
        with dpg.window(tag='confirm_close',label='Konfirmasi Keluar',modal=True,
                        show=False,no_resize=True,width=705,height=200):
            with dpg.group(tag='close_normal_group'):
                dpg.add_text('Sesi masih berlangsung. Simpan hanya jika BP AWAL dan AKHIR lengkap.')
                dpg.add_text('Jika perangkat bermasalah, gunakan KELUAR DARURAT (tanpa ringkasan).',
                             color=ORANGE)
                dpg.add_spacer(height=14)
                with dpg.group(horizontal=True):
                    dpg.add_button(label='SIMPAN & TUTUP',width=205,
                                   callback=self.confirm_close)
                    dpg.add_button(label='KELUAR DARURAT',width=205,
                                   callback=self.show_emergency_confirmation)
                    dpg.add_button(label='BATAL',width=120,
                                   callback=lambda *args:dpg.hide_item('confirm_close'))
            with dpg.group(tag='close_emergency_group',show=False):
                dpg.add_text('KELUAR DARURAT: data pijakan yang sudah terekam dipertahankan.',
                             color=RED)
                dpg.add_text('Pengukuran BP boleh belum selesai; tidak akan dipaksa menunggu.',
                             color=MUTED)
                dpg.add_text('Sesi ditandai BELUM SELESAI, tanpa ringkasan palsu.',color=ORANGE)
                dpg.add_spacer(height=9)
                with dpg.group(horizontal=True):
                    dpg.add_button(label='YA, KELUAR DARURAT',width=240,
                                   callback=self.emergency_close)
                    dpg.add_button(label='KEMBALI',width=140,
                                   callback=self.cancel_emergency_confirmation)
        dpg.bind_item_theme('confirm_close','theme_music_dialog')
        dpg.set_primary_window('main',True)
        dpg.setup_dearpygui()
        dpg.show_viewport()
        self.refresh_ports()
        self.refresh_music()
        self.layout(force=True)
        self.update_buttons()




    def layout(self,force=False):
        width=max(850,dpg.get_viewport_width())
        height=max(650,dpg.get_viewport_height())
        key=(width,height)
        if not force and self.last_layout_size==key:
            return
        self.last_layout_size=key
        dpg.configure_item('main',width=width,height=height,
                           no_scrollbar=height>=900)
        # Alignment is relative to the header card, not the whole viewport.
        dpg.set_item_pos('btn_close', [max(10,width-235), 11])
        dpg.set_item_pos('clock', [max(10,width-430), 21])
        # Keep colored instructions inside the header, below device status.
        dpg.set_item_pos('activity_line', [18, 101])
        content_width=width-44
        button_width=max(150,(content_width-30)//4)
        for tag in ('btn_start','btn_pause','btn_finish','btn_music'):
            dpg.configure_item(tag,width=button_width)
        extra=max(0,width-1380)
        dpg.configure_item('patient_name',width=235+extra//3)
        dpg.configure_item('patient_id',width=190+extra//3)
        dpg.configure_item('connection_spacer',width=max(8,width-1030))
        # Roomier cards and result strip, with a compact control bar and logo footer.
        toolbar_h=174
        footer_h=54 if height>=850 else 44
        comparison_h=157 if height>=850 else 143
        available=height-toolbar_h-footer_h-comparison_h-205
        # A 520px panel on Full HD; smaller screens use a compact mode.
        panel_h=max(360,min(520,available))
        panel_w=max(255,(width-52)//3)
        dpg.configure_item('toolbar',height=toolbar_h)
        for panel in ('panel_foot','panel_awal','panel_akhir'):
            dpg.configure_item(panel,width=panel_w,height=panel_h)
        dpg.configure_item('panel_comparison',height=comparison_h)
        dpg.configure_item('footer_logos',height=footer_h)
        big_w=max(105,(panel_w-36)//2)
        small_w=max(73,(panel_w-46)//3)
        for foot_tag in ('foot_level','foot_avg','foot_count'):
            dpg.configure_item('foot_box_'+foot_tag, width=small_w)
        for target in ('awal','akhir'):
            for metric in ('sys','dia'):
                dpg.configure_item(f'bp_box_{target}_{metric}',width=big_w)
            for metric in ('pulse','pp','map'):
                dpg.configure_item(f'metric_box_{target}_{metric}',width=small_w)
        # On smaller displays, shorten internal tiles instead of clipping
        # measurement controls or hiding Sesi/Waktu at the bottom.
        compact=panel_h<450
        for target in ('awal','akhir'):
            for metric in ('sys','dia'):
                dpg.configure_item(f'bp_box_{target}_{metric}',
                                   height=110 if compact else 132)
            for metric in ('pulse','pp','map'):
                dpg.configure_item(f'metric_box_{target}_{metric}',
                                   height=73 if compact else 89)
            dpg.configure_item(f'btn_{target}',height=50 if compact else 60)
            dpg.configure_item(f'{target}_detail',show=not compact)

        delta_w=max(110,(width-65)//5)
        for metric in ('sys','dia','pulse','pp','map'):
            dpg.configure_item(f'delta_box_{metric}',width=delta_w)
        # Scale chart to actual remaining space, including axis and card padding.
        chart_h=max(80,int(max(95,min(270,panel_h-225))*0.85))
        dpg.configure_item('foot_plot',height=chart_h)
        if self.logos:
            max_img_h=max(24,footer_h-18)
            max_img_w=max(55,(width-55)//len(self.logos)-12)
            for i,(_,w,h,_) in enumerate(self.logos):
                scale=min(max_img_w/w,max_img_h/h)
                dpg.configure_item(f'logo_img_{i}',
                                   width=max(1,int(w*scale)),
                                   height=max(1,int(h*scale)))

    def log(self, message):
        stamp=datetime.now().strftime('%H:%M:%S')
        text=f'{stamp}  |  {message}'
        self.messages.append(text)
        self.messages=self.messages[-60:]
        dpg.set_value('activity_line',text)

    def refresh_ports(self,*_):
        names=[p.device for p in list_ports.comports()] if list_ports else []
        old=dpg.get_value('port_combo')
        dpg.configure_item('port_combo',items=['Pilih COM']+names)
        if old not in names:
            dpg.set_value('port_combo','Pilih COM')

    def toggle_serial(self,*_):
        if self.session_state not in ('IDLE','PAUSED') or self.pending_target is not None:
            self.log('Port Pijakan hanya dapat diubah ketika IDLE atau sesi DIJEDA')
            return
        if self.demo:
            self.log('Mode simulasi: serial Arduino tidak digunakan')
            return
        if not self.foot_worker:
            self.log('PySerial tidak tersedia')
            return
        if self.foot_worker.selected_port:
            self.foot_worker.set_port(None)
            self.log('Koneksi serial diminta berhenti')
            self.update_buttons()
        else:
            port=dpg.get_value('port_combo')
            if port=='Pilih COM' or not port:
                self.log('Pilih COM Arduino pijakan dahulu')
                return
            self.foot_worker.set_port(port)
            self.log(f'Menghubungkan Arduino pijakan melalui {port}')
            self.update_buttons()

    def update_foot(self,value:int):
        self.last_foot=value
        self.last_foot_event=time.monotonic()
        self.foot_tick += 1
        self.foot_history_x.append(self.foot_tick)
        self.foot_history_y.append(value)
        if len(self.foot_history_x) > 240:
            self.foot_history_x.pop(0)
            self.foot_history_y.pop(0)
        dpg.set_value('foot_series',[self.foot_history_x,self.foot_history_y])
        dpg.set_axis_limits('foot_axis_x',max(0,self.foot_tick-60),
                            max(60,self.foot_tick))
        dpg.set_value('foot_level',f'{value}%')
        dpg.set_value('foot_progress', value / 100.0)
        dpg.configure_item('foot_progress', overlay=f'{value}%')
        if self.session_state=='RUNNING' and self.foot_writer:
            now=time.monotonic()
            if now-self.last_foot_logged>=FOOT_LOG_MIN_PERIOD:
                self.last_foot_logged=now
                self.foot_count+=1
                self.foot_sum+=value
                self.foot_min=value if self.foot_min is None else min(self.foot_min,value)
                self.foot_max=value if self.foot_max is None else max(self.foot_max,value)
                try:
                    self.foot_writer.writerow([
                        datetime.now().isoformat(timespec='milliseconds'),
                        round(self.elapsed_seconds(),2),self.session_user_id,
                        self.session_name,value,'RUNNING'
                    ])
                    self.foot_file.flush()
                except OSError as exc:
                    self.log(f'Gagal tulis CSV pijakan: {exc}')
        dpg.set_value('foot_avg',f'{self.foot_sum/self.foot_count:.1f}%' if self.foot_count else '--')
        dpg.set_value('foot_count',str(self.foot_count))

    def elapsed_seconds(self):
        elapsed=self.elapsed_before_pause
        if self.session_state=='RUNNING' and self.running_since is not None:
            elapsed+=time.monotonic()-self.running_since
        return max(0,elapsed)

    @staticmethod
    def elapsed_fmt(seconds):
        seconds=int(seconds)
        return f'{seconds//3600:02}:{seconds%3600//60:02}:{seconds%60:02}'



    def refresh_music(self, force=False):
        # Auto-discovery: recognizes MP3 in music/ under any filename.
        if force or not self.music_path or not self.music_path.is_file():
            self.music_path = remembered_music()
        if self.music_path and self.music_path.is_file():
            dpg.set_value('music_label', self.music_path.name)
            return True
        dpg.set_value('music_label', 'Belum ada lagu - klik PILIH MUSIK')
        return False

    def scan_music_folder(self, *_):
        # Discover all local audio files, including music/ subfolders.
        found = []
        try:
            found = sorted(
                (p.resolve() for p in MUSIC_DIR.rglob('*')
                 if p.is_file() and p.suffix.lower() in MUSIC_EXTENSIONS),
                key=lambda p: (p.suffix.lower() != '.mp3', str(p).casefold())
            )
        except OSError as exc:
            self.log('Tidak bisa mengakses folder musik: '+str(exc))
        self.music_choices = {}
        for p in found:
            name = str(p.relative_to(MUSIC_DIR))
            self.music_choices[name] = p
        names = list(self.music_choices)
        if names:
            dpg.configure_item('music_file_choices', items=names)
            selected_name = next(
                (n for n,p in self.music_choices.items()
                 if self.music_path and p == self.music_path.resolve()),
                names[0]
            )
            dpg.set_value('music_file_choices', selected_name)
            if not dpg.get_value('music_path_entry'):
                dpg.set_value('music_path_entry', str(self.music_choices[selected_name]))
            dpg.set_value('music_picker_status',
                          f'Ditemukan {len(names)} file audio di folder music.')
        else:
            dpg.configure_item('music_file_choices',
                               items=['Tidak ada lagu di folder music'])
            dpg.set_value('music_file_choices','Tidak ada lagu di folder music')
            dpg.set_value('music_picker_status',
                          'Folder music belum berisi MP3/WAV/OGG. '
                          'Pilih lagu dari Windows atau tempel lokasi file lengkap.')

    def music_dropdown_changed(self, sender, app_data, user_data=None):
        selected = self.music_choices.get(app_data)
        if selected:
            dpg.set_value('music_path_entry', str(selected))
            dpg.set_value('music_picker_status', selected.name)

    def open_music_picker(self, *_):
        if not self.workflow().can_change_music:
            self.log('Musik hanya bisa diganti saat IDLE')
            return
        self.refresh_music()
        dpg.set_value('music_path_entry',
                      str(self.music_path) if self.music_path else '')
        self.scan_music_folder()
        dpg.show_item('music_dialog')

    def browse_music_windows(self, *_):
        # A normal Windows Open File dialog, rather than DearPyGui's dark
        # built-in file dialog. Manual path entry remains available as fallback.
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes('-topmost', True)
            try:
                initial = self.music_path.parent if self.music_path and self.music_path.is_file() else MUSIC_DIR
                file_path = filedialog.askopenfilename(
                    parent=root,
                    title='Pilih musik relaksasi KursiMu',
                    initialdir=str(initial),
                    filetypes=[
                        ('Audio MP3 / WAV / OGG', '*.mp3 *.wav *.ogg'),
                        ('MP3', '*.mp3'),
                        ('WAV', '*.wav'),
                        ('OGG', '*.ogg'),
                        ('Semua file', '*.*'),
                    ]
                )
            finally:
                root.destroy()
            if file_path:
                dpg.set_value('music_path_entry', file_path)
                dpg.set_value('music_picker_status',
                              'Terpilih: '+Path(file_path).name+'. Klik GUNAKAN MUSIK.')
        except Exception as exc:
            self.log('File picker Windows tidak tersedia: '+str(exc))
            dpg.set_value('music_picker_status',
                          'Tidak bisa membuka penjelajah Windows. '
                          'Tempel lokasi lengkap file MP3 pada kolom di atas.')

    def use_music(self, *_):
        if not self.workflow().can_change_music:
            self.log('Musik tidak dapat diubah saat sesi berlangsung')
            dpg.hide_item('music_dialog')
            return
        filename = (dpg.get_value('music_path_entry') or '').strip().strip('"')
        if not filename:
            dpg.set_value('music_picker_status',
                          'Pilih dari daftar, gunakan PILIH DARI WINDOWS, '
                          'atau tempel lokasi file MP3.')
            return
        candidate = Path(filename).expanduser()
        if not candidate.is_absolute():
            candidate = MUSIC_DIR / candidate
        if candidate.suffix.lower() not in MUSIC_EXTENSIONS:
            dpg.set_value('music_picker_status',
                          'Format tidak didukung. Pilih file MP3, WAV, atau OGG.')
            return
        try:
            candidate = candidate.resolve(strict=True)
            if not candidate.is_file():
                raise OSError('Lokasi tidak menunjuk ke file.')
            with candidate.open('rb') as f:
                if not f.read(64):
                    raise OSError('File kosong.')
        except (OSError, ValueError) as exc:
            self.log('Musik tidak terbaca: '+str(exc))
            dpg.set_value('music_picker_status',
                          'Gagal membaca file: '+str(exc))
            return
        self.music_path = candidate
        dpg.set_value('music_label', candidate.name)
        try:
            save_music_choice(candidate)
        except OSError as exc:
            self.log('Pilihan lagu tidak tersimpan untuk nanti: '+str(exc))
        self.log('Musik dipilih: '+candidate.name)
        dpg.hide_item('music_dialog')

    def init_audio(self):
        if pygame is None:
            self.audio_error='pygame-ce belum terpasang (pip install pygame-ce)'
            return False
        try:
            if not pygame.mixer.get_init():
                pygame.mixer.init()
            self.audio_ready=True
            return True
        except Exception as exc:
            self.audio_error=str(exc)
            self.audio_ready=False
            return False


    def start_music(self):
        # Scan again because the user may have added MP3 after opening the GUI.
        self.refresh_music()
        if not self.music_path or not self.music_path.is_file():
            self.log('Musik tidak ditemukan. Letakkan MP3 di: '+str(MUSIC_DIR))
            if self.demo:
                self.log('[DEMO] Sesi simulasi berjalan tanpa musik')
                return True
            return False
        if not self.init_audio():
            self.log('Audio belum siap: '+self.audio_error)
            return False
        try:
            self.stop_music()
            pygame.mixer.music.load(str(self.music_path))
            pygame.mixer.music.set_volume(0.55)
            pygame.mixer.music.play(loops=-1)
            self.audio_playing=True
            self.log('Musik diputar: '+self.music_path.name)
            return True
        except Exception as exc:
            self.audio_playing=False
            self.audio_error=str(exc)
            self.log('Gagal membuka MP3: '+str(exc)+' | '+str(self.music_path))
            return False

    def stop_music(self):
        if pygame is not None and self.audio_ready:
            try:
                pygame.mixer.music.stop()
            except Exception:
                pass
        self.audio_playing=False

    def begin_relaxation(self,*_):
        if self.session_state!='IDLE':
            self.log('Sesi sudah dibuat. Gunakan LANJUTKAN atau SESI BARU.')
            return
        if not self.workflow().can_start:
            self.log('MULAI ditolak: '+self.workflow().guidance)
            return
        if not self.demo and not self.serial_connected:
            dpg.show_item('confirm_missing_foot')
            self.log('Pijakan tidak terhubung; menunggu konfirmasi untuk memulai tanpa pijakan')
            return
        self.start_relaxation_confirmed()

    def start_relaxation_confirmed(self,*_):
        dpg.hide_item('confirm_missing_foot')
        if self.session_state!='IDLE' or not self.workflow().can_start:
            self.log('MULAI dibatalkan: prasyarat BP/sesi berubah')
            return
        name,user_id=self.baseline_identity
        if not self.start_music():
            return
        # Generate and open streaming CSV. If this fails, do not leave music playing.
        uid=datetime.now().strftime('%Y%m%d_%H%M%S')+'_'+secrets.token_hex(3)
        ident=re.sub(r'[^a-zA-Z0-9_-]+','_',user_id)[:35] or 'id'
        folder=SESSION_DIR / datetime.now().strftime('%Y-%m-%d')
        file=folder / f'{uid}_{ident}_pijakan.csv'
        try:
            folder.mkdir(parents=True,exist_ok=True)
            fd=file.open('w',newline='',encoding='utf-8-sig')
            writer=csv.writer(fd)
            writer.writerow(['timestamp','elapsed_active_s','id_pengguna',
                             'nama_pengguna','pijakan_level_0_100','status_sesi'])
            fd.flush()
        except OSError as exc:
            self.stop_music()
            self.log(f'Gagal memulai CSV: {exc}')
            return
        self.foot_file=fd
        self.foot_writer=writer
        self.session_uid=uid
        self.session_name=name
        self.session_user_id=user_id
        self.session_started=datetime.now()
        self.session_files={'pijakan':file,'ringkasan':file.with_name(f'{uid}_{ident}_ringkasan.csv')}
        self.elapsed_before_pause=0.0
        self.running_since=time.monotonic()
        self.session_state='RUNNING'
        self.foot_count=0
        self.foot_sum=0
        self.foot_min=None
        self.foot_max=None
        self.last_foot_logged=0
        self.update_buttons()
        self.log('Sesi relaksasi dimulai: musik dan logging pijakan aktif')
        self.update_buttons()


    def pause_relaxation(self,*_):
        d=self.workflow()
        if self.session_state=='RUNNING':
            if not d.can_pause:
                self.log('PAUSE ditolak: sesi belum berjalan')
                return
            self.elapsed_before_pause=self.elapsed_seconds()
            self.running_since=None
            self.session_state='PAUSED'
            if self.audio_ready:
                try:
                    pygame.mixer.music.pause()
                except Exception as exc:
                    self.log('Musik gagal dijeda: '+str(exc))
            self.log('Sesi dijeda: musik dan pencatatan pijakan berhenti sementara')
        elif self.session_state=='PAUSED':
            if not d.can_resume:
                self.log('LANJUTKAN ditolak: hubungkan BP KursiMu dahulu')
                return
            if self.audio_ready:
                try:
                    pygame.mixer.music.unpause()
                except Exception as exc:
                    self.log('Musik gagal dilanjutkan: '+str(exc))
                    return
            self.running_since=time.monotonic()
            self.session_state='RUNNING'
            self.log('Sesi dilanjutkan: musik dan pencatatan pijakan aktif')
        else:
            self.log('PAUSE hanya tersedia saat relaksasi berlangsung')
        self.update_buttons()

    def pause_on_disconnect(self, device):
        if self.demo or self.session_state!='RUNNING':
            return
        self.elapsed_before_pause=self.elapsed_seconds()
        self.running_since=None
        self.session_state='PAUSED'
        if self.audio_ready:
            try:
                pygame.mixer.music.pause()
            except Exception:
                pass
        self.update_buttons()
        self.log(device+' terputus: sesi otomatis dijeda sampai koneksi pulih')


    def finish_relaxation(self,*_):
        if not self.workflow().can_finish:
            self.log('SELESAI ditolak: hasil AWAL dan AKHIR harus lengkap')
            return False
        if self.session_state == 'SAVE_FAILED':
            ok=self.write_summary()
            self.session_state='FINISHED' if ok else 'SAVE_FAILED'
            self.update_buttons()
            self.log('Ringkasan berhasil disimpan' if ok else 'Masih gagal simpan. Periksa disk.')
            return ok

        duration=self.elapsed_seconds()
        self.stop_music()
        self.elapsed_before_pause=duration
        self.running_since=None
        self.session_finished=datetime.now()
        if self.foot_file:
            try:
                self.foot_file.flush()
                os.fsync(self.foot_file.fileno())
            finally:
                self.foot_file.close()
                self.foot_file=None
                self.foot_writer=None
        ok=self.write_summary()
        self.session_state='FINISHED' if ok else 'SAVE_FAILED'
        self.update_buttons()
        if ok:
            self.log(f'Sesi lengkap tersimpan: {self.session_files["ringkasan"].name}')
        else:
            self.log('CSV pijakan tersimpan, ringkasan gagal. Klik SELESAI untuk mencoba lagi.')
        return ok

    def write_summary(self):
        """One summary row per relaxation session, never silently invent BP readings."""
        row={
            'session_uid':self.session_uid,
            'id_pengguna':self.session_user_id,
            'nama_pengguna':self.session_name,
            'waktu_mulai':self.session_started.isoformat(timespec='seconds') if self.session_started else '',
            'waktu_selesai':self.session_finished.isoformat(timespec='seconds') if self.session_finished else '',
            'durasi_aktif_detik':round(self.elapsed_before_pause,1),
            'mode':'SIMULASI' if self.demo else 'LIVE',
            'musik':self.music_path.name if self.music_path else '',
            'pijakan_samples':self.foot_count,
            'pijakan_rata_rata':round(self.foot_sum/self.foot_count,2) if self.foot_count else '',
            'pijakan_min':self.foot_min if self.foot_min is not None else '',
            'pijakan_max':self.foot_max if self.foot_max is not None else '',
            'file_pijakan':self.session_files.get('pijakan',Path('')).name,
        }
        keys=('session','timestamp','sys','dia','pulse','pp','map')
        for target in ('awal','akhir'):
            result=self.readings.get(target) or {}
            for key in keys:
                row[f'{target}_{key}']=result.get(key,'')
        for key in ('sys','dia','pulse','pp','map'):
            awal=self.readings['awal'] or {}
            akhir=self.readings['akhir'] or {}
            row[f'delta_{key}']=(round(akhir[key]-awal[key],2)
                                  if key in awal and key in akhir else '')
        try:
            output=self.session_files['ringkasan']
            with output.open('w',encoding='utf-8-sig',newline='') as stream:
                writer=csv.DictWriter(stream,fieldnames=list(row))
                writer.writeheader()
                writer.writerow(row)
                stream.flush()
                os.fsync(stream.fileno())
            return True
        except OSError as exc:
            self.log(f'Ringkasan CSV gagal: {exc}')
            return False


    def on_identity_changed(self,*_):
        self.update_buttons()

    def workflow(self):
        name=(dpg.get_value('patient_name') or '').strip()
        user_id=(dpg.get_value('patient_id') or '').strip()
        identity_ready=bool(name and user_id)
        if self.baseline_identity is not None:
            identity_ready=identity_ready and (name,user_id)==self.baseline_identity
        bp_busy=bool(not self.demo and self.ble_connected and self.bp_status and
                     (self.bp_status['state'] in (1,2,3,4) or self.bp_status['pending']))
        return evaluate_session(
            state=self.session_state,
            bp_connected=self.ble_connected,
            foot_connected=self.serial_connected,
            bp_ready=self.demo or self.bp_status is not None,
            identity_ready=identity_ready,
            initial_ready=self.readings['awal'] is not None,
            final_ready=self.readings['akhir'] is not None,
            measuring=self.pending_target is not None,
            bp_busy=bp_busy,
            music_ready=self.demo or bool(self.music_path and self.music_path.is_file()),
        )

    def update_buttons(self):
        d=self.workflow()
        dpg.configure_item('btn_awal',enabled=d.can_measure_awal)
        dpg.configure_item('btn_akhir',enabled=d.can_measure_akhir)
        dpg.configure_item('btn_start',enabled=d.can_start)
        dpg.configure_item('btn_pause',enabled=d.can_pause or d.can_resume,
                           label='▶  LANJUTKAN' if self.session_state=='PAUSED' else '⏸  PAUSE')
        dpg.configure_item('btn_finish',enabled=d.can_finish)
        dpg.configure_item('btn_new',enabled=d.can_new)
        dpg.configure_item('btn_music',enabled=d.can_change_music)
        dpg.configure_item('patient_name',enabled=d.can_edit_identity)
        dpg.configure_item('patient_id',enabled=d.can_edit_identity)
        can_change_port=self.session_state in ('IDLE','PAUSED') and self.pending_target is None
        for tag in ('port_combo','btn_connect','btn_refresh'):
            dpg.configure_item(tag,enabled=can_change_port)
        # Show the actual requested and connected Arduino COM state.
        connecting=bool(self.foot_worker and self.foot_worker.selected_port)
        if self.demo:
            com_label='⊕  SIMULASI COM'
        elif self.serial_connected:
            com_label='⊗  PUTUS COM'
        elif connecting:
            com_label='⊗  BATAL COM'
        else:
            com_label='⊕  SAMBUNG COM'
        dpg.configure_item('btn_connect',label=com_label,
                           enabled=can_change_port and not self.demo)
        dpg.bind_item_theme('btn_connect',
                            'theme_disconnect' if connecting else 'theme_connect')
        color={'red':RED,'yellow':ORANGE,'green':GREEN}[d.color]
        dpg.set_value('session_state',d.phase)
        dpg.configure_item('session_state',color=color)
        dpg.set_value('activity_line',d.guidance)
        dpg.configure_item('activity_line',color=color)


    def show_bp_progress(self, stage, detail='', auto_close_ms=None):
        # Informational non-modal window: never blocks BLE recovery or exit controls.
        self.bp_popup_stage=stage
        dpg.set_value('bp_progress_title',stage)
        dpg.set_value('bp_progress_detail',detail)
        dpg.show_item('bp_progress')
        self.bp_popup_hide_at=(time.monotonic()+auto_close_ms/1000.0
                               if auto_close_ms is not None else None)

    def dismiss_bp_progress(self):
        dpg.hide_item('bp_progress')
        self.bp_popup_hide_at=None
        self.bp_popup_stage=None

    def update_bp_progress(self):
        if self.bp_popup_hide_at is not None and time.monotonic()>=self.bp_popup_hide_at:
            self.dismiss_bp_progress()

    def start_bp(self,target):
        d=self.workflow()
        allowed=d.can_measure_awal if target=='awal' else d.can_measure_akhir
        if not allowed:
            self.log('UKUR '+target.upper()+' ditolak: '+d.guidance)
            return
        if target=='awal':
            # Freeze identity before controlling the BP button.
            self.baseline_identity=(
                (dpg.get_value('patient_name') or '').strip(),
                (dpg.get_value('patient_id') or '').strip()
            )
        if self.demo:
            self.pending_target={'target':target,'baseline':self.demo_session,'accepted':True}
            self.demo_target=target
            self.demo_due=time.monotonic()+5.2
            self.show_bp_progress('PERSIAPAN','Menyiapkan pengukuran BP '+target.upper()+'...')
            self.update_buttons()
            self.log('[DEMO] Mengukur BP '+target.upper())
            return
        self.pending_target={'target':target,
                             'baseline':self.bp_status['total'],
                             'accepted':False}
        # Once per click. No automatic retry or START on BLE reconnect.
        self.show_bp_progress('PERSIAPAN','Mengirim perintah ukur BP '+target.upper()+'...')
        self.ble_requests.put({'op':OP_START,'target':target})
        self.update_buttons()
        self.log('START BP '+target.upper()+' sekali, menunggu pompa')

    def unlock_failed_awal(self):
        if self.session_state=='IDLE' and self.readings['awal'] is None:
            if self.pending_target is None:
                self.baseline_identity=None
                self.update_buttons()

    def set_panel(self,target,result):
        self.readings[target]=dict(result)
        dpg.set_value(f'{target}_sys',str(result['sys']))
        dpg.set_value(f'{target}_dia',str(result['dia']))
        dpg.set_value(f'{target}_pulse',str(result['pulse']))
        dpg.set_value(f'{target}_pp',str(result['pp']))
        dpg.set_value(f'{target}_map',f'{result["map"]:.1f}')
        dpg.set_value(f'{target}_session',str(result['session']))
        dpg.set_value(f'{target}_time',result['timestamp'][11:16])
        dpg.set_value(f'{target}_detail',f'Direkam {result["timestamp"].replace("T"," ")}')
        self.update_deltas()
        self.log(f'BP {target.upper()}: {result["sys"]}/{result["dia"]} mmHg; PP {result["pp"]}; MAP {result["map"]}')

    def update_deltas(self):
        awal=self.readings.get('awal')
        akhir=self.readings.get('akhir')
        dpg.set_value('comparison_hint',
                      'AKHIR - AWAL' if awal and akhir
                      else 'Menunggu hasil AWAL dan AKHIR')
        for metric in ('sys','dia','pulse','pp','map'):
            tag=f'delta_{metric}'
            if awal is None or akhir is None:
                dpg.set_value(tag,'--')
                continue
            change=round(akhir[metric]-awal[metric],1)
            value=f'{change:+g}'
            dpg.set_value(tag,value)
            # Parameter color is informational, never a clinical risk classification.
            accent={'sys': MED_BLUE, 'dia': MED_TEAL, 'pulse': MED_BLUE,
                    'pp': MED_AMBER, 'map': MED_PURPLE}[metric]
            dpg.configure_item(tag,color=accent)

    def handle_result(self,result):
        assigned=False
        if (self.pending_target and self.pending_target['accepted']
                and result['session']>self.pending_target['baseline']
                and self.readings[self.pending_target['target']] is None):
            target=self.pending_target['target']
            self.set_panel(target,result)
            self.pending_target=None
            self.show_bp_progress('PENGUKURAN SELESAI','Hasil BP '+target.upper()+' berhasil diterima.',auto_close_ms=1300)
            assigned=True
        if not assigned:
            self.unassigned=result
            dpg.set_value('unassigned_label',f'#{result["session"]} {result["sys"]}/{result["dia"]}')
            dpg.configure_item('assign_awal',show=True)
            dpg.configure_item('assign_akhir',show=True)
            self.log(f'Recovery hasil BP #{result["session"]}; perlu pilih AWAL/AKHIR')
        self.update_buttons()


    def assign_unassigned(self,target):
        if self.unassigned is None:
            return
        d=self.workflow()
        allowed=d.can_measure_awal if target=='awal' else d.can_measure_akhir
        if not allowed:
            self.log('Hasil BP belum dapat dipasangkan: '+d.guidance)
            return
        if target=='awal':
            self.baseline_identity=(
                (dpg.get_value('patient_name') or '').strip(),
                (dpg.get_value('patient_id') or '').strip()
            )
        self.set_panel(target,self.unassigned)
        self.unassigned=None
        dpg.set_value('unassigned_label','Tidak ada')
        dpg.configure_item('assign_awal',show=False)
        dpg.configure_item('assign_akhir',show=False)
        self.update_buttons()

    def new_session(self,*_):
        if self.session_state!='FINISHED':
            self.log('SESI BARU hanya dapat dilakukan setelah hasil lengkap tersimpan')
            return
        if self.pending_target:
            self.log('Pengukuran BP masih berlangsung')
            return
        self.readings={'awal':None,'akhir':None}
        self.baseline_identity=None
        self.unassigned=None
        self.pending_target=None
        self.session_state='IDLE'
        self.foot_count=0
        self.foot_sum=0.0
        self.foot_min=self.foot_max=None
        self.session_uid=None
        self.session_files={}
        self.session_started=None
        self.session_finished=None
        self.elapsed_before_pause=0.0
        self.running_since=None
        dpg.set_value('session_state','BELUM DIMULAI')
        dpg.configure_item('session_state',color=ORANGE)
        dpg.set_value('unassigned_label','Tidak ada')
        dpg.configure_item('assign_awal',show=False)
        dpg.configure_item('assign_akhir',show=False)
        dpg.set_value('elapsed','00:00:00')
        dpg.set_value('patient_name','')
        dpg.set_value('patient_id','')
        for target in ('awal','akhir'):
            dpg.set_value(f'{target}_sys','--')
            dpg.set_value(f'{target}_dia','--')
            for field in ('pulse','pp','map','session','time'):
                dpg.set_value(f'{target}_{field}','--')
            dpg.set_value(f'{target}_detail','Belum ada pengukuran')
        self.foot_history_x.clear()
        self.foot_history_y.clear()
        self.foot_tick=0
        dpg.set_value('foot_series',[[],[]])
        dpg.set_value('foot_avg','--')
        dpg.set_value('foot_count','0')
        self.update_deltas()
        self.update_foot(self.last_foot)
        self.update_buttons()
        self.log('Sesi baru siap; hasil sesi sebelumnya tersimpan')

    def process_event(self,item):
        typ=item.get('type')
        if typ=='log':
            self.log(item['message'])
        elif typ=='ble':
            self.ble_connected=item['connected']
            dpg.set_value('ble_state',item['text'])
            dpg.configure_item('ble_state',color=GREEN if self.ble_connected else ORANGE)
            if not self.ble_connected:
                self.bp_status=None
                dpg.set_value('bp_state','TIDAK TERHUBUNG')
                self.pause_on_disconnect('BP KursiMu')
                if self.pending_target:
                    self.show_bp_progress('KONEKSI TERPUTUS','Menunggu koneksi BP kembali.',auto_close_ms=4000)
            self.update_buttons()
        elif typ=='serial':
            self.serial_connected=item['connected']
            dpg.set_value('serial_state',item['text'])
            dpg.configure_item('serial_state',color=GREEN if self.serial_connected else ORANGE)
            # Foot COM is an optional data stream: its loss must not pause music
            # or the BP session. No fabricated foot samples are recorded.
            if not self.serial_connected and self.session_state=='RUNNING':
                self.log('Pijakan terputus: relaksasi tetap berlangsung tanpa data pijakan')
            self.update_buttons()
        elif typ=='foot':
            self.update_foot(item['level'])
        elif typ=='status':
            previous=self.bp_status
            self.bp_status=item['value']
            code=self.bp_status['state']
            state=STATE_NAMES[code] if code<len(STATE_NAMES) else str(code)
            dpg.set_value('bp_state',state)
            dpg.configure_item('bp_state',color=RED if code==6 else GREEN if code in (0,5) else ORANGE)
            if self.pending_target and code in (1,2,3,4):
                if code==1:
                    self.show_bp_progress('PERSIAPAN','Perintah diterima. Menunggu pompa BP.')
                elif code==2:
                    self.show_bp_progress('POMPA DIMULAI','Pengukuran tekanan darah sedang berlangsung.')
                else:
                    self.show_bp_progress('ANALISA TEKANAN DARAH','Menunggu hasil pembacaan dari alat BP.')
            if self.pending_target and self.pending_target['accepted'] and code==6:
                self.show_bp_progress('PENGUKURAN GAGAL','Alat melaporkan error atau timeout.',auto_close_ms=4500)
                self.log('BP gagal atau timeout. Hasil tidak dipasangkan')
                self.pending_target=None
                self.unlock_failed_awal()
            if previous is None or previous['state']!=code:
                self.log(f'BP {state}, total #{self.bp_status["total"]}')
            self.update_buttons()
        elif typ=='command_ack':
            answer=item['answer']
            code=answer['ack']
            desc=ACK_NAMES[code] if code<len(ACK_NAMES) else str(code)
            if item.get('op')==OP_START and self.pending_target and item.get('target')==self.pending_target['target']:
                if code==0:
                    self.pending_target['accepted']=True
                    self.show_bp_progress('PERSIAPAN','Perintah UKUR diterima. Menunggu pompa BP.')
                    self.log(f'START diterima untuk {item["target"].upper()}')
                else:
                    self.show_bp_progress('PERINTAH DITOLAK',f'BP menolak perintah: {desc}',auto_close_ms=4000)
                    self.log(f'START ditolak: ACK_{desc}')
                    self.pending_target=None
                    self.unlock_failed_awal()
            self.update_buttons()
        elif typ=='command_error':
            self.show_bp_progress('PERINTAH BP BERMASALAH',str(item['message']),auto_close_ms=4500)
            self.log(f'Perintah BP gagal: {item["message"]}')
            if item.get('op')==OP_START:
                # Result may still arrive; never repeat a potentially issued START.
                self.pending_target=None
                self.unlock_failed_awal()
            self.update_buttons()
        elif typ=='result':
            self.handle_result(item['value'])

    def update_demo(self):
        now=time.monotonic()
        if now-self.last_demo_tick>.30:
            self.last_demo_tick=now
            self.update_foot(random.randint(25,85))
        if self.demo_due is not None and self.pending_target:
            remaining=self.demo_due-now
            if remaining<=1.7 and self.bp_popup_stage!='ANALISA TEKANAN DARAH':
                self.show_bp_progress('ANALISA TEKANAN DARAH','[DEMO] Mengolah pembacaan tekanan darah.')
            elif 1.7<remaining<=3.6 and self.bp_popup_stage!='POMPA DIMULAI':
                self.show_bp_progress('POMPA DIMULAI','[DEMO] Pengukuran tekanan darah berlangsung.')
        if self.demo_due is not None and now>=self.demo_due:
            self.demo_due=None
            self.demo_session+=1
            s=random.randint(112,132) if self.demo_target=='awal' else random.randint(106,124)
            d=random.randint(77,91) if self.demo_target=='awal' else random.randint(68,83)
            p=random.randint(65,85)
            result={'session':self.demo_session,'sys':s,'dia':d,'pulse':p,
                    'pp':s-d,'map':round(d+(s-d)/3,1),
                    'timestamp':datetime.now().isoformat(timespec='seconds'),
                    'err':0,'flags':1,'demo':True}
            self.handle_result(result)

    def request_close(self,*_):
        # IDLE (including after SESI BARU) and FINISHED can close immediately.
        # An active session or in-flight BP command requires confirmation.
        if requires_exit_confirmation(self.session_state,
                                      self.pending_target is not None):
            self.dismiss_bp_progress()
            dpg.show_item('close_normal_group')
            dpg.hide_item('close_emergency_group')
            dpg.show_item('confirm_close')
        else:
            dpg.stop_dearpygui()

    def show_emergency_confirmation(self,*_):
        # Keep ONE modal; nested modal stacks can swallow input on Win11.
        dpg.hide_item('close_normal_group')
        dpg.show_item('close_emergency_group')

    def cancel_emergency_confirmation(self,*_):
        dpg.hide_item('close_emergency_group')
        dpg.show_item('close_normal_group')

    def emergency_close(self,*_):
        # Explicit recovery path if a device fails mid-session.
        # Preserve raw foot CSV, but NEVER invent a completed BP summary.
        # Emergency exit must not depend on hardware response, BP ACK,
        # unfinished readings or successful disk writes.
        self.pending_target=None
        self.demo_due=None
        self.dismiss_bp_progress()
        if self.foot_file:
            stream=self.foot_file
            self.foot_file=None
            self.foot_writer=None
            try:
                stream.flush()
                os.fsync(stream.fileno())
            except OSError as exc:
                self.log('Peringatan flush darurat: '+str(exc))
            finally:
                try:
                    stream.close()
                except OSError as exc:
                    self.log('Peringatan close darurat: '+str(exc))
        self.stop_music()
        if self.session_uid and self.session_files:
            marker=self.session_files['pijakan'].with_name(
                self.session_files['pijakan'].stem+'_BELUM_SELESAI.json'
            )
            try:
                with marker.open('w',encoding='utf-8') as output:
                    json.dump({
                        'status':'INCOMPLETE_EMERGENCY_EXIT',
                        'session_uid':self.session_uid,
                        'waktu_keluar':datetime.now().isoformat(timespec='seconds'),
                        'has_awal':self.readings['awal'] is not None,
                        'has_akhir':self.readings['akhir'] is not None,
                    },output,ensure_ascii=False,indent=2)
            except OSError as exc:
                self.log('Penanda sesi belum selesai gagal disimpan: '+str(exc))
        self.session_state='ABORTED'
        dpg.stop_dearpygui()

    def confirm_close(self,*_):
        if self.pending_target:
            self.log('BP masih mengukur. Tunggu selesai sebelum keluar.')
            dpg.hide_item('confirm_close')
            return
        if self.session_state in ('RUNNING','PAUSED','SAVE_FAILED'):
            if not self.finish_relaxation():
                dpg.hide_item('confirm_close')
                return
        dpg.stop_dearpygui()

    def run(self):
        self.create_ui()
        if self.bp_worker:
            self.bp_worker.start()
        if self.foot_worker:
            self.foot_worker.start()
        last_clock=0.0
        last_elapsed=0.0
        try:
            while dpg.is_dearpygui_running():
                for _ in range(120):
                    try:
                        event=self.events.get_nowait()
                    except queue.Empty:
                        break
                    self.process_event(event)
                if self.demo:
                    self.update_demo()
                now=time.monotonic()
                if now-last_clock>8:
                    dpg.set_value('clock',datetime.now().strftime('%d %b %Y  %H:%M'))
                    last_clock=now
                if now-last_elapsed>=0.25:
                    dpg.set_value('elapsed',self.elapsed_fmt(self.elapsed_seconds()))
                    last_elapsed=now
                self.layout()
                self.update_bp_progress()
                dpg.render_dearpygui_frame()
        finally:
            if self.session_state in ('RUNNING','PAUSED'):
                # Only a complete AWAL/AKHIR pair creates the normal summary.
                if self.readings['awal'] and self.readings['akhir'] and not self.pending_target:
                    self.finish_relaxation()
                if self.foot_file:
                    try:
                        self.foot_file.flush()
                        os.fsync(self.foot_file.fileno())
                    finally:
                        self.foot_file.close()
                        self.foot_file=None
            elif self.foot_file:
                self.foot_file.close()
            self.stop_music()
            # Notify background workers before destroying the DearPyGui context.
            # This prevents avoidable WinRT/serial cleanup noise after GUI exit.
            if self.bp_worker:
                self.bp_worker.stop.set()
            if self.foot_worker:
                self.foot_worker.stop.set()
                self.foot_worker.set_port(None)
            for worker in (self.foot_worker, self.bp_worker):
                if worker and worker.thread.is_alive():
                    worker.thread.join(timeout=2.0)
            dpg.destroy_context()


def main():
    parser=argparse.ArgumentParser(description='KursiMu - DearPyGui white/green desktop')
    parser.add_argument('--demo',action='store_true',help='Simulated sensor values (not actual measurements)')
    parser.add_argument('--test-music',action='store_true',
                        help='Diagnose MP3 discovery and decoding without opening the GUI')
    parser.add_argument('--music-file',type=Path,default=None,
                        help='Optional audio file to check with --test-music')
    args=parser.parse_args()
    if args.test_music:
        song=args.music_file.expanduser() if args.music_file else remembered_music()
        print('[MUSIC] Folder:',MUSIC_DIR)
        if song is None or not song.is_file():
            print('[MUSIC] ERROR: MP3/WAV/OGG not found.')
            print('[MUSIC] Files:',[p.name for p in MUSIC_DIR.iterdir()])
            raise SystemExit(1)
        print('[MUSIC] Found:',song.resolve())
        if pygame is None:
            print('[MUSIC] ERROR: pygame-ce is not installed. Run: pip install pygame-ce')
            raise SystemExit(2)
        try:
            pygame.mixer.init()
            pygame.mixer.music.load(str(song.resolve()))
            print('[MUSIC] OK: file recognized and decoded by pygame-ce')
            pygame.mixer.music.unload()
        except Exception as exc:
            print('[MUSIC] Decoder/audio error:',repr(exc))
            raise SystemExit(3)
        finally:
            pygame.mixer.quit()
        return
    KursiMuApp(demo=args.demo).run()


if __name__=='__main__':
    main()
