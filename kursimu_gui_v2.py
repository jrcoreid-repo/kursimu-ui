"""
KursiMu GUI v2.3 - compact three-card interface with dedicated comparison strip.
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
from typing import TextIO

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
        self.unassigned: dict | None = None
        self.readings = {"awal": None, "akhir": None}
        self.last_foot = 0
        self.foot_count = 0
        self.foot_sum = 0.0
        self.foot_min = None
        self.foot_max = None
        self.last_foot_logged = 0.0
        self.last_foot_event = 0.0
        self.session_state = "IDLE"  # IDLE/RUNNING/PAUSED/FINISHED
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
        self.music_path = DEFAULT_MUSIC if DEFAULT_MUSIC.exists() else None
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
            self.fonts['base'] = dpg.add_font(str(font), 16)
            self.fonts['small'] = dpg.add_font(str(font), 14)
            self.fonts['title'] = dpg.add_font(str(bold if bold.exists() else font), 25)
            self.fonts['hero'] = dpg.add_font(str(bold if bold.exists() else font), 40)
            self.fonts['section'] = dpg.add_font(str(bold if bold.exists() else font), 19)
            self.fonts['metric'] = dpg.add_font(str(bold if bold.exists() else font), 23)
        dpg.bind_font(self.fonts['base'])

    def bind_font(self, tag, name):
        if name in self.fonts:
            dpg.bind_item_font(tag, self.fonts[name])

    def create_themes(self):
        with dpg.theme(tag='theme_global'):
            with dpg.theme_component(dpg.mvAll):
                for idx, color in (
                    (dpg.mvThemeCol_WindowBg,BG), (dpg.mvThemeCol_ChildBg,WHITE),
                    (dpg.mvThemeCol_Text,INK), (dpg.mvThemeCol_Border,LINE),
                    (dpg.mvThemeCol_FrameBg,(246,250,247,255)),
                    (dpg.mvThemeCol_FrameBgHovered,PALE),
                    (dpg.mvThemeCol_FrameBgActive,PALE),
                    (dpg.mvThemeCol_Button,GREEN),
                    (dpg.mvThemeCol_ButtonHovered,DARK_GREEN),
                    (dpg.mvThemeCol_ButtonActive,DARK_GREEN),
                    (dpg.mvThemeCol_Header,PALE),
                    (dpg.mvThemeCol_CheckMark,GREEN),
                ):
                    dpg.add_theme_color(idx,color)
                dpg.add_theme_style(dpg.mvStyleVar_WindowPadding,10,8)
                dpg.add_theme_style(dpg.mvStyleVar_FramePadding,8,5)
                dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing,6,5)
                dpg.add_theme_style(dpg.mvStyleVar_FrameRounding,7)
                dpg.add_theme_style(dpg.mvStyleVar_ChildRounding,12)
                dpg.add_theme_style(dpg.mvStyleVar_WindowBorderSize,0)
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
        with dpg.theme(tag='theme_pause'):
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Button,(206,146,42,255))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered,(179,119,22,255))
        with dpg.theme(tag='theme_finish'):
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Button,(188,76,78,255))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered,(155,56,59,255))
                dpg.add_theme_color(dpg.mvThemeCol_Text,WHITE)
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
        with dpg.theme(tag='theme_close'):
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Button,(230,238,232,255))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered,(246,222,220,255))
                dpg.add_theme_color(dpg.mvThemeCol_Text,(137,47,48,255))
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
        with dpg.child_window(tag=f'metric_box_{target}_{field}',
                              width=115, height=96, border=False, no_scrollbar=True):
            dpg.add_text(label, color=MUTED)
            tag = f'{target}_{field}'
            dpg.add_text('--', tag=tag, color=DARK_GREEN)
            self.bind_font(tag, 'metric')
            dpg.add_text(unit, color=MUTED)


    def add_bp_panel(self, target, number, label):
        tag = f'panel_{target}'
        with dpg.child_window(tag=tag, width=400, height=415,
                              border=True, no_scrollbar=True):
            dpg.add_text(f'{number}  /  TEKANAN DARAH {label}',
                         tag=f'{target}_title', color=DARK_GREEN)
            self.bind_font(f'{target}_title','section')
            dpg.add_spacer(height=7)

            # Two prominent values: suitable for nontechnical users.
            with dpg.group(horizontal=True):
                for field, title in (('sys','SISTOL'),('dia','DIASTOL')):
                    with dpg.child_window(tag=f'bp_box_{target}_{field}',
                                          width=170, height=142, border=False,
                                          no_scrollbar=True):
                        dpg.add_text(title, color=MUTED)
                        dpg.add_text('--', tag=f'{target}_{field}', color=INK)
                        self.bind_font(f'{target}_{field}','hero')
                        dpg.add_text('mmHg', color=MUTED)
            dpg.add_spacer(height=6)

            with dpg.group(horizontal=True):
                self.add_metric(target,'HR','BPM')
                self.add_metric(target,'PP','mmHg')
                self.add_metric(target,'MAP','mmHg')

            dpg.add_spacer(height=10)
            dpg.add_button(label=f'UKUR {label}',tag=f'btn_{target}',
                           width=-1,height=54,
                           callback=lambda s,a,u: self.start_bp(u),
                           user_data=target)
            dpg.bind_item_theme(f'btn_{target}',
                                'theme_akhir' if target=='akhir' else 'theme_measure')
            dpg.add_spacer(height=7)
            dpg.add_text('Belum ada pengukuran',tag=f'{target}_detail',
                         color=MUTED)
            with dpg.group(horizontal=True):
                dpg.add_text('Sesi',color=MUTED)
                dpg.add_text('--',tag=f'{target}_session',color=DARK_GREEN)
                dpg.add_spacer(width=12)
                dpg.add_text('Waktu',color=MUTED)
                dpg.add_text('--',tag=f'{target}_time',color=DARK_GREEN)
        dpg.bind_item_theme(tag,'theme_card')


    def add_foot_panel(self):
        with dpg.child_window(tag='panel_foot',width=400,height=415,
                              border=True,no_scrollbar=True):
            dpg.add_text('01  /  PIJAKAN SENSOR',tag='foot_title',
                         color=DARK_GREEN)
            self.bind_font('foot_title','section')
            dpg.add_spacer(height=7)
            with dpg.group(horizontal=True):
                with dpg.group():
                    dpg.add_text('0%',tag='foot_level',color=INK)
                    self.bind_font('foot_level','hero')
                    dpg.add_text('LEVEL PIJAKAN',color=MUTED)
                dpg.add_spacer(width=8)
                with dpg.drawlist(width=145,height=110,tag='foot_draw'):
                    dpg.draw_rectangle((23,8),(90,105),fill=PALE,
                                       color=LINE,rounding=12)
                    dpg.draw_rectangle((29,99),(84,99),fill=GREEN,
                                       color=GREEN,rounding=9,tag='foot_fill')
                    for y,txt in ((6,'100%'),(51,'50%'),(92,'0%')):
                        dpg.draw_text((102,y),txt,color=MUTED,size=14)
                dpg.add_spacer(width=8)
                with dpg.group():
                    dpg.add_text('RATA-RATA',color=MUTED)
                    dpg.add_text('--',tag='foot_avg',color=DARK_GREEN)
                    self.bind_font('foot_avg','metric')
                    dpg.add_text('SAMPEL',color=MUTED)
                    dpg.add_text('0',tag='foot_count',color=DARK_GREEN)
                    self.bind_font('foot_count','metric')
            dpg.add_separator()
            dpg.add_text('GRAFIK PIJAKAN',color=DARK_GREEN)
            with dpg.plot(tag='foot_plot',height=175,width=-1,
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
        # A distinct horizontal strip, below the 3 measurement cards and above logos.
        with dpg.child_window(tag='panel_comparison',width=-1,height=120,
                              border=True,no_scrollbar=True):
            with dpg.group(horizontal=True):
                dpg.add_text('PERUBAHAN SETELAH RELAKSASI',tag='comparison_title',
                             color=DARK_GREEN)
                self.bind_font('comparison_title','section')
                dpg.add_spacer(width=18)
                dpg.add_text('Menunggu hasil AWAL dan AKHIR',
                             tag='comparison_hint',color=MUTED)
            dpg.add_spacer(height=5)
            with dpg.group(horizontal=True):
                for field,title,unit in (('sys','SISTOL','mmHg'),
                                          ('dia','DIASTOL','mmHg'),
                                          ('pulse','HR','BPM'),
                                          ('pp','PP','mmHg'),
                                          ('map','MAP','mmHg')):
                    with dpg.child_window(tag=f'delta_box_{field}',
                                          width=160,height=70,border=False,
                                          no_scrollbar=True):
                        dpg.add_text(title,color=MUTED)
                        with dpg.group(horizontal=True):
                            dpg.add_text('--',tag=f'delta_{field}',color=DARK_GREEN)
                            self.bind_font(f'delta_{field}','metric')
                            dpg.add_text(unit,color=MUTED)
        dpg.bind_item_theme('panel_comparison','theme_card')

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
            with dpg.group(horizontal=True,tag='header'):
                dpg.add_text('KursiMu',tag='app_title',color=DARK_GREEN)
                self.bind_font('app_title','title')
                dpg.add_text('RELAXATION & HEALTH MONITORING',color=MUTED)
                if self.demo:
                    dpg.add_text('DATA SIMULASI',color=ORANGE)
            dpg.add_button(label='TUTUP APLIKASI   X',tag='btn_close',
                           callback=self.request_close,height=36,width=185)
            dpg.bind_item_theme('btn_close','theme_close')
            with dpg.group(horizontal=True,tag='state_row'):
                dpg.add_text('BP KursiMu:',color=MUTED)
                dpg.add_text('DEMO' if self.demo else 'Mencari...',tag='ble_state',color=ORANGE)
                dpg.add_spacer(width=22)
                dpg.add_text('Pijakan Sensor:',color=MUTED)
                dpg.add_text('DEMO' if self.demo else 'Belum terhubung',tag='serial_state',color=ORANGE)
                dpg.add_spacer(width=22)
                dpg.add_text('Status BP:',color=MUTED)
                dpg.add_text('DEMO' if self.demo else '--',tag='bp_state',color=DARK_GREEN)
                dpg.add_spacer(width=15)
                dpg.add_text(datetime.now().strftime('%d %b %Y  %H:%M'),tag='clock',color=MUTED)
            dpg.add_text('Ukur AWAL  >  Mulai relaksasi  >  Ukur AKHIR  >  Selesai',
                         tag='activity_line',color=MUTED)
            with dpg.child_window(tag='toolbar',height=204,width=-1,border=True,no_scrollbar=True):
                with dpg.group(horizontal=True,tag='identity_row'):
                    dpg.add_text('NAMA PENGGUNA',color=MUTED)
                    dpg.add_input_text(tag='patient_name',hint='Nama pengguna',width=235)
                    dpg.add_text('ID PENGGUNA',color=MUTED)
                    dpg.add_input_text(tag='patient_id',hint='ID unik pengguna',width=190)
                    dpg.add_spacer(width=15)
                    dpg.add_text('DURASI',color=MUTED)
                    dpg.add_text('00:00:00',tag='elapsed',color=DARK_GREEN)
                    dpg.add_spacer(width=8)
                    dpg.add_text('STATUS',color=MUTED)
                    dpg.add_text('BELUM DIMULAI',tag='session_state',color=ORANGE)
                    dpg.add_button(label='SESI BARU',tag='btn_new',width=140,callback=self.new_session)
                    dpg.bind_item_theme('btn_new','theme_neutral')
                dpg.add_separator()
                with dpg.group(horizontal=True,tag='action_row'):
                    dpg.add_button(label='MULAI',tag='btn_start',height=47,width=250,
                                   callback=self.begin_relaxation)
                    dpg.bind_item_theme('btn_start','theme_measure')
                    dpg.add_button(label='PAUSE',tag='btn_pause',height=47,width=250,
                                   callback=self.pause_relaxation)
                    dpg.bind_item_theme('btn_pause','theme_pause')
                    dpg.add_button(label='SELESAI',tag='btn_finish',height=47,width=250,
                                   callback=self.finish_relaxation)
                    dpg.bind_item_theme('btn_finish','theme_finish')
                    dpg.add_button(label='PILIH MUSIK',tag='btn_music',height=47,width=250,
                                   callback=lambda *args: dpg.show_item('music_dialog'))
                    dpg.bind_item_theme('btn_music','theme_music')
                with dpg.group(horizontal=True,tag='connect_row'):
                    dpg.add_text('COM PIJAKAN',color=MUTED)
                    dpg.add_combo(items=['Pilih COM'],default_value='Pilih COM',
                                  tag='port_combo',width=155)
                    dpg.add_button(label='REFRESH',tag='btn_refresh',callback=self.refresh_ports,width=150)
                    dpg.bind_item_theme('btn_refresh','theme_neutral')
                    dpg.add_button(label='SAMBUNG / PUTUS',tag='btn_connect',callback=self.toggle_serial,width=185)
                    dpg.bind_item_theme('btn_connect','theme_connect')
                    dpg.add_spacer(width=20,tag='connection_spacer')
                    dpg.add_text('LAGU:',color=MUTED)
                    dpg.add_text(self.music_path.name if self.music_path else 'Belum ada file musik',
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
        with dpg.file_dialog(tag='music_dialog',show=False,directory_selector=False,
                             callback=self.choose_music,cancel_callback=lambda *args:None,
                             width=750,height=450):
            dpg.add_file_extension('.mp3')
            dpg.add_file_extension('.wav')
            dpg.add_file_extension('.ogg')
        with dpg.window(tag='confirm_close',label='Konfirmasi Keluar',modal=True,
                        show=False,no_resize=True,width=470,height=165):
            dpg.add_text('Sesi sedang berlangsung. Simpan hasil dan tutup aplikasi?')
            dpg.add_spacer(height=12)
            with dpg.group(horizontal=True):
                dpg.add_button(label='SIMPAN & TUTUP',width=205,callback=self.confirm_close)
                dpg.add_button(label='BATAL',width=130,callback=lambda *args:dpg.hide_item('confirm_close'))
        dpg.set_primary_window('main',True)
        dpg.setup_dearpygui()
        dpg.show_viewport()
        self.refresh_ports()
        self.layout(force=True)
        self.update_buttons()



    def layout(self,force=False):
        width = max(850,dpg.get_viewport_width())
        height = max(650,dpg.get_viewport_height())
        key=(width,height)
        if not force and self.last_layout_size==key:
            return
        self.last_layout_size=key
        dpg.configure_item('main',width=width,height=height)
        dpg.set_item_pos('btn_close',[max(8,width-207),8])

        content_width=width-44
        button_width=max(150,(content_width-30)//4)
        for tag in ('btn_start','btn_pause','btn_finish','btn_music'):
            dpg.configure_item(tag,width=button_width)
        extra=max(0,width-1380)
        dpg.configure_item('patient_name',width=235+extra//3)
        dpg.configure_item('patient_id',width=190+extra//3)
        dpg.configure_item('connection_spacer',width=max(8,width-1030))

        # Keep all four visible zones inside the viewport: controls,
        # three equal cards, comparison strip, and slim logo footer.
        toolbar_h=174
        footer_h=60 if height>=800 else 50
        comparison_h=116 if height>=820 else 96
        # Reserve header and outer gaps (approx. 139px).
        available=height-toolbar_h-footer_h-comparison_h-139
        panel_h=max(305,min(450,available))
        panel_w=max(255,(width-52)//3)
        dpg.configure_item('toolbar',height=toolbar_h)
        for panel in ('panel_foot','panel_awal','panel_akhir'):
            dpg.configure_item(panel,width=panel_w,height=panel_h)
        dpg.configure_item('panel_comparison',height=comparison_h)
        dpg.configure_item('footer_logos',height=footer_h)

        # Fit metric cards across each BP panel, without horizontal overflow.
        large_width=max(105,(panel_w-32)//2)
        small_width=max(73,(panel_w-40)//3)
        for target in ('awal','akhir'):
            for metric in ('sys','dia'):
                dpg.configure_item(f'bp_box_{target}_{metric}',width=large_width)
            for metric in ('pulse','pp','map'):
                dpg.configure_item(f'metric_box_{target}_{metric}',width=small_width)

        delta_width=max(115,(width-55)//5)
        for metric in ('sys','dia','pulse','pp','map'):
            dpg.configure_item(f'delta_box_{metric}',width=delta_width)

        # Chart is sized from remaining space, not a fixed graph height.
        # Leave room for header, gauge, counts, title, padding, and axes.
        chart_h=max(90,min(198,panel_h-230))
        dpg.configure_item('foot_plot',height=chart_h)

        if self.logos:
            max_img_h=max(24,footer_h-20)
            max_img_w=max(60,(width-55)//len(self.logos)-12)
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
        if self.demo:
            self.log('Mode simulasi: serial Arduino tidak digunakan')
            return
        if not self.foot_worker:
            self.log('PySerial tidak tersedia')
            return
        if self.foot_worker.selected_port:
            self.foot_worker.set_port(None)
            self.log('Koneksi serial diminta berhenti')
        else:
            port=dpg.get_value('port_combo')
            if port=='Pilih COM' or not port:
                self.log('Pilih COM Arduino pijakan dahulu')
                return
            self.foot_worker.set_port(port)
            self.log(f'Menghubungkan Arduino pijakan melalui {port}')

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
        top=99-value/100*66
        dpg.configure_item('foot_fill',pmin=(29,top),pmax=(84,99))
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

    def choose_music(self,sender,app_data,user_data=None):
        filename=app_data.get('file_path_name')
        if filename:
            self.music_path=Path(filename)
            dpg.set_value('music_label',self.music_path.name)
            self.log(f'Musik dipilih: {self.music_path.name}')

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
        if not self.music_path or not self.music_path.is_file():
            if self.demo:
                self.log('[DEMO] Tidak ada musik; sesi simulasi berjalan tanpa audio')
                return True
            self.log('Pilih file musik relaksasi terlebih dahulu')
            return False
        if not self.init_audio():
            if self.demo:
                self.log(f'[DEMO] Audio tidak tersedia: {self.audio_error}')
                return True
            self.log(f'Musik gagal diputar: {self.audio_error}')
            return False
        try:
            pygame.mixer.music.load(str(self.music_path))
            pygame.mixer.music.set_volume(0.55)
            pygame.mixer.music.play(loops=-1)
            self.audio_playing=True
            return True
        except Exception as exc:
            self.log(f'Audio tidak dapat dimulai: {exc}')
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
        name=(dpg.get_value('patient_name') or '').strip()
        user_id=(dpg.get_value('patient_id') or '').strip()
        if not name or not user_id:
            self.log('Isi Nama dan ID Pengguna sebelum menekan MULAI')
            return
        if self.pending_target:
            self.log('Tunggu hasil pengukuran BP selesai sebelum MULAI')
            return
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
        if self.readings['awal'] is None:
            self.log('Sesi dimulai; PERHATIAN: BP AWAL belum tersedia')
        else:
            self.log('Sesi relaksasi dimulai - musik dan logging pijakan aktif')
        dpg.set_value('session_state','RELAKSASI AKTIF')
        dpg.configure_item('session_state',color=GREEN)

    def pause_relaxation(self,*_):
        if self.session_state=='RUNNING':
            self.elapsed_before_pause=self.elapsed_seconds()
            self.running_since=None
            self.session_state='PAUSED'
            if self.audio_ready:
                pygame.mixer.music.pause()
            dpg.set_value('session_state','DIJEDA')
            dpg.configure_item('session_state',color=ORANGE)
            self.log('Relaksasi dijeda; musik dan logging pijakan berhenti sementara')
        elif self.session_state=='PAUSED':
            if self.audio_ready:
                try:
                    pygame.mixer.music.unpause()
                except Exception as exc:
                    self.log(f'Gagal melanjutkan musik: {exc}')
                    return
            self.running_since=time.monotonic()
            self.session_state='RUNNING'
            dpg.set_value('session_state','RELAKSASI AKTIF')
            dpg.configure_item('session_state',color=GREEN)
            self.log('Relaksasi dilanjutkan; musik dan logging pijakan aktif')
        else:
            self.log('PAUSE hanya tersedia saat sesi relaksasi berjalan')
        self.update_buttons()

    def finish_relaxation(self,*_):
        if self.session_state not in ('RUNNING','PAUSED'):
            self.log('Belum ada sesi aktif untuk diselesaikan')
            return False
        if self.pending_target:
            self.log('Tunggu pengukuran BP selesai sebelum menekan SELESAI')
            return False
        if self.readings['akhir'] is None:
            self.log('BP AKHIR belum tersedia; kolom AKHIR pada CSV akan kosong')
        duration=self.elapsed_seconds()
        self.stop_music()
        self.session_state='FINISHED'
        self.elapsed_before_pause=duration
        self.running_since=None
        self.session_finished=datetime.now()
        if self.foot_file:
            self.foot_file.flush()
            os.fsync(self.foot_file.fileno())
            self.foot_file.close()
            self.foot_file=None
            self.foot_writer=None
        ok=self.write_summary()
        dpg.set_value('session_state','SELESAI' if ok else 'GAGAL SIMPAN')
        dpg.configure_item('session_state',color=GREEN if ok else RED)
        self.update_buttons()
        if ok:
            self.log(f'Sesi selesai, musik berhenti. CSV: {self.session_files["ringkasan"].name}')
        else:
            self.log('Sesi selesai tetapi ringkasan gagal; CSV pijakan tetap tersimpan')
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

    def update_buttons(self):
        ready=self.demo or (self.ble_connected and self.bp_status is not None)
        busy=self.pending_target is not None
        if self.bp_status:
            busy=busy or self.bp_status['state'] in (1,2,3,4) or self.bp_status['pending']
        dpg.configure_item('btn_awal',enabled=ready and not busy and self.session_state=='IDLE')
        dpg.configure_item('btn_akhir',enabled=ready and not busy and self.session_state in ('RUNNING','PAUSED'))
        dpg.configure_item('btn_start',enabled=self.session_state=='IDLE' and not self.pending_target)
        dpg.configure_item('btn_pause',enabled=self.session_state in ('RUNNING','PAUSED'),
                           label='LANJUTKAN' if self.session_state=='PAUSED' else 'PAUSE')
        dpg.configure_item('btn_finish',enabled=self.session_state in ('RUNNING','PAUSED') and not self.pending_target)
        dpg.configure_item('btn_new',enabled=self.session_state in ('IDLE','FINISHED') and not self.pending_target)

    def start_bp(self,target):
        if self.pending_target:
            self.log('Pengukuran sebelumnya belum selesai')
            return
        if target=='awal' and self.session_state!='IDLE':
            self.log('BP AWAL diukur sebelum sesi relaksasi dimulai')
            return
        if target=='akhir' and self.session_state not in ('RUNNING','PAUSED'):
            self.log('BP AKHIR diukur saat/di akhir sesi relaksasi')
            return
        if self.demo:
            self.pending_target={'target':target,'baseline':self.demo_session,'accepted':True}
            self.demo_target=target
            self.demo_due=time.monotonic()+2.2
            self.log(f'[DEMO] Mengukur BP {target.upper()}')
            self.update_buttons()
            return
        if not self.ble_connected or self.bp_status is None:
            self.log('BP KursiMu belum terhubung')
            return
        if self.bp_status['state'] in (1,2,3,4) or self.bp_status['pending']:
            self.log('BP sedang mengukur / hasil menunggu ACK')
            return
        self.pending_target={'target':target,'baseline':self.bp_status['total'],'accepted':False}
        self.ble_requests.put({'op':OP_START,'target':target})
        self.log(f'START satu kali untuk BP {target.upper()}')
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
            dpg.configure_item(tag,color=GREEN if change<0 else ORANGE if change>0 else MUTED)

    def handle_result(self,result):
        assigned=False
        if (self.pending_target and self.pending_target['accepted']
                and result['session']>self.pending_target['baseline']):
            target=self.pending_target['target']
            self.set_panel(target,result)
            self.pending_target=None
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
        if target=='awal' and self.session_state not in ('IDLE',):
            self.log('Hasil AWAL hanya dapat ditetapkan sebelum sesi dimulai')
            return
        if target=='akhir' and self.session_state not in ('RUNNING','PAUSED'):
            self.log('Hasil AKHIR hanya dapat ditetapkan selama sesi aktif')
            return
        self.set_panel(target,self.unassigned)
        self.unassigned=None
        dpg.set_value('unassigned_label','Tidak ada')
        dpg.configure_item('assign_awal',show=False)
        dpg.configure_item('assign_akhir',show=False)
        self.update_buttons()

    def new_session(self,*_):
        if self.session_state in ('RUNNING','PAUSED'):
            self.log('Tekan SELESAI dulu sebelum membuat sesi baru')
            return
        if self.pending_target:
            self.log('Pengukuran BP masih berlangsung')
            return
        self.readings={'awal':None,'akhir':None}
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
            self.update_buttons()
        elif typ=='serial':
            self.serial_connected=item['connected']
            dpg.set_value('serial_state',item['text'])
            dpg.configure_item('serial_state',color=GREEN if self.serial_connected else ORANGE)
        elif typ=='foot':
            self.update_foot(item['level'])
        elif typ=='status':
            previous=self.bp_status
            self.bp_status=item['value']
            code=self.bp_status['state']
            state=STATE_NAMES[code] if code<len(STATE_NAMES) else str(code)
            dpg.set_value('bp_state',state)
            dpg.configure_item('bp_state',color=RED if code==6 else GREEN if code in (0,5) else ORANGE)
            if self.pending_target and self.pending_target['accepted'] and code==6:
                self.log('BP gagal atau timeout. Hasil tidak dipasangkan')
                self.pending_target=None
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
                    self.log(f'START diterima untuk {item["target"].upper()}')
                else:
                    self.log(f'START ditolak: ACK_{desc}')
                    self.pending_target=None
            self.update_buttons()
        elif typ=='command_error':
            self.log(f'Perintah BP gagal: {item["message"]}')
            if item.get('op')==OP_START:
                # Result may still arrive; never repeat a potentially issued START.
                self.pending_target=None
            self.update_buttons()
        elif typ=='result':
            self.handle_result(item['value'])

    def update_demo(self):
        now=time.monotonic()
        if now-self.last_demo_tick>.30:
            self.last_demo_tick=now
            self.update_foot(random.randint(25,85))
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
        if self.session_state in ('RUNNING','PAUSED'):
            dpg.show_item('confirm_close')
        else:
            dpg.stop_dearpygui()

    def confirm_close(self,*_):
        if self.pending_target:
            self.log('BP masih mengukur. Tunggu selesai sebelum keluar.')
            dpg.hide_item('confirm_close')
            return
        if self.session_state in ('RUNNING','PAUSED'):
            self.finish_relaxation()
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
                dpg.render_dearpygui_frame()
        finally:
            if self.session_state in ('RUNNING','PAUSED'):
                # Best-effort summary even if interrupted with BP acquisition pending.
                self.pending_target=None
                self.finish_relaxation()
            elif self.foot_file:
                self.foot_file.close()
            self.stop_music()
            if self.bp_worker:
                self.bp_worker.stop.set()
            if self.foot_worker:
                self.foot_worker.stop.set()
            dpg.destroy_context()


def main():
    parser=argparse.ArgumentParser(description='KursiMu - DearPyGui white/green desktop')
    parser.add_argument('--demo',action='store_true',help='Simulated sensor values (not actual measurements)')
    args=parser.parse_args()
    KursiMuApp(demo=args.demo).run()


if __name__=='__main__':
    main()
