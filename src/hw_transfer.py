"""
hw_transfer.py
==============

LBTiny-IDE hardware transfer dialog.

Opens a debug/test window for sending binary payloads to the LBTiny Supervisor
(Nucleo-F446RE) over its ST-Link virtual COM port. Verifies that the CRC
returned by the supervisor matches a locally-computed CRC over the same data.

Protocol (v3, command-framed):
    PC -> Nucleo:  0xA5  [cmd_1B]  [len_le_4B]  [payload...]
    Nucleo -> PC:  0x5A  [cmd_1B]  [status_1B]  [data_len_le_4B]  [data...]

Commands:
    CMD_TRANSFER_CRC = 0x01
        payload = bytes to be CRC'd
        response data = [declared_len_le_4B] [crc_le_4B]  (8 bytes)
        status: 0x00 OK, 0x01 overflow, 0xFF unknown command

Future commands (flash read, sector erase, ping, etc.) plug into the same
framing without breaking compatibility.
"""

import os
import struct
import time
import random
from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import Qt, QObject, QThread, Signal, Slot
from PySide6.QtGui import QFont, QTextCursor
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QGridLayout,
    QGroupBox, QLabel, QPushButton, QComboBox, QPlainTextEdit, QLineEdit,
    QFileDialog, QSizePolicy, QFrame, QWidget,
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
# Result dataclass passed back from worker to GUI
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
# Worker - lives in its own QThread, does all the serial I/O
# ----------------------------------------------------------------------------
class TransferWorker(QObject):
    """
    Performs serial I/O off the GUI thread. The dialog connects to these
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
                # On Windows USB-CDC the ST-Link sometimes withholds the final
                # short USB frame. Try poking it: check in_waiting (forces driver
                # poll), then a brief sleep before retrying.
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
                f"{RESPONSE_READ_TIMEOUT_S:.1f}s, last in_waiting check")
        return bytes(buf)

    def _read_response_header(self):
        """
        Find the response sync byte 0x5A, then read the rest of the
        7-byte fixed header. Returns (header_bytes_or_None, dropped_count).

        We scan for sync to recover from stale bytes left in the OS buffer
        from a previous failed transfer, and from short reads where bytes
        arrive in pieces split across the sync boundary.
        """
        deadline = time.time() + RESPONSE_READ_TIMEOUT_S
        dropped = 0
        # Scan byte-by-byte until we see 0x5A or hit deadline
        while time.time() < deadline:
            self._port.timeout = 0.25
            b = self._port.read(1)
            if not b:
                continue
            if b[0] == SYNC_NUCLEO_TO_HOST:
                # Found sync. Read the remaining 6 bytes of the fixed header.
                rest = self._read_exact(RESPONSE_FIXED_HEADER - 1)
                if len(rest) != RESPONSE_FIXED_HEADER - 1:
                    # Truncated - return what we got so caller can report
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
        header = struct.pack("<BBI", SYNC_HOST_TO_NUCLEO, CMD_TRANSFER_CRC,
                             declared_len)
        frame = header + payload

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
# Dialog
# ----------------------------------------------------------------------------
class HwTransferDialog(QDialog):
    """
    Debug/test dialog for the LBTiny hardware transfer protocol.

    Layout (top to bottom):
      - Connection bar: port dropdown, refresh, baud, Connect/Disconnect
      - Binary info: source label, size, padded size, local CRC, recompute
      - Debug actions: Send Current Binary / Empty / Test Pattern / Random / Overflow
      - Last transfer results: declared, recv, local CRC, nucleo CRC, status, elapsed
      - Log pane: timestamped log of everything, with Clear button
    """

    # Signals to the worker (cross-thread)
    _request_open       = Signal(str, int)
    _request_close      = Signal()
    _request_xfer       = Signal(str, bytes)
    _request_ping       = Signal()
    _request_mem_write  = Signal(int, bytes)   # (addr, data)
    _request_mem_read   = Signal(int, int)     # (addr, length)
    _request_erase      = Signal()

    def __init__(self, parent=None):
        super().__init__(parent, Qt.Window)
        self._parent_window = parent  # used to fetch current_binary
        self.setWindowTitle("LBTiny - Hardware Transfer (Debug)")
        self.resize(820, 720)

        self._build_ui()
        self._start_worker()
        self._refresh_ports()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        root = QHBoxLayout(self)
        root.setSpacing(6)
        root.setContentsMargins(8, 8, 8, 8)

        # ── LEFT column ──────────────────────────────────────────────────
        left_col = QVBoxLayout()
        left_col.setSpacing(6)
        root.addLayout(left_col, 2)

        # Connection
        conn_group = QGroupBox("Connection")
        conn_row = QHBoxLayout(conn_group)

        conn_row.addWidget(QLabel("Port:"))
        self.port_combo = QComboBox()
        self.port_combo.setMinimumWidth(160)
        conn_row.addWidget(self.port_combo)

        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self._refresh_ports)
        conn_row.addWidget(self.refresh_btn)

        conn_row.addWidget(QLabel("Baud:"))
        self.baud_combo = QComboBox()
        self.baud_combo.addItems(["115200"])
        self.baud_combo.setCurrentText("115200")
        conn_row.addWidget(self.baud_combo)

        self.connect_btn = QPushButton("Connect")
        self.connect_btn.clicked.connect(self._on_connect_clicked)
        conn_row.addWidget(self.connect_btn)

        self.conn_status_label = QLabel("disconnected")
        self.conn_status_label.setStyleSheet("color: #AAA; font-style: italic;")
        conn_row.addWidget(self.conn_status_label, 1)

        left_col.addWidget(conn_group)

        # Binary info
        bin_group = QGroupBox("Binary Info")
        bin_layout = QFormLayout(bin_group)

        self.bin_source_label = QLabel("(none)")
        self.bin_source_label.setWordWrap(True)
        bin_layout.addRow("Source:", self.bin_source_label)

        self.bin_size_label = QLabel("0 bytes")
        bin_layout.addRow("Size:", self.bin_size_label)

        self.bin_padded_label = QLabel("0 bytes")
        bin_layout.addRow("Padded (for CRC):", self.bin_padded_label)

        crc_row = QHBoxLayout()
        self.bin_local_crc_label = QLabel("—")
        self.bin_local_crc_label.setFont(self._mono_font())
        crc_row.addWidget(self.bin_local_crc_label, 1)
        self.recompute_btn = QPushButton("Recompute CRC")
        self.recompute_btn.clicked.connect(self._recompute_local_crc)
        crc_row.addWidget(self.recompute_btn)
        crc_wrap = QWidget()
        crc_wrap.setLayout(crc_row)
        bin_layout.addRow("Local CRC:", crc_wrap)

        left_col.addWidget(bin_group)

        # Debug actions
        act_group = QGroupBox("CRC Debug Actions")
        act_grid = QGridLayout(act_group)
        act_grid.setSpacing(4)

        self.btn_send_current = QPushButton("Send Current Binary")
        self.btn_send_empty   = QPushButton("Send Empty")
        self.btn_send_pattern = QPushButton("Send Test Pattern")
        self.btn_send_random  = QPushButton("Send Random 3000")
        self.btn_send_over    = QPushButton("Send Overflow 5000")

        self.btn_send_current.clicked.connect(self._send_current)
        self.btn_send_empty.clicked.connect(self._send_empty)
        self.btn_send_pattern.clicked.connect(self._send_pattern)
        self.btn_send_random.clicked.connect(self._send_random)
        self.btn_send_over.clicked.connect(self._send_overflow)

        act_grid.addWidget(self.btn_send_current, 0, 0)
        act_grid.addWidget(self.btn_send_empty,   0, 1)
        act_grid.addWidget(self.btn_send_pattern, 1, 0)
        act_grid.addWidget(self.btn_send_random,  1, 1)
        act_grid.addWidget(self.btn_send_over,    2, 0)

        left_col.addWidget(act_group)

        # Last transfer result
        res_group = QGroupBox("Last Transfer Result")
        res_form = QFormLayout(res_group)

        self.res_banner = QLabel("(no transfer yet)")
        self.res_banner.setAlignment(Qt.AlignCenter)
        self.res_banner.setStyleSheet(
            "padding: 6px; background: #333; color: #AAA; font-weight: bold;"
        )
        res_form.addRow(self.res_banner)

        self.res_label_label  = QLabel("—")
        self.res_declared_lbl = QLabel("—")
        self.res_recv_lbl     = QLabel("—")
        self.res_local_crc    = QLabel("—"); self.res_local_crc.setFont(self._mono_font())
        self.res_nucleo_crc   = QLabel("—"); self.res_nucleo_crc.setFont(self._mono_font())
        self.res_status_lbl   = QLabel("—")
        self.res_elapsed_lbl  = QLabel("—")
        self.res_error_lbl    = QLabel(""); self.res_error_lbl.setStyleSheet("color: #FF8888;")

        res_form.addRow("Label:",        self.res_label_label)
        res_form.addRow("Declared len:", self.res_declared_lbl)
        res_form.addRow("Recv len:",     self.res_recv_lbl)
        res_form.addRow("Local CRC:",    self.res_local_crc)
        res_form.addRow("Nucleo CRC:",   self.res_nucleo_crc)
        res_form.addRow("Status:",       self.res_status_lbl)
        res_form.addRow("Elapsed:",      self.res_elapsed_lbl)
        res_form.addRow("Error:",        self.res_error_lbl)

        left_col.addWidget(res_group)
        left_col.addStretch(1)

        self._set_actions_enabled(False)

        # ── RIGHT column ─────────────────────────────────────────────────
        right_col = QVBoxLayout()
        right_col.setSpacing(6)
        root.addLayout(right_col, 3)

        # Memory operations
        self._build_memory_ops_group(right_col)

        # Log
        log_group = QGroupBox("Log")
        log_layout = QVBoxLayout(log_group)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setFont(self._mono_font())
        self.log_view.setMaximumBlockCount(2000)
        self.log_view.setMinimumHeight(40)
        self.log_view.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        log_layout.addWidget(self.log_view, 1)

        log_btn_row = QHBoxLayout()
        clear_btn = QPushButton("Clear Log")
        clear_btn.clicked.connect(self.log_view.clear)
        log_btn_row.addStretch()
        log_btn_row.addWidget(clear_btn)
        log_layout.addLayout(log_btn_row)

        right_col.addWidget(log_group, 1)

    def _mono_font(self) -> QFont:
        f = QFont("Courier New", 10)
        f.setStyleHint(QFont.Monospace)
        f.setFixedPitch(True)
        return f

        # ------------------------------------------------ memory operations UI
    def _build_memory_ops_group(self, parent_layout):
        """
        Build the "Memory Operations" group: ping/erase/program/read buttons
        plus a hex viewer for the most recent read.
        """
        from PySide6.QtWidgets import QSpinBox  # local import to avoid header churn

        mem_group = QGroupBox("Memory Operations  (FPGA bus slave)")
        mem_layout = QVBoxLayout(mem_group)

        # --- Action buttons row ---
        btn_row = QHBoxLayout()

        self.btn_mem_ping = QPushButton("Ping")
        self.btn_mem_ping.setToolTip(
            "CMD_PING - confirms the supervisor link is alive without touching the bus.")
        self.btn_mem_ping.clicked.connect(self._on_mem_ping)
        btn_row.addWidget(self.btn_mem_ping)

        self.btn_mem_erase = QPushButton("Erase ROM")
        self.btn_mem_erase.setToolTip(
            "Send the SST39VF010A sector-erase command sequence "
            "(fills ROM with 0xFF).")
        self.btn_mem_erase.clicked.connect(self._on_mem_erase)
        btn_row.addWidget(self.btn_mem_erase)

        self.btn_mem_program = QPushButton("Program ROM from Current Binary")
        self.btn_mem_program.setToolTip(
            "Write the current assembled binary into ROM starting at 0x000.")
        self.btn_mem_program.clicked.connect(self._on_mem_program_rom)
        btn_row.addWidget(self.btn_mem_program)

        self.btn_mem_read_rom = QPushButton("Read ROM")
        self.btn_mem_read_rom.setToolTip("Read back the full 3 KB ROM region.")
        self.btn_mem_read_rom.clicked.connect(self._on_mem_read_rom)
        btn_row.addWidget(self.btn_mem_read_rom)

        self.btn_mem_read_ram = QPushButton("Read RAM")
        self.btn_mem_read_ram.setToolTip("Read back the 768-byte RAM region.")
        self.btn_mem_read_ram.clicked.connect(self._on_mem_read_ram)
        btn_row.addWidget(self.btn_mem_read_ram)

        btn_row.addStretch(1)
        mem_layout.addLayout(btn_row)

        # --- Custom range row ---
        range_row = QHBoxLayout()
        range_row.addWidget(QLabel("Addr:"))
        self.mem_addr_edit = QLineEdit("0x000")
        self.mem_addr_edit.setMaximumWidth(80)
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

        # Status / banner for last memory op
        self.mem_status_lbl = QLabel("(no op yet)")
        self.mem_status_lbl.setStyleSheet("color: #AAA; font-style: italic;")
        range_row.addWidget(self.mem_status_lbl, 1)
        mem_layout.addLayout(range_row)

        # --- Hex viewer ---
        self.mem_hex_view = QPlainTextEdit()
        self.mem_hex_view.setReadOnly(True)
        self.mem_hex_view.setFont(self._mono_font())
        # Allow plenty of lines for a full 4096 byte dump (256 lines).
        self.mem_hex_view.setMaximumBlockCount(2000)
        self.mem_hex_view.setMinimumHeight(40)
        mem_layout.addWidget(self.mem_hex_view, 1)

        # Stash the buttons so we can enable/disable them with the others.
        self._mem_action_buttons = [
            self.btn_mem_ping, self.btn_mem_erase, self.btn_mem_program,
            self.btn_mem_read_rom, self.btn_mem_read_ram, self.btn_mem_read_custom,
        ]
        for b in self._mem_action_buttons:
            b.setEnabled(False)

        parent_layout.addWidget(mem_group)

    # ------------------------------------------------------------- worker
    def _start_worker(self):
        self._thread = QThread(self)
        self._worker = TransferWorker()
        self._worker.moveToThread(self._thread)

        # Worker -> dialog
        self._worker.log.connect(self._on_log)
        self._worker.connection_changed.connect(self._on_connection_changed)
        self._worker.transfer_complete.connect(self._on_transfer_complete)
        self._worker.memory_op_complete.connect(self._on_memory_op_complete)

        # Dialog -> worker
        self._request_open.connect(self._worker.open_port)
        self._request_close.connect(self._worker.close_port)
        self._request_xfer.connect(self._worker.do_transfer)
        self._request_ping.connect(self._worker.do_ping)
        self._request_mem_write.connect(self._worker.do_mem_write)
        self._request_mem_read.connect(self._worker.do_mem_read)
        self._request_erase.connect(self._worker.do_flash_erase)

        self._thread.start()

    def closeEvent(self, event):
        """Close the worker thread cleanly when the dialog closes."""
        try:
            self._request_close.emit()
            self._thread.quit()
            self._thread.wait(2000)
        except Exception:
            pass
        super().closeEvent(event)

    # ------------------------------------------------------------ helpers
    def _set_actions_enabled(self, enabled: bool):
        self.btn_send_current.setEnabled(enabled)
        self.btn_send_empty.setEnabled(enabled)
        self.btn_send_pattern.setEnabled(enabled)
        self.btn_send_random.setEnabled(enabled)
        self.btn_send_over.setEnabled(enabled)
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
            baud = int(self.baud_combo.currentText())
            self._request_open.emit(dev, baud)
        else:
            self._request_close.emit()

    # ------------------------------------------------------- signal slots
    @Slot(str, str)
    def _on_log(self, level: str, message: str):
        colors = {
            "info":  "#CCC",
            "tx":    "#80C0FF",
            "rx":    "#80FFB0",
            "error": "#FF8888",
        }
        prefixes = {"info": " ", "tx": "→", "rx": "←", "error": "!"}
        color = colors.get(level, "#CCC")
        prefix = prefixes.get(level, " ")
        ts = time.strftime("%H:%M:%S")
        # Use HTML so we can color the line; QPlainTextEdit doesn't render HTML
        # so we use appendPlainText with a level marker prefix instead.
        self.log_view.appendPlainText(f"{ts} {prefix} [{level:5s}] {message}")
        # Scroll to bottom
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
        # Banner
        if r.ok:
            self.res_banner.setText(f"PASS  —  {r.label}")
            self.res_banner.setStyleSheet(
                "padding: 8px; background: #B49600; color: white; font-weight: bold;"
            )
        else:
            self.res_banner.setText(f"FAIL  —  {r.label}")
            self.res_banner.setStyleSheet(
                "padding: 8px; background: #882222; color: white; font-weight: bold;"
            )

        # Fields
        self.res_label_label.setText(r.label)
        self.res_declared_lbl.setText(f"{r.declared_len} bytes")
        self.res_recv_lbl.setText(
            f"{r.nucleo_recv_len} bytes" if r.nucleo_recv_len is not None else "—"
        )
        self.res_local_crc.setText(f"0x{r.local_crc:08X}")
        self.res_nucleo_crc.setText(
            f"0x{r.nucleo_crc:08X}" if r.nucleo_crc is not None else "—"
        )
        if r.nucleo_status is not None:
            status_name = STATUS_NAMES.get(r.nucleo_status, "?")
            self.res_status_lbl.setText(f"0x{r.nucleo_status:02X} ({status_name})")
        else:
            self.res_status_lbl.setText("—")
        self.res_elapsed_lbl.setText(f"{r.elapsed_s:.3f} s")
        self.res_error_lbl.setText(r.error_message or "")

    # ----------------------------------------------------- action handlers
    def _get_current_binary(self) -> Optional[bytes]:
        """Pull the freshest binary from the parent IDE."""
        binary = getattr(self._parent_window, "current_binary", None)
        if binary is None:
            return None
        # current_binary in main.py is a bytearray; convert to bytes
        return bytes(binary)

    def _recompute_local_crc(self):
        """Refresh the binary-info group from whatever is loaded in the IDE."""
        binary = self._get_current_binary()
        if binary is None:
            self.bin_source_label.setText("(no binary - assemble in IDE first)")
            self.bin_size_label.setText("0 bytes")
            self.bin_padded_label.setText("0 bytes")
            self.bin_local_crc_label.setText("—")
            self._on_log("info", "no current_binary on parent window")
            return

        src = getattr(self._parent_window, "current_file", None) or "(unsaved)"
        self.bin_source_label.setText(os.path.basename(src) if src else "(unsaved)")
        self.bin_size_label.setText(f"{len(binary)} bytes")
        self.bin_padded_label.setText(f"{padded_length(binary)} bytes")
        crc = stm32_crc32(binary)
        self.bin_local_crc_label.setText(f"0x{crc:08X}")
        self._on_log("info",
            f"local CRC of current binary: 0x{crc:08X} "
            f"(size {len(binary)}, padded {padded_length(binary)})")

    def _send_current(self):
        binary = self._get_current_binary()
        if binary is None:
            self._on_log("error",
                "no current_binary loaded - assemble in the IDE first")
            return
        self._recompute_local_crc()
        self._request_xfer.emit(f"current binary ({len(binary)} B)", binary)

    def _send_empty(self):
        self._request_xfer.emit("empty payload", b"")

    def _send_pattern(self):
        payload = bytes(range(64))
        self._request_xfer.emit("test pattern (64 counting)", payload)

    def _send_random(self):
        rng = random.Random(0xC0FFEE)
        payload = bytes(rng.randint(0, 255) for _ in range(3000))
        self._request_xfer.emit("random 3000 bytes", payload)

    def _send_overflow(self):
        payload = bytes((i & 0xFF) for i in range(5000))
        self._request_xfer.emit("overflow 5000 bytes", payload)

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
        if binary is None:
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
        # Compose a single-line status banner
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

        # Log it too
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