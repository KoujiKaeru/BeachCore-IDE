"""
hw_transfer.py
==============

LBTiny-IDE hardware transfer panel.

Provides an embeddable QWidget panel for talking to the LBTiny Supervisor
(Nucleo-F446RE) over its ST-Link virtual COM port. The panel is designed to
dock as a fourth column in the IDE's main splitter (to the right of the
register/watch/memory pane) rather than float as a separate dialog window.

The TransferWorker class below carries the full protocol-v4 implementation
(framing, sync-scan, chunked read/write, CRC test) on a background thread.
That code is intentionally left alone — every diagnostic/log path that was
useful during bring-up is preserved in case a future regression needs them.

Protocol (v4, command-framed):
    PC -> Nucleo:  0xA5  [cmd_1B]  [len_le_4B]  [payload...]
    Nucleo -> PC:  0x5A  [cmd_1B]  [status_1B]  [data_len_le_4B]  [data...]

Commands:
    0x01  CMD_TRANSFER_CRC  - payload bytes, returns [declared_len_4B][crc_4B]
    0x02  CMD_PING          - link liveness check
    0x10  CMD_MEM_WRITE     - payload [addr_le_2B][bytes...]
    0x11  CMD_MEM_READ      - payload [addr_le_2B][len_le_2B], returns bytes
    0x12  CMD_FLASH_ERASE   - sector-erase ROM
"""

import os
import struct
import time
from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import Qt, QObject, QThread, Signal, Slot
from PySide6.QtGui import QFont, QTextCursor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QLabel, QPushButton,
    QComboBox, QPlainTextEdit, QLineEdit, QSizePolicy, QSpinBox,
)

import serial
import serial.tools.list_ports

from stm32_crc import stm32_crc32, padded_length


# ----------------------------------------------------------------------------
# Protocol constants
# ----------------------------------------------------------------------------
SYNC_HOST_TO_NUCLEO = 0xA5
SYNC_NUCLEO_TO_HOST = 0x5A

CMD_TRANSFER_CRC    = 0x01
CMD_PING            = 0x02
CMD_MEM_WRITE       = 0x10
CMD_MEM_READ        = 0x11
CMD_FLASH_ERASE     = 0x12

STATUS_OK            = 0x00
STATUS_OVERFLOW      = 0x01
STATUS_OUT_OF_RANGE  = 0x02
STATUS_PAYLOAD_INVAL = 0x03
STATUS_UNKNOWN_CMD   = 0xFF

STATUS_NAMES = {
    STATUS_OK:            "OK",
    STATUS_OVERFLOW:      "OVERFLOW",
    STATUS_OUT_OF_RANGE:  "OUT_OF_RANGE",
    STATUS_PAYLOAD_INVAL: "PAYLOAD_INVALID",
    STATUS_UNKNOWN_CMD:   "UNKNOWN_CMD",
}

# Memory map mirrors firmware / RTL
MEM_ROM_BASE   = 0x000
MEM_ROM_END    = 0xBFF      # inclusive
MEM_RAM_BASE   = 0xC00
MEM_RAM_END    = 0xEFF      # inclusive
MEM_MMIO_BASE  = 0xF00
MEM_MMIO_END   = 0xFFF      # inclusive
MEM_TOTAL_SIZE = 0x1000     # 4096 bytes total

# Largest single-chunk read the firmware can return. The firmware caps each
# CMD_MEM_READ response to 256 bytes; the IDE chunks larger reads.
MAX_READ_CHUNK = 64

MAX_PAYLOAD_BYTES = 4096
DEFAULT_BAUD = 115200
RESPONSE_FIXED_HEADER = 7   # sync + cmd + status + data_len_4B
RESPONSE_READ_TIMEOUT_S = 5.0


# ----------------------------------------------------------------------------
# Result dataclasses passed back from worker to GUI
# ----------------------------------------------------------------------------
@dataclass
class TransferResult:
    ok: bool
    label: str
    declared_len: int
    local_crc: int
    nucleo_recv_len: Optional[int]
    nucleo_crc: Optional[int]
    nucleo_status: Optional[int]
    elapsed_s: float
    sent_bytes: bytes
    received_bytes: bytes
    error_message: str = ""


@dataclass
class MemoryOpResult:
    """Result of a CMD_MEM_WRITE / CMD_MEM_READ / CMD_FLASH_ERASE / CMD_PING."""
    ok: bool
    op: str                    # "write" / "read" / "erase" / "ping"
    addr: int                  # start address (0 for ping/erase)
    length: int                # bytes written / read (0 for ping/erase)
    data: bytes                # returned bytes (only for "read")
    status: Optional[int]
    elapsed_s: float
    error_message: str = ""


# ----------------------------------------------------------------------------
# Worker - lives in its own QThread, does all the serial I/O.
#
# Everything below this banner is left alone on purpose: the verbose stall
# logging in _read_exact / _read_response_header, the small settle sleeps,
# and the drain-before-send block were all earned during cross-platform
# bring-up. Keep them until something better proves out in the field.
# ----------------------------------------------------------------------------
class TransferWorker(QObject):
    """
    Performs serial I/O off the GUI thread. The panel connects to these
    signals and updates the UI from the main thread when they fire.
    """
    log = Signal(str, str)                 # (level, message) - level: "info"/"tx"/"rx"/"error"
    connection_changed = Signal(bool, str) # (connected, status_text)
    transfer_complete = Signal(object)     # TransferResult (CRC operation)
    memory_op_complete = Signal(object)    # MemoryOpResult (all new commands)

    def __init__(self):
        super().__init__()
        self._port: Optional[serial.Serial] = None

    # -- connection management ----------------------------------------------
    @Slot(str, int)
    def open_port(self, port_name: str, baud: int):
        try:
            if self._port is not None and self._port.is_open:
                self._port.close()
            self._port = serial.Serial(port_name, baud, timeout=2.0)
            time.sleep(0.4)  # let the Nucleo boot banner settle
            # Drain anything that arrived during boot/open
            try:
                self._port.reset_input_buffer()
            except Exception:
                pass
            self.connection_changed.emit(
                True, f"connected to {port_name} @ {baud} baud"
            )
            self.log.emit("info", f"opened {port_name} at {baud} baud")
        except serial.SerialException as e:
            self._port = None
            self.connection_changed.emit(False, f"open failed: {e}")
            self.log.emit("error", f"could not open {port_name}: {e}")

    @Slot()
    def close_port(self):
        if self._port is not None and self._port.is_open:
            try:
                self._port.close()
            except Exception:
                pass
            self.log.emit("info", "port closed")
        self._port = None
        self.connection_changed.emit(False, "disconnected")

    # -- read helpers (robust against Windows USB-CDC short reads) ----------
    def _read_exact(self, n: int) -> bytes:
        if n <= 0:
            return b""
        deadline = time.time() + RESPONSE_READ_TIMEOUT_S
        buf = bytearray()
        empty_polls = 0
        while len(buf) < n:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            self._port.timeout = min(0.5, remaining)
            chunk = self._port.read(n - len(buf))
            if chunk:
                buf.extend(chunk)
                empty_polls = 0
            else:
                try:
                    avail = self._port.in_waiting
                except Exception:
                    avail = -1
                empty_polls += 1
                if empty_polls <= 3 or (empty_polls % 20) == 0:
                    self.log.emit("info",
                        f"_read_exact stall: have {len(buf)}/{n}, "
                        f"in_waiting={avail}, empty_polls={empty_polls}")
                time.sleep(0.01)
        if len(buf) < n:
            self.log.emit("error",
                f"_read_exact gave up: have {len(buf)}/{n} after "
                f"{RESPONSE_READ_TIMEOUT_S:.1f}s")
        return bytes(buf)

    def _read_response_header(self):
        """
        Find the response sync byte 0x5A, then read the rest of the
        7-byte fixed header. Returns (header_bytes_or_None, dropped_count).
        """
        deadline = time.time() + RESPONSE_READ_TIMEOUT_S
        dropped = 0
        stall_log_count = 0
        while time.time() < deadline:
            self._port.timeout = 0.25
            b = self._port.read(1)
            if not b:
                try:
                    avail = self._port.in_waiting
                except Exception:
                    avail = -1
                stall_log_count += 1
                if stall_log_count <= 3 or (stall_log_count % 8) == 0:
                    self.log.emit("info",
                        f"_read_response_header stall: dropped={dropped} "
                        f"in_waiting={avail} polls={stall_log_count}")
                continue
            if b[0] == SYNC_NUCLEO_TO_HOST:
                rest = self._read_exact(RESPONSE_FIXED_HEADER - 1)
                if len(rest) != RESPONSE_FIXED_HEADER - 1:
                    return (bytes([b[0]]) + rest, dropped)
                return (bytes([b[0]]) + rest, dropped)
            dropped += 1
        return (None, dropped)

    # -- transfer execution -------------------------------------------------
    @Slot(str, bytes)
    def do_transfer(self, label: str, payload: bytes):
        """Send a CMD_TRANSFER_CRC and read back the response."""
        if self._port is None or not self._port.is_open:
            self.log.emit("error", "transfer requested but port is not open")
            self.transfer_complete.emit(TransferResult(
                ok=False, label=label, declared_len=len(payload),
                local_crc=0, nucleo_recv_len=None, nucleo_crc=None,
                nucleo_status=None, elapsed_s=0.0,
                sent_bytes=b"", received_bytes=b"",
                error_message="port not open",
            ))
            return

        declared_len = len(payload)

        # Drain any stale bytes from boot banner or previous transfers
        try:
            stale = self._port.read(self._port.in_waiting or 0)
            if stale:
                self.log.emit("info",
                    f"drained {len(stale)} stale bytes before transfer")
        except Exception:
            pass

        # Build frame: 0xA5 | cmd | len_le_4B | payload
        header = struct.pack("<BBI", SYNC_HOST_TO_NUCLEO, cmd, len(payload))
        frame = header + payload
        # Pause to let the STM32 finish transmitting any leftover response bytes
        # (data + flush_pad) before we send the next request. Without this, our
        # bytes arrive while the STM32's RX-IT is disarmed and trigger an Overrun
        # Error that disables RX until the next manual reset.
        #time.sleep(0.05)
        self.log.emit("tx", f"frame {len(frame)} bytes  "
                            f"(header: {header.hex(' ')}, payload: {declared_len} bytes)")

        # Local CRC is over what the Nucleo would actually store (max 4096)
        stored_portion = payload[:MAX_PAYLOAD_BYTES]
        local_crc = stm32_crc32(stored_portion)

        t_start = time.time()
        try:
            self._port.write(frame)
            self._port.flush()
        except serial.SerialException as e:
            self.log.emit("error", f"write failed: {e}")
            self.transfer_complete.emit(TransferResult(
                ok=False, label=label, declared_len=declared_len,
                local_crc=local_crc, nucleo_recv_len=None, nucleo_crc=None,
                nucleo_status=None, elapsed_s=time.time() - t_start,
                sent_bytes=frame, received_bytes=b"",
                error_message=f"write failed: {e}",
            ))
            return

        # Small settle delay - on Windows the USB CDC driver may still be
        # transmitting bytes when port.write() returns. Reading too early
        # races the wire and produces phantom timeouts.
        time.sleep(0.05)

        # Robust response read: scan for sync byte, then read header+data.
        # Windows USB CDC drivers can return short reads or leak stale bytes
        # from previous transfers, so we don't trust the first byte to be sync.
        head, sync_search_dropped = self._read_response_header()

        if sync_search_dropped:
            self.log.emit("info",
                f"skipped {sync_search_dropped} non-sync bytes while "
                f"searching for response start")

        if head is None:
            self.log.emit("error",
                "response header timed out (no 0x5A sync byte received)")
            self.transfer_complete.emit(TransferResult(
                ok=False, label=label, declared_len=declared_len,
                local_crc=local_crc, nucleo_recv_len=None, nucleo_crc=None,
                nucleo_status=None, elapsed_s=time.time() - t_start,
                sent_bytes=frame, received_bytes=b"",
                error_message="no response sync byte received within timeout",
            ))
            return

        if len(head) != RESPONSE_FIXED_HEADER:
            self.log.emit("error",
                f"response header truncated: got {len(head)} of "
                f"{RESPONSE_FIXED_HEADER} bytes")
            self.transfer_complete.emit(TransferResult(
                ok=False, label=label, declared_len=declared_len,
                local_crc=local_crc, nucleo_recv_len=None, nucleo_crc=None,
                nucleo_status=None, elapsed_s=time.time() - t_start,
                sent_bytes=frame, received_bytes=head,
                error_message="response header truncated",
            ))
            return

        sync, cmd, status, data_len = struct.unpack("<BBBI", head)

        # By construction sync == SYNC_NUCLEO_TO_HOST (we scanned for it).
        # Sanity guard the data_len so a corrupt header can't make us
        # read forever.
        if data_len > 1024:
            self.log.emit("error",
                f"response data_len suspiciously large: {data_len}")
            self.transfer_complete.emit(TransferResult(
                ok=False, label=label, declared_len=declared_len,
                local_crc=local_crc, nucleo_recv_len=None, nucleo_crc=None,
                nucleo_status=status, elapsed_s=time.time() - t_start,
                sent_bytes=frame, received_bytes=head,
                error_message=f"response data_len out of range ({data_len})",
            ))
            return

        # Read variable-length data with retry-until-complete
        data = self._read_exact(data_len) if data_len else b""
        t_end = time.time()
        full_response = head + data

        self.log.emit("rx", f"response {len(full_response)} bytes: "
                            f"{full_response.hex(' ')}")
        self.log.emit("info",
            f"cmd=0x{cmd:02X} status=0x{status:02X} ({STATUS_NAMES.get(status, '?')}) "
            f"data_len={data_len}")

        # Parse CMD_TRANSFER_CRC response data
        nucleo_recv_len = None
        nucleo_crc = None
        if cmd == CMD_TRANSFER_CRC and data_len >= 8:
            nucleo_recv_len, nucleo_crc = struct.unpack("<II", data[:8])

        # Decide PASS/FAIL
        ok = False
        error_message = ""

        if cmd != CMD_TRANSFER_CRC:
            error_message = f"response cmd 0x{cmd:02X} doesn't match request"
        elif nucleo_recv_len is None or nucleo_crc is None:
            error_message = "response data missing length/CRC"
        elif nucleo_recv_len != declared_len:
            error_message = (f"length mismatch: declared {declared_len}, "
                             f"echoed {nucleo_recv_len}")
        elif nucleo_crc != local_crc:
            if status == STATUS_OVERFLOW:
                error_message = ("CRCs disagree even after accounting for "
                                 "overflow truncation")
            else:
                error_message = "CRC mismatch - data corruption in transfer"
        else:
            ok = True
            if status == STATUS_OVERFLOW:
                self.log.emit("info",
                    f"overflow case OK: sent {declared_len}, "
                    f"stored {MAX_PAYLOAD_BYTES}, CRCs agree on stored portion")

        self.transfer_complete.emit(TransferResult(
            ok=ok, label=label, declared_len=declared_len,
            local_crc=local_crc, nucleo_recv_len=nucleo_recv_len,
            nucleo_crc=nucleo_crc, nucleo_status=status,
            elapsed_s=t_end - t_start,
            sent_bytes=frame, received_bytes=full_response,
            error_message=error_message,
        ))

    # -- generic single-command exchange (used by all the memory commands) --
    def _exchange(self, cmd: int, payload: bytes,
                  data_len_limit: int = 1024):
        """
        Send a framed command and read the response.

        Returns (ok, status, data_bytes, error_message).
        On framing failure ok=False and status may be None.
        """
        if self._port is None or not self._port.is_open:
            return (False, None, b"", "port not open")

        # Drain stale bytes
        try:
            stale = self._port.read(self._port.in_waiting or 0)
            if stale:
                self.log.emit("info",
                    f"drained {len(stale)} stale bytes before cmd 0x{cmd:02X}")
        except Exception:
            pass

        header = struct.pack("<BBI", SYNC_HOST_TO_NUCLEO, cmd, len(payload))
        frame = header + payload
        self.log.emit("tx", f"cmd 0x{cmd:02X} frame {len(frame)} B "
                            f"(payload {len(payload)} B)")
        try:
            self._port.write(frame)
            self._port.flush()
        except serial.SerialException as e:
            return (False, None, b"", f"write failed: {e}")

        time.sleep(0.02)  # small settle for USB CDC

        head, dropped = self._read_response_header()
        if dropped:
            self.log.emit("info", f"skipped {dropped} non-sync bytes")
        if head is None or len(head) != RESPONSE_FIXED_HEADER:
            return (False, None, b"", "no/truncated response header")

        sync, rcmd, status, data_len = struct.unpack("<BBBI", head)
        if data_len > data_len_limit:
            return (False, status, b"",
                    f"response data_len {data_len} exceeds limit {data_len_limit}")

        data = self._read_exact(data_len) if data_len else b""
        if len(data) != data_len:
            return (False, status, data,
                    f"data truncated: got {len(data)} of {data_len}")

        self.log.emit("rx",
            f"cmd 0x{rcmd:02X} status 0x{status:02X} "
            f"({STATUS_NAMES.get(status, '?')}) data_len {data_len}")

        if rcmd != cmd:
            return (False, status, data,
                    f"response cmd 0x{rcmd:02X} != requested 0x{cmd:02X}")
        return (status == STATUS_OK, status, data, "")

    # ---------------- Public memory-command slots ------------------------

    @Slot()
    def do_ping(self):
        """Send CMD_PING and emit a MemoryOpResult."""
        t0 = time.time()
        ok, status, _data, err = self._exchange(CMD_PING, b"")
        self.memory_op_complete.emit(MemoryOpResult(
            ok=ok, op="ping", addr=0, length=0, data=b"",
            status=status, elapsed_s=time.time() - t0, error_message=err,
        ))

    @Slot(int, bytes)
    def do_mem_write(self, addr: int, data: bytes):
        """
        Write `data` to memory starting at `addr`. The firmware handles flash
        unlock sequences for ROM addresses internally.
        """
        t0 = time.time()
        if addr < 0 or addr + len(data) > MEM_TOTAL_SIZE:
            self.memory_op_complete.emit(MemoryOpResult(
                ok=False, op="write", addr=addr, length=len(data), data=b"",
                status=None, elapsed_s=0.0,
                error_message=f"address+length out of range "
                              f"(addr=0x{addr:03X}, len={len(data)})",
            ))
            return

        # The firmware accepts up to 4094 byte writes (4096 minus the 2-byte
        # address header). To keep latency reasonable and progress visible we
        # chunk by 256 bytes.
        chunk = 256
        offset = 0
        while offset < len(data):
            this_n = min(chunk, len(data) - offset)
            payload = struct.pack("<H", addr + offset) + data[offset:offset + this_n]
            ok, status, _d, err = self._exchange(CMD_MEM_WRITE, payload)
            if not ok:
                self.memory_op_complete.emit(MemoryOpResult(
                    ok=False, op="write",
                    addr=addr, length=offset,
                    data=b"", status=status,
                    elapsed_s=time.time() - t0,
                    error_message=err or (
                        f"write failed at offset {offset} "
                        f"(status={STATUS_NAMES.get(status, '?')})"
                    ),
                ))
                return
            offset += this_n

        self.memory_op_complete.emit(MemoryOpResult(
            ok=True, op="write", addr=addr, length=len(data), data=b"",
            status=STATUS_OK, elapsed_s=time.time() - t0,
        ))

    @Slot(int, int)
    def do_mem_read(self, addr: int, length: int):
        """Read `length` bytes from memory starting at `addr`, in 256B chunks."""
        t0 = time.time()
        if addr < 0 or length < 0 or addr + length > MEM_TOTAL_SIZE:
            self.memory_op_complete.emit(MemoryOpResult(
                ok=False, op="read", addr=addr, length=length, data=b"",
                status=None, elapsed_s=0.0,
                error_message=f"address+length out of range "
                              f"(addr=0x{addr:03X}, len={length})",
            ))
            return

        out = bytearray()
        offset = 0
        while offset < length:
            this_n = min(MAX_READ_CHUNK, length - offset)
            payload = struct.pack("<HH", addr + offset, this_n)
            ok, status, data, err = self._exchange(
                CMD_MEM_READ, payload, data_len_limit=this_n + 16)
            if not ok or len(data) != this_n:
                self.memory_op_complete.emit(MemoryOpResult(
                    ok=False, op="read", addr=addr, length=offset,
                    data=bytes(out), status=status,
                    elapsed_s=time.time() - t0,
                    error_message=err or (
                        f"read failed at offset {offset} "
                        f"(status={STATUS_NAMES.get(status, '?')})"
                    ),
                ))
                return
            out.extend(data)
            offset += this_n

        self.memory_op_complete.emit(MemoryOpResult(
            ok=True, op="read", addr=addr, length=length, data=bytes(out),
            status=STATUS_OK, elapsed_s=time.time() - t0,
        ))

    @Slot()
    def do_flash_erase(self):
        t0 = time.time()
        ok, status, _data, err = self._exchange(CMD_FLASH_ERASE, b"")
        self.memory_op_complete.emit(MemoryOpResult(
            ok=ok, op="erase", addr=0, length=0, data=b"",
            status=status, elapsed_s=time.time() - t0, error_message=err,
        ))


# ----------------------------------------------------------------------------
# Panel (embeddable QWidget, lives inside the main window's splitter)
# ----------------------------------------------------------------------------
class HwTransferPanel(QWidget):
    """
    Hardware transfer panel for the LBTiny IDE.

    Designed to dock as a column inside the main window splitter. The panel
    is fairly narrow by default; the log and hex-dump expand to fill the
    column's vertical space.

    Layout (top to bottom):
      - Connection row    : port combo + connect/disconnect + status text
      - Binary status row : "binary: foo.asm  234 B  CRC 0xABCDEF12"
                            (auto-refreshes when the IDE re-assembles)
      - Memory ops group  : Ping / Erase / Program / Read ROM / Read RAM /
                            custom range + last-op status banner
      - Hex viewer        : most recent read response
      - Log pane          : timestamped TX/RX/info/error trace
    """

    # Signals to the worker (cross-thread)
    _request_open       = Signal(str, int)
    _request_close      = Signal()
    _request_xfer       = Signal(str, bytes)
    _request_ping       = Signal()
    _request_mem_write  = Signal(int, bytes)   # (addr, data)
    _request_mem_read   = Signal(int, int)     # (addr, length)
    _request_erase      = Signal()

    def __init__(self, parent_window=None):
        super().__init__()
        self._parent_window = parent_window  # used to fetch current_binary
        self._build_ui()
        self._start_worker()
        self._refresh_ports()
        self.refresh_binary_info()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(6)
        root.setContentsMargins(6, 6, 6, 6)

        root.addWidget(QLabel("<b>Hardware Transfer</b>"))

        # --- Connection row -------------------------------------------------
        conn_row = QHBoxLayout()
        conn_row.setSpacing(4)

        self.port_combo = QComboBox()
        self.port_combo.setMinimumWidth(140)
        self.port_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        conn_row.addWidget(self.port_combo, 1)

        self.refresh_btn = QPushButton("↻")
        self.refresh_btn.setToolTip("Rescan serial ports")
        self.refresh_btn.setMaximumWidth(28)
        self.refresh_btn.clicked.connect(self._refresh_ports)
        conn_row.addWidget(self.refresh_btn)

        self.connect_btn = QPushButton("Connect")
        self.connect_btn.clicked.connect(self._on_connect_clicked)
        conn_row.addWidget(self.connect_btn)

        root.addLayout(conn_row)

        self.conn_status_label = QLabel("disconnected")
        self.conn_status_label.setStyleSheet("color: #AAA; font-style: italic;")
        root.addWidget(self.conn_status_label)

        # --- Binary status (one line, auto-refreshed) ----------------------
        self.binary_status_label = QLabel("binary: (none)")
        self.binary_status_label.setFont(self._mono_font())
        self.binary_status_label.setWordWrap(True)
        self.binary_status_label.setStyleSheet(
            "padding: 4px; background: #2A2A2A; color: #DDD;"
            "border: 1px solid #444;"
        )
        root.addWidget(self.binary_status_label)

        # --- Memory operations group ---------------------------------------
        self._build_memory_ops_group(root)

        # --- Log pane (expands to fill remaining space) --------------------
        log_group = QGroupBox("Log")
        log_layout = QVBoxLayout(log_group)
        log_layout.setContentsMargins(4, 4, 4, 4)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setFont(self._mono_font())
        self.log_view.setMaximumBlockCount(2000)
        self.log_view.setMinimumHeight(80)
        self.log_view.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        log_layout.addWidget(self.log_view, 1)

        log_btn_row = QHBoxLayout()
        clear_btn = QPushButton("Clear Log")
        clear_btn.clicked.connect(self.log_view.clear)
        log_btn_row.addStretch()
        log_btn_row.addWidget(clear_btn)
        log_layout.addLayout(log_btn_row)

        root.addWidget(log_group, 1)

        self._set_actions_enabled(False)

    def _build_memory_ops_group(self, parent_layout):
        """
        Build the "Memory Operations" group: ping/erase/program/read buttons
        plus a hex viewer for the most recent read.
        """
        mem_group = QGroupBox("Memory Operations")
        mem_layout = QVBoxLayout(mem_group)
        mem_layout.setContentsMargins(4, 4, 4, 4)
        mem_layout.setSpacing(4)

        # --- Primary action grid (2 columns) ---
        primary = QHBoxLayout()
        primary.setSpacing(4)

        self.btn_mem_ping = QPushButton("Ping")
        self.btn_mem_ping.setToolTip(
            "CMD_PING - confirms the supervisor link is alive without touching the bus.")
        self.btn_mem_ping.clicked.connect(self._on_mem_ping)
        primary.addWidget(self.btn_mem_ping)

        self.btn_mem_erase = QPushButton("Erase ROM")
        self.btn_mem_erase.setToolTip(
            "Send the SST39VF010A sector-erase command sequence "
            "(fills ROM with 0xFF).")
        self.btn_mem_erase.clicked.connect(self._on_mem_erase)
        primary.addWidget(self.btn_mem_erase)
        mem_layout.addLayout(primary)

        # Program ROM gets its own row (longest label, primary action)
        self.btn_mem_program = QPushButton("Program ROM from Current Binary")
        self.btn_mem_program.setToolTip(
            "Write the current assembled binary into ROM starting at 0x000.")
        self.btn_mem_program.clicked.connect(self._on_mem_program_rom)
        mem_layout.addWidget(self.btn_mem_program)

        read_row = QHBoxLayout()
        read_row.setSpacing(4)
        self.btn_mem_read_rom = QPushButton("Read ROM")
        self.btn_mem_read_rom.setToolTip("Read back the full 3 KB ROM region.")
        self.btn_mem_read_rom.clicked.connect(self._on_mem_read_rom)
        read_row.addWidget(self.btn_mem_read_rom)

        self.btn_mem_read_ram = QPushButton("Read RAM")
        self.btn_mem_read_ram.setToolTip("Read back the 768-byte RAM region.")
        self.btn_mem_read_ram.clicked.connect(self._on_mem_read_ram)
        read_row.addWidget(self.btn_mem_read_ram)
        mem_layout.addLayout(read_row)

        # --- Custom range row ---
        range_row = QHBoxLayout()
        range_row.setSpacing(4)
        range_row.addWidget(QLabel("Addr:"))
        self.mem_addr_edit = QLineEdit("0x000")
        self.mem_addr_edit.setMaximumWidth(70)
        self.mem_addr_edit.setFont(self._mono_font())
        range_row.addWidget(self.mem_addr_edit)
        range_row.addWidget(QLabel("Len:"))
        self.mem_len_spin = QSpinBox()
        self.mem_len_spin.setMinimum(1)
        self.mem_len_spin.setMaximum(MEM_TOTAL_SIZE)
        self.mem_len_spin.setValue(16)
        range_row.addWidget(self.mem_len_spin)

        self.btn_mem_read_custom = QPushButton("Read Range")
        self.btn_mem_read_custom.clicked.connect(self._on_mem_read_custom)
        range_row.addWidget(self.btn_mem_read_custom)
        range_row.addStretch(1)
        mem_layout.addLayout(range_row)

        # Status banner for last memory op
        self.mem_status_lbl = QLabel("(no op yet)")
        self.mem_status_lbl.setStyleSheet("color: #AAA; font-style: italic;")
        self.mem_status_lbl.setWordWrap(True)
        mem_layout.addWidget(self.mem_status_lbl)

        # --- Hex viewer ---
        self.mem_hex_view = QPlainTextEdit()
        self.mem_hex_view.setReadOnly(True)
        self.mem_hex_view.setFont(self._mono_font())
        # Allow plenty of lines for a full 4096 byte dump (256 lines).
        self.mem_hex_view.setMaximumBlockCount(2000)
        self.mem_hex_view.setMinimumHeight(80)
        self.mem_hex_view.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        mem_layout.addWidget(self.mem_hex_view, 1)

        # Stash the buttons so we can enable/disable them all together.
        self._mem_action_buttons = [
            self.btn_mem_ping, self.btn_mem_erase, self.btn_mem_program,
            self.btn_mem_read_rom, self.btn_mem_read_ram, self.btn_mem_read_custom,
        ]
        for b in self._mem_action_buttons:
            b.setEnabled(False)

        parent_layout.addWidget(mem_group)

    def _mono_font(self) -> QFont:
        f = QFont("Courier New", 10)
        f.setStyleHint(QFont.Monospace)
        f.setFixedPitch(True)
        return f

    # ------------------------------------------------------------- worker
    def _start_worker(self):
        self._thread = QThread(self)
        self._worker = TransferWorker()
        self._worker.moveToThread(self._thread)

        # Worker -> panel
        self._worker.log.connect(self._on_log)
        self._worker.connection_changed.connect(self._on_connection_changed)
        self._worker.transfer_complete.connect(self._on_transfer_complete)
        self._worker.memory_op_complete.connect(self._on_memory_op_complete)

        # Panel -> worker
        self._request_open.connect(self._worker.open_port)
        self._request_close.connect(self._worker.close_port)
        self._request_xfer.connect(self._worker.do_transfer)
        self._request_ping.connect(self._worker.do_ping)
        self._request_mem_write.connect(self._worker.do_mem_write)
        self._request_mem_read.connect(self._worker.do_mem_read)
        self._request_erase.connect(self._worker.do_flash_erase)

        self._thread.start()

    def shutdown(self):
        """Cleanly stop the worker thread. Called by the main window on close."""
        try:
            self._request_close.emit()
            self._thread.quit()
            self._thread.wait(2000)
        except Exception:
            pass

    # ------------------------------------------------------------ helpers
    def _set_actions_enabled(self, enabled: bool):
        for b in getattr(self, "_mem_action_buttons", []):
            b.setEnabled(enabled)

    def _refresh_ports(self):
        self.port_combo.clear()
        ports = list(serial.tools.list_ports.comports())
        # Prefer /dev/ttyACM* on Linux, /dev/cu.usbmodem* on Mac, COM* on Windows
        def sort_key(p):
            n = p.device
            score = 0
            if "ACM" in n or "usbmodem" in n: score -= 100
            return (score, n)
        ports.sort(key=sort_key)
        if not ports:
            self.port_combo.addItem("(no ports found)", userData=None)
            return
        for p in ports:
            desc = f"{p.device}"
            if p.description and p.description != "n/a":
                desc += f"  —  {p.description}"
            self.port_combo.addItem(desc, userData=p.device)
        self._on_log("info", f"found {len(ports)} serial port(s)")

    def _current_port_device(self) -> Optional[str]:
        return self.port_combo.currentData()

    def _on_connect_clicked(self):
        if self.connect_btn.text() == "Connect":
            dev = self._current_port_device()
            if not dev:
                self._on_log("error", "no port selected")
                return
            self._request_open.emit(dev, DEFAULT_BAUD)
        else:
            self._request_close.emit()

    # ------------------------------------------------------- signal slots
    @Slot(str, str)
    def _on_log(self, level: str, message: str):
        prefixes = {"info": " ", "tx": "→", "rx": "←", "error": "!"}
        prefix = prefixes.get(level, " ")
        ts = time.strftime("%H:%M:%S")
        self.log_view.appendPlainText(f"{ts} {prefix} [{level:5s}] {message}")
        self.log_view.moveCursor(QTextCursor.End)

    @Slot(bool, str)
    def _on_connection_changed(self, connected: bool, status_text: str):
        self.conn_status_label.setText(status_text)
        if connected:
            self.conn_status_label.setStyleSheet("color: #80FFB0;")
            self.connect_btn.setText("Disconnect")
            self._set_actions_enabled(True)
        else:
            self.conn_status_label.setStyleSheet("color: #AAA; font-style: italic;")
            self.connect_btn.setText("Connect")
            self._set_actions_enabled(False)

    @Slot(object)
    def _on_transfer_complete(self, r: TransferResult):
        # CRC test result is logged; no separate result form anymore.
        status_name = (STATUS_NAMES.get(r.nucleo_status, "?")
                       if r.nucleo_status is not None else "—")
        if r.ok:
            self._on_log("info",
                f"CRC test PASS: {r.label} declared={r.declared_len} "
                f"local=0x{r.local_crc:08X} nucleo=0x{r.nucleo_crc:08X} "
                f"status={status_name} elapsed={r.elapsed_s:.3f}s")
        else:
            self._on_log("error",
                f"CRC test FAIL: {r.label} ({r.error_message}) "
                f"local=0x{r.local_crc:08X} "
                f"nucleo={('0x%08X' % r.nucleo_crc) if r.nucleo_crc is not None else '—'}")

    # ---------------------------------------------- binary info refresh
    def _get_current_binary(self) -> Optional[bytes]:
        """Pull the freshest binary from the parent IDE."""
        binary = getattr(self._parent_window, "current_binary", None)
        if binary is None:
            return None
        # current_binary in main.py is bytes / bytearray
        return bytes(binary)

    def refresh_binary_info(self):
        """
        Update the one-line binary status. Called automatically from the
        main window whenever the IDE re-assembles or saves.
        """
        binary = self._get_current_binary()
        if not binary:
            self.binary_status_label.setText("binary: (none — assemble in IDE first)")
            return

        src = getattr(self._parent_window, "current_file", None) or "(unsaved)"
        name = os.path.basename(src) if src else "(unsaved)"
        size = len(binary)
        crc = stm32_crc32(binary)
        self.binary_status_label.setText(
            f"binary: {name}\n"
            f"{size} B  (padded {padded_length(binary)})  CRC 0x{crc:08X}"
        )

    # ----------------------------------------------- memory op handlers
    def _parse_addr(self, text: str) -> Optional[int]:
        """Parse an address string like '0x100', '256', etc."""
        text = text.strip()
        if not text:
            return None
        try:
            return int(text, 0)  # base=0 auto-detects 0x prefix
        except ValueError:
            return None

    def _on_mem_ping(self):
        self.mem_status_lbl.setText("ping in progress...")
        self.mem_status_lbl.setStyleSheet("color: #DDD;")
        self._request_ping.emit()

    def _on_mem_erase(self):
        self.mem_status_lbl.setText("erasing ROM...")
        self.mem_status_lbl.setStyleSheet("color: #DDD;")
        self._request_erase.emit()

    def _on_mem_program_rom(self):
        """Program the IDE's current binary into ROM starting at address 0."""
        binary = self._get_current_binary()
        if not binary:
            self._on_log("error", "no current binary - assemble in the IDE first")
            return
        if len(binary) > (MEM_ROM_END - MEM_ROM_BASE + 1):
            self._on_log("error",
                f"binary too large for ROM: {len(binary)} > "
                f"{MEM_ROM_END - MEM_ROM_BASE + 1} bytes")
            return
        self.mem_status_lbl.setText(f"programming ROM, {len(binary)} bytes...")
        self.mem_status_lbl.setStyleSheet("color: #DDD;")
        self._request_mem_write.emit(MEM_ROM_BASE, bytes(binary))

    def _on_mem_read_rom(self):
        n = MEM_ROM_END - MEM_ROM_BASE + 1
        self.mem_status_lbl.setText(f"reading ROM ({n} bytes)...")
        self.mem_status_lbl.setStyleSheet("color: #DDD;")
        self._request_mem_read.emit(MEM_ROM_BASE, n)

    def _on_mem_read_ram(self):
        n = MEM_RAM_END - MEM_RAM_BASE + 1
        self.mem_status_lbl.setText(f"reading RAM ({n} bytes)...")
        self.mem_status_lbl.setStyleSheet("color: #DDD;")
        self._request_mem_read.emit(MEM_RAM_BASE, n)

    def _on_mem_read_custom(self):
        addr = self._parse_addr(self.mem_addr_edit.text())
        if addr is None:
            self._on_log("error", f"invalid address: {self.mem_addr_edit.text()!r}")
            return
        n = self.mem_len_spin.value()
        if addr < 0 or addr + n > MEM_TOTAL_SIZE:
            self._on_log("error",
                f"range 0x{addr:03X}+{n} extends past 0x{MEM_TOTAL_SIZE:03X}")
            return
        self.mem_status_lbl.setText(f"reading 0x{addr:03X}+{n}...")
        self.mem_status_lbl.setStyleSheet("color: #DDD;")
        self._request_mem_read.emit(addr, n)

    @Slot(object)
    def _on_memory_op_complete(self, r):
        """Receive a MemoryOpResult and update the UI."""
        if r.ok:
            self.mem_status_lbl.setStyleSheet("color: #80FFB0;")
            if r.op == "ping":
                self.mem_status_lbl.setText(
                    f"ping OK  ({r.elapsed_s * 1000:.1f} ms)")
            elif r.op == "erase":
                self.mem_status_lbl.setText(
                    f"erase OK  ({r.elapsed_s * 1000:.1f} ms)")
            elif r.op == "write":
                self.mem_status_lbl.setText(
                    f"write OK: {r.length} B at 0x{r.addr:03X}  "
                    f"({r.elapsed_s:.2f} s, "
                    f"{r.length / max(r.elapsed_s, 1e-6):.0f} B/s)")
            elif r.op == "read":
                self.mem_status_lbl.setText(
                    f"read OK: {r.length} B at 0x{r.addr:03X}  "
                    f"({r.elapsed_s:.2f} s)")
                self._render_hex_dump(r.addr, r.data)
        else:
            self.mem_status_lbl.setStyleSheet("color: #FF8888;")
            stxt = STATUS_NAMES.get(r.status, "?") if r.status is not None else "—"
            self.mem_status_lbl.setText(
                f"{r.op} FAILED  (status={stxt}): {r.error_message}")

        log_level = "info" if r.ok else "error"
        self._on_log(log_level,
            f"memory op {r.op} ok={r.ok} addr=0x{r.addr:03X} len={r.length} "
            f"elapsed={r.elapsed_s:.3f}s err='{r.error_message}'")

    # ------------------------------------------------------- hex dump
    def _render_hex_dump(self, base_addr: int, data: bytes):
        """Render a classic 16-column hex+ASCII view into the memory pane."""
        lines = []
        for offset in range(0, len(data), 16):
            row = data[offset:offset + 16]
            hex_part = " ".join(f"{b:02X}" for b in row)
            hex_part = hex_part.ljust(16 * 3 - 1)
            ascii_part = "".join(
                (chr(b) if 32 <= b < 127 else ".") for b in row
            )
            lines.append(f"{base_addr + offset:04X}  {hex_part}  |{ascii_part}|")
        self.mem_hex_view.setPlainText("\n".join(lines))


# ----------------------------------------------------------------------------
# Backwards-compat alias.
#
# Older code paths may still try to import HwTransferDialog. The panel is the
# canonical widget now; main.py embeds it directly into the main splitter and
# toggles its visibility from the toolbar. No floating dialog is created.
# ----------------------------------------------------------------------------
HwTransferDialog = HwTransferPanel
