# Copyright (C) 2026-Today Daysum
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl.html).

import logging
import re
import socket
import time
from collections import deque
from pathlib import Path
from threading import Event, Lock, Thread

from flask import jsonify, request

from pywebdriver import app, config, drivers

from .base_driver import AbstractDriver

_logger = logging.getLogger(__name__)

try:
    import serial
except ImportError:
    serial = None

MAX_EPC_LENGTH = 128
DEFAULT_MAX_TAGS_PER_POLL = 200
DEFAULT_MAX_BUFFER_EVENTS = 10000


def normalize_epc(value):
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    value = "".join(ch for ch in value if ch.isprintable()).strip().upper()
    epc = re.sub(r"[^0-9A-Z]", "", value)
    if len(epc) > MAX_EPC_LENGTH:
        return ""
    return epc


def extract_epc(value):
    if isinstance(value, dict):
        for key in ("epc", "EPC", "tag"):
            if key in value:
                return value[key]
        return ""
    return value


def split_complete_lines(chunks, pending=b""):
    if pending is None:
        pending = b""
    if isinstance(pending, str):
        pending = pending.encode("utf-8")

    complete_lines = []
    for chunk in chunks:
        if not chunk:
            continue
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8")
        pending += chunk
        lines = pending.splitlines(True)
        if pending.endswith((b"\n", b"\r")):
            complete_lines.extend(lines)
            pending = b""
        elif lines:
            complete_lines.extend(lines[:-1])
            pending = lines[-1]
        if len(pending) > 65536:
            pending = pending[-4096:]
    return complete_lines, pending


class RfidEventBuffer(object):
    def __init__(self, max_events):
        self.events = deque(maxlen=max_events)
        self.next_cursor = 0
        self.lock = Lock()

    def add_tags(self, tags):
        if tags is None:
            return 0
        if isinstance(tags, (dict, str, bytes)):
            tags = [tags]
        normalized_tags = []
        seen_epcs = set()
        for tag in tags:
            epc = normalize_epc(extract_epc(tag))
            if not epc or epc in seen_epcs:
                continue
            seen_epcs.add(epc)
            event = {"epc": epc}
            if isinstance(tag, dict):
                for key in ("antenna", "rssi", "seen_at"):
                    if key in tag:
                        event[key] = tag[key]
            normalized_tags.append(event)

        with self.lock:
            for event in normalized_tags:
                self.next_cursor += 1
                self.events.append((self.next_cursor, event))
        return len(normalized_tags)

    def read_since(self, cursor, limit):
        cursor = self._parse_cursor(cursor)
        with self.lock:
            matching_events = [
                (event_cursor, event)
                for event_cursor, event in self.events
                if event_cursor > cursor
            ]
            selected_events = matching_events[:limit]
            if selected_events:
                next_cursor = selected_events[-1][0]
            else:
                next_cursor = cursor
            has_more = len(matching_events) > len(selected_events)

        return {
            "cursor": str(next_cursor),
            "tags": [event for _event_cursor, event in selected_events],
            "has_more": has_more,
        }

    @staticmethod
    def _parse_cursor(cursor):
        try:
            return int(cursor or 0)
        except (TypeError, ValueError):
            return 0


class RfidDriver(AbstractDriver):
    def __init__(self, driver_config):
        super(RfidDriver, self).__init__()
        self.config = driver_config
        self.mode = self._get("mode", "disabled").strip().lower()
        self.max_tags_per_poll = self._get_int(
            "max_tags_per_poll",
            DEFAULT_MAX_TAGS_PER_POLL,
            1,
            DEFAULT_MAX_TAGS_PER_POLL,
        )
        max_buffer_events = self._get_int(
            "max_buffer_events",
            DEFAULT_MAX_BUFFER_EVENTS,
            self.max_tags_per_poll,
            100000,
        )
        self.poll_interval = self._get_float("poll_interval", 0.2, 0.05, 60.0)
        self.read_timeout = self._get_float("read_timeout", 0.2, 0.05, 30.0)
        self.reconnect_delay = self._get_float("reconnect_delay", 1.0, 0.1, 60.0)
        self.buffer = RfidEventBuffer(max_buffer_events)
        self.stop_event = Event()
        self.thread = None

        if self.mode == "disabled":
            self.set_status("disconnected", "RFID driver is disabled")
        elif self.mode == "simulator":
            self.buffer.add_tags(self._configured_simulator_tags())
            self.set_status("connected", "RFID simulator enabled")
        elif self.mode == "file":
            self._start_background_reader(self._run_file_reader)
        elif self.mode == "serial":
            self._start_background_reader(self._run_serial_reader)
        elif self.mode == "tcp_line":
            self._start_background_reader(self._run_tcp_line_reader)
        else:
            self.set_status(
                "error",
                "Unsupported RFID driver mode: {}".format(self.mode),
            )

    def get_vendor_product(self):
        return None

    def get_status(self, **params):
        return {
            "status": self.status["status"],
            "messages": list(self.status["messages"]),
        }

    def set_status(self, status, message=None):
        if status == self.status["status"]:
            if message and (
                not self.status["messages"]
                or message != self.status["messages"][-1]
            ):
                self.status["messages"].append(message)
        else:
            self.status["status"] = status
            self.status["messages"] = [message] if message else []

    def read(self, payload):
        if self.mode == "simulator":
            self.buffer.add_tags(payload.get("tags", []))
        result = self.buffer.read_since(
            payload.get("cursor"),
            self.max_tags_per_poll,
        )
        result["info"] = "ok"
        return result

    def _get(self, option, fallback):
        return self.config.get("rfid_driver", option, fallback=fallback)

    def _get_int(self, option, fallback, minimum, maximum):
        try:
            value = self.config.getint("rfid_driver", option, fallback=fallback)
        except ValueError:
            _logger.warning("Invalid rfid_driver.%s value; using %s", option, fallback)
            value = fallback
        return max(minimum, min(value, maximum))

    def _get_float(self, option, fallback, minimum, maximum):
        try:
            value = self.config.getfloat("rfid_driver", option, fallback=fallback)
        except ValueError:
            _logger.warning("Invalid rfid_driver.%s value; using %s", option, fallback)
            value = fallback
        return max(minimum, min(value, maximum))

    def _get_boolean(self, option, fallback):
        try:
            return self.config.getboolean("rfid_driver", option, fallback=fallback)
        except ValueError:
            _logger.warning("Invalid rfid_driver.%s value; using %s", option, fallback)
            return fallback

    def _configured_simulator_tags(self):
        raw_tags = self._get("simulator_tags", "")
        if not raw_tags:
            return []
        return [tag.strip() for tag in re.split(r"[,\n]+", raw_tags) if tag.strip()]

    def _start_background_reader(self, target):
        self.thread = Thread(target=target, name="pywebdriver-rfid", daemon=True)
        self.thread.start()

    def _add_lines(self, lines):
        return self.buffer.add_tags(
            line.decode("utf-8", errors="ignore") if isinstance(line, bytes) else line
            for line in lines
        )

    def _run_file_reader(self):
        file_path = self._get("file_path", "")
        if not file_path:
            self.set_status("disconnected", "rfid_driver.file_path is not configured")
            return

        path = Path(file_path)
        offset = 0
        pending = b""
        if self._get_boolean("file_start_at_end", True) and path.exists():
            offset = path.stat().st_size

        while not self.stop_event.is_set():
            try:
                if path.exists() and path.stat().st_size < offset:
                    offset = 0
                    pending = b""
                with path.open("rb") as file_handle:
                    file_handle.seek(offset)
                    chunks = file_handle.readlines()
                    offset = file_handle.tell()
                lines, pending = split_complete_lines(chunks, pending)
                if lines:
                    self._add_lines(lines)
                self.set_status("connected", "RFID file reader active")
            except FileNotFoundError:
                self.set_status(
                    "disconnected",
                    "RFID tag file not found: {}".format(file_path),
                )
            except OSError as error:
                self.set_status("error", str(error))
                _logger.warning("RFID file reader error: %s", error)
            time.sleep(self.poll_interval)

    def _run_serial_reader(self):
        if serial is None:
            self.set_status("error", "pyserial is required for RFID serial mode")
            return
        port = self._get("port", "")
        if not port:
            self.set_status("disconnected", "rfid_driver.port is not configured")
            return

        while not self.stop_event.is_set():
            try:
                pending = b""
                with serial.Serial(
                    port=port,
                    baudrate=self._get_int("baudrate", 9600, 1, 921600),
                    bytesize=self._get_int("bytesize", 8, 5, 8),
                    parity=self._get("parity", "N"),
                    stopbits=self._get_float("stopbits", 1, 1, 2),
                    timeout=self.read_timeout,
                ) as connection:
                    self.set_status("connected", "RFID serial reader active")
                    while not self.stop_event.is_set():
                        chunk = connection.readline()
                        lines, pending = split_complete_lines([chunk], pending)
                        if lines:
                            self._add_lines(lines)
            except serial.SerialException as error:
                self.set_status("error", str(error))
                _logger.warning("RFID serial reader error: %s", error)
                time.sleep(self.reconnect_delay)
            except OSError as error:
                self.set_status("error", str(error))
                _logger.warning("RFID serial reader OS error: %s", error)
                time.sleep(self.reconnect_delay)

    def _run_tcp_line_reader(self):
        host = self._get("host", "")
        port = self._get_int("port", 0, 0, 65535)
        if not host or not port:
            self.set_status(
                "disconnected",
                "rfid_driver.host and rfid_driver.port are required for tcp_line mode",
            )
            return

        while not self.stop_event.is_set():
            try:
                with socket.create_connection(
                    (host, port),
                    timeout=self._get_float("connect_timeout", 2, 0.1, 60.0),
                ) as connection:
                    connection.settimeout(self.read_timeout)
                    self.set_status("connected", "RFID TCP line reader active")
                    self._read_tcp_lines(connection)
            except OSError as error:
                self.set_status("error", str(error))
                _logger.warning("RFID TCP line reader error: %s", error)
                time.sleep(self.reconnect_delay)

    def _read_tcp_lines(self, connection):
        pending = b""
        while not self.stop_event.is_set():
            try:
                chunk = connection.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                raise OSError("RFID TCP line stream closed")
            lines, pending = split_complete_lines([chunk], pending)
            if lines:
                self._add_lines(lines)


def _request_payload():
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return {}, None
    params = body.get("params")
    request_id = body.get("id")
    if isinstance(params, dict):
        return params, request_id
    return body, request_id


drivers["rfid"] = RfidDriver(config)


@app.route("/hw_proxy/rfid_read", methods=["POST"])
def rfid_read_post():
    payload, request_id = _request_payload()
    response = {
        "jsonrpc": "2.0",
        "result": drivers["rfid"].read(payload),
    }
    if request_id is not None:
        response["id"] = request_id
    return jsonify(response)
