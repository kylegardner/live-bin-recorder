#!/usr/bin/env python3
"""
ArduPilot BIN Block Receiver — real-time MAVLink log streaming GUI.

FC param:  LOG_BACKEND_TYPE = 2  (MAVLink only)
                             3  (SD + MAVLink)

Requires:  pip install pymavlink PySide6
"""

import os
import sys
import time
import threading
from pathlib import Path
from datetime import datetime

from PySide6.QtCore import (Qt, QThread, Signal, QTimer, QSize)
from PySide6.QtGui import (QFont, QFontDatabase, QColor, QPalette, QIcon,
                            QPainter, QBrush, QPen)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QLineEdit, QFileDialog, QScrollArea,
    QFrame, QSizePolicy, QSpacerItem, QGridLayout,
)

try:
    from pymavlink import mavutil
except ImportError:
    # Show a friendly message if run without pymavlink
    app = QApplication(sys.argv)
    from PySide6.QtWidgets import QMessageBox
    msg = QMessageBox()
    msg.setWindowTitle("Missing dependency")
    msg.setText("pymavlink is not installed.\n\nRun:  pip install pymavlink")
    msg.exec()
    sys.exit(1)

# ── constants ─────────────────────────────────────────────────────────────────

BLOCK_SIZE = 200
MAV_REMOTE_LOG_DATA_BLOCK_STOP  = 0xFFFFFFFF
MAV_REMOTE_LOG_DATA_BLOCK_START = 0xFFFFFFFE
ACK  = 1

# ── palette tokens ─────────────────────────────────────────────────────────────

C_BG      = "#0B0E14"
C_PANEL   = "#131720"
C_BORDER  = "#1E2533"
C_AMBER   = "#E8A020"
C_AMBER_D = "#7A5010"
C_GREEN   = "#3DBA6F"
C_RED     = "#D45050"
C_TEXT    = "#D0D8E8"
C_MUTED   = "#5A6478"
C_INPUT   = "#0F1319"

APP_STYLESHEET = f"""
QMainWindow, QWidget {{
    background-color: {C_BG};
    color: {C_TEXT};
    font-family: "JetBrains Mono", "Consolas", "Courier New", monospace;
    font-size: 13px;
}}
QLabel {{
    background: transparent;
}}
QLineEdit {{
    background-color: {C_INPUT};
    border: 1px solid {C_BORDER};
    border-radius: 4px;
    color: {C_TEXT};
    padding: 6px 10px;
    selection-background-color: {C_AMBER_D};
}}
QLineEdit:focus {{
    border-color: {C_AMBER};
}}
QPushButton {{
    background-color: {C_PANEL};
    border: 1px solid {C_BORDER};
    border-radius: 4px;
    color: {C_TEXT};
    padding: 7px 16px;
    font-family: "JetBrains Mono", "Consolas", monospace;
    font-size: 13px;
}}
QPushButton:hover {{
    border-color: {C_AMBER};
    color: {C_AMBER};
}}
QPushButton:pressed {{
    background-color: {C_AMBER_D};
}}
QPushButton:disabled {{
    color: {C_MUTED};
    border-color: {C_BORDER};
}}
QPushButton#primary {{
    background-color: {C_AMBER};
    border-color: {C_AMBER};
    color: {C_BG};
    font-weight: bold;
}}
QPushButton#primary:hover {{
    background-color: #F5B840;
    color: {C_BG};
}}
QPushButton#primary:disabled {{
    background-color: {C_AMBER_D};
    border-color: {C_AMBER_D};
    color: #3A2A08;
}}
QPushButton#danger {{
    border-color: {C_RED};
    color: {C_RED};
}}
QPushButton#danger:hover {{
    background-color: {C_RED};
    color: white;
}}
QScrollArea {{
    border: none;
    background: transparent;
}}
QScrollBar:vertical {{
    background: {C_PANEL};
    width: 6px;
    border: none;
}}
QScrollBar::handle:vertical {{
    background: {C_BORDER};
    border-radius: 3px;
    min-height: 20px;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0px;
}}
"""

# ── receiver thread ───────────────────────────────────────────────────────────

class ReceiverThread(QThread):
    status_changed  = Signal(str)        # "idle" | "connecting" | "connected" | "recording" | "saving"
    block_received  = Signal(int, int)   # (seq_count, bytes_total)
    log_saved       = Signal(str, int)   # (file_path, bytes)
    message         = Signal(str)        # log line
    error           = Signal(str)

    def __init__(self, udp: str, out_dir: Path, idle_timeout: int = 10):
        super().__init__()
        self._udp = udp
        self._out = out_dir
        self._idle_timeout = idle_timeout
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        # Build connection string
        udp = self._udp.strip()
        if ":" not in udp:
            udp = f"0.0.0.0:{udp}"
        conn_str = f"udpin:{udp}"

        self.status_changed.emit("connecting")
        self.message.emit(f"Opening {conn_str} …")

        try:
            mav = mavutil.mavlink_connection(conn_str, autoreconnect=True)
        except Exception as e:
            self.error.emit(f"Connection failed: {e}")
            self.status_changed.emit("idle")
            return

        self.message.emit("Waiting for heartbeat …")
        hb = mav.wait_heartbeat(timeout=30)
        if self._stop.is_set():
            mav.close(); return
        if not hb:
            self.error.emit("No heartbeat received — check FC is sending MAVLink to this address.")
            self.status_changed.emit("idle")
            mav.close(); return

        self.message.emit(f"Heartbeat from sys={mav.target_system}")
        self.status_changed.emit("connected")

        # Loop: receive one log per iteration
        while not self._stop.is_set():
            self._receive_one_log(mav)
            if not self._stop.is_set():
                self.message.emit("Ready for next log stream …")
                self.status_changed.emit("connected")

        mav.close()
        self.status_changed.emit("idle")
        self.message.emit("Disconnected.")

    def _receive_one_log(self, mav):
        blocks: dict[int, bytes] = {}
        highest_seq = -1
        recording = False
        last_block_time = time.time()

        while not self._stop.is_set():
            msg = mav.recv_match(type="REMOTE_LOG_DATA_BLOCK", blocking=True, timeout=1)
            if msg is None:
                if recording and (time.time() - last_block_time > self._idle_timeout):
                    self.message.emit("Stream idle — saving log.")
                    break
                continue

            seqno = msg.seqno
            last_block_time = time.time()

            if seqno == MAV_REMOTE_LOG_DATA_BLOCK_START:
                if not recording:
                    self.message.emit("Log stream started.")
                    self.status_changed.emit("recording")
                    recording = True
                mav.mav.remote_log_block_status_send(
                    mav.target_system, mav.target_component, seqno, ACK)
                continue

            if seqno == MAV_REMOTE_LOG_DATA_BLOCK_STOP:
                self.message.emit("STOP sentinel received.")
                mav.mav.remote_log_block_status_send(
                    mav.target_system, mav.target_component, seqno, ACK)
                break

            if not recording:
                recording = True
                self.status_changed.emit("recording")

            blocks[seqno] = bytes(msg.data[:BLOCK_SIZE])
            mav.mav.remote_log_block_status_send(
                mav.target_system, mav.target_component, seqno, ACK)

            if seqno > highest_seq:
                highest_seq = seqno

            total = (highest_seq + 1) * BLOCK_SIZE
            self.block_received.emit(len(blocks), total)

        if not blocks:
            return

        self.status_changed.emit("saving")
        self.message.emit(f"Saving {len(blocks)} blocks …")
        self._out.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = self._out / f"flight_{ts}.BIN"
        with open(path, "wb") as fh:
            for i in range(highest_seq + 1):
                fh.write(blocks.get(i, b"\x00" * BLOCK_SIZE))
        size = path.stat().st_size
        self.log_saved.emit(str(path), size)
        self.message.emit(f"Saved → {path.name}")

        missing = (highest_seq + 1) - len(blocks)
        if missing:
            self.message.emit(f"  ⚠  {missing} gap(s) filled with zeros")


# ── custom widgets ─────────────────────────────────────────────────────────────

class StatusLamp(QWidget):
    """Big circular status indicator."""
    COLORS = {
        "idle":       ("#2A2F3C", "#3A4050"),
        "connecting": ("#7A5010", "#E8A020"),
        "connected":  ("#1A3A28", "#3DBA6F"),
        "recording":  ("#3DBA6F", "#80F0AF"),
        "saving":     ("#7A5010", "#E8A020"),
        "error":      ("#5A1010", "#D45050"),
    }

    def __init__(self):
        super().__init__()
        self._state = "idle"
        self._pulse = 0.0
        self._pulse_dir = 1
        self.setFixedSize(96, 96)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(40)

    def set_state(self, state: str):
        self._state = state
        self.update()

    def _tick(self):
        if self._state in ("recording", "connecting"):
            self._pulse += 0.06 * self._pulse_dir
            if self._pulse >= 1.0:
                self._pulse_dir = -1
            elif self._pulse <= 0.0:
                self._pulse_dir = 1
        else:
            self._pulse = 0.0
        self.update()

    def paintEvent(self, _):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        core_hex, glow_hex = self.COLORS.get(self._state, self.COLORS["idle"])

        cx, cy, r = self.width() // 2, self.height() // 2, 34

        # Outer glow ring (pulsing when active)
        if self._state in ("recording", "connecting", "saving"):
            glow = QColor(glow_hex)
            glow.setAlpha(int(30 + 60 * self._pulse))
            painter.setBrush(QBrush(glow))
            painter.setPen(Qt.NoPen)
            glow_r = r + 10 + int(8 * self._pulse)
            painter.drawEllipse(cx - glow_r, cy - glow_r, glow_r * 2, glow_r * 2)

        # Core circle
        painter.setBrush(QBrush(QColor(core_hex)))
        painter.setPen(QPen(QColor(glow_hex), 2))
        painter.drawEllipse(cx - r, cy - r, r * 2, r * 2)

        # Inner highlight
        inner = QColor(glow_hex)
        inner.setAlpha(160)
        painter.setBrush(QBrush(inner))
        painter.setPen(Qt.NoPen)
        ir = r - 8
        painter.drawEllipse(cx - ir, cy - ir, ir * 2, ir * 2)
        painter.end()


class Divider(QFrame):
    def __init__(self, vertical=False):
        super().__init__()
        self.setFrameShape(QFrame.VLine if vertical else QFrame.HLine)
        self.setStyleSheet(f"color: {C_BORDER}; background: {C_BORDER};")
        if vertical:
            self.setFixedWidth(1)
        else:
            self.setFixedHeight(1)


class StatCard(QWidget):
    def __init__(self, label: str, initial: str = "—"):
        super().__init__()
        self.setStyleSheet(
            f"background:{C_PANEL}; border:1px solid {C_BORDER}; border-radius:6px;"
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(4)

        self._lbl = QLabel(label.upper())
        self._lbl.setStyleSheet(f"color:{C_MUTED}; font-size:10px; letter-spacing:1.5px; border:none;")
        lay.addWidget(self._lbl)

        self._val = QLabel(initial)
        self._val.setStyleSheet(
            f"color:{C_AMBER}; font-size:22px; font-weight:bold; border:none;"
        )
        self._val.setFont(QFont("JetBrains Mono, Consolas, Courier New", 18, QFont.Bold))
        lay.addWidget(self._val)

    def set_value(self, v: str):
        self._val.setText(v)


class LogRow(QWidget):
    def __init__(self, path: str, size: int, parent=None):
        super().__init__(parent)
        self.setStyleSheet(
            f"background:{C_PANEL}; border:1px solid {C_BORDER}; border-radius:4px;"
        )
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)

        icon = QLabel("◉")
        icon.setStyleSheet(f"color:{C_GREEN}; border:none; font-size:10px;")
        lay.addWidget(icon)

        name = Path(path).name
        name_lbl = QLabel(name)
        name_lbl.setStyleSheet(f"color:{C_TEXT}; border:none;")
        lay.addWidget(name_lbl, 1)

        size_lbl = QLabel(self._fmt(size))
        size_lbl.setStyleSheet(f"color:{C_MUTED}; border:none; font-size:12px;")
        size_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        lay.addWidget(size_lbl)

    def _fmt(self, n: int) -> str:
        for u in ("B", "KB", "MB"):
            if n < 1024: return f"{n:.1f} {u}"
            n /= 1024
        return f"{n:.1f} GB"


class MessageLog(QScrollArea):
    def __init__(self):
        super().__init__()
        self.setWidgetResizable(True)
        self._container = QWidget()
        self._layout = QVBoxLayout(self._container)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(2)
        self._layout.addStretch()
        self.setWidget(self._container)
        self.setStyleSheet(
            f"background:{C_INPUT}; border:1px solid {C_BORDER}; border-radius:4px;"
        )

    def append(self, text: str):
        ts = datetime.now().strftime("%H:%M:%S")
        lbl = QLabel(f"<span style='color:{C_MUTED}'>{ts}</span>  {text}")
        lbl.setWordWrap(True)
        lbl.setStyleSheet(f"color:{C_TEXT}; padding:1px 8px; border:none; font-size:12px;")
        lbl.setTextFormat(Qt.RichText)
        # Insert before the trailing stretch
        self._layout.insertWidget(self._layout.count() - 1, lbl)
        QTimer.singleShot(10, lambda: self.verticalScrollBar().setValue(
            self.verticalScrollBar().maximum()
        ))


# ── main window ───────────────────────────────────────────────────────────────

STATUS_LABELS = {
    "idle":       "IDLE",
    "connecting": "CONNECTING …",
    "connected":  "CONNECTED",
    "recording":  "RECORDING",
    "saving":     "SAVING",
    "error":      "ERROR",
}

STATUS_COLORS = {
    "idle":       C_MUTED,
    "connecting": C_AMBER,
    "connected":  C_GREEN,
    "recording":  C_GREEN,
    "saving":     C_AMBER,
    "error":      C_RED,
}


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("BIN Block Receiver")
        self.setMinimumSize(860, 620)
        self._thread: ReceiverThread | None = None
        self._start_time: float | None = None
        self._block_count = 0
        self._byte_total = 0

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick_elapsed)
        self._timer.start(1000)

        self._build_ui()

    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        root_lay = QHBoxLayout(root)
        root_lay.setContentsMargins(0, 0, 0, 0)
        root_lay.setSpacing(0)

        # ── left sidebar ──────────────────────────────────────────────────────
        sidebar = QWidget()
        sidebar.setFixedWidth(260)
        sidebar.setStyleSheet(f"background:{C_PANEL}; border-right:1px solid {C_BORDER};")
        sb_lay = QVBoxLayout(sidebar)
        sb_lay.setContentsMargins(20, 20, 20, 20)
        sb_lay.setSpacing(16)

        # App title
        title = QLabel("BIN BLOCK\nRECEIVER")
        title.setStyleSheet(
            f"color:{C_AMBER}; font-size:18px; font-weight:bold; letter-spacing:2px; border:none;"
        )
        title.setAlignment(Qt.AlignLeft)
        sb_lay.addWidget(title)

        subtitle = QLabel("ArduPilot MAVLink log streaming")
        subtitle.setStyleSheet(f"color:{C_MUTED}; font-size:11px; border:none;")
        subtitle.setWordWrap(True)
        sb_lay.addWidget(subtitle)

        sb_lay.addWidget(Divider())

        # UDP address
        sb_lay.addWidget(self._field_label("UDP ADDRESS"))
        self._udp_input = QLineEdit("14550")
        self._udp_input.setPlaceholderText("host:port  or  port")
        sb_lay.addWidget(self._udp_input)

        # Output directory
        sb_lay.addWidget(self._field_label("OUTPUT DIRECTORY"))
        dir_row = QWidget()
        dir_row.setStyleSheet("background:transparent;")
        dir_lay = QHBoxLayout(dir_row)
        dir_lay.setContentsMargins(0, 0, 0, 0)
        dir_lay.setSpacing(6)
        self._dir_input = QLineEdit(str(Path.home() / "bin_logs"))
        dir_lay.addWidget(self._dir_input, 1)
        browse_btn = QPushButton("…")
        browse_btn.setFixedWidth(34)
        browse_btn.clicked.connect(self._browse_dir)
        dir_lay.addWidget(browse_btn)
        sb_lay.addWidget(dir_row)

        sb_lay.addWidget(Divider())

        # Connect / disconnect
        self._connect_btn = QPushButton("CONNECT")
        self._connect_btn.setObjectName("primary")
        self._connect_btn.clicked.connect(self._toggle_connection)
        sb_lay.addWidget(self._connect_btn)

        self._stop_btn = QPushButton("DISCONNECT")
        self._stop_btn.setObjectName("danger")
        self._stop_btn.setEnabled(False)
        self._stop_btn.clicked.connect(self._disconnect)
        sb_lay.addWidget(self._stop_btn)

        sb_lay.addWidget(Divider())

        # FC param hint
        hint = QLabel(
            "FC PARAM\n"
            "LOG_BACKEND_TYPE\n"
            "  2 → MAVLink only\n"
            "  3 → SD + MAVLink"
        )
        hint.setStyleSheet(
            f"color:{C_MUTED}; font-size:11px; line-height:160%; border:none;"
        )
        hint.setWordWrap(True)
        sb_lay.addWidget(hint)

        sb_lay.addStretch()

        version = QLabel("Remote Aerospace")
        version.setStyleSheet(f"color:{C_MUTED}; font-size:10px; border:none;")
        sb_lay.addWidget(version)

        root_lay.addWidget(sidebar)

        # ── main panel ────────────────────────────────────────────────────────
        main = QWidget()
        main.setStyleSheet("background:transparent;")
        main_lay = QVBoxLayout(main)
        main_lay.setContentsMargins(24, 24, 24, 24)
        main_lay.setSpacing(20)

        # Status row
        status_row = QWidget()
        status_row.setStyleSheet("background:transparent;")
        sr_lay = QHBoxLayout(status_row)
        sr_lay.setContentsMargins(0, 0, 0, 0)
        sr_lay.setSpacing(20)
        sr_lay.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)

        self._lamp = StatusLamp()
        sr_lay.addWidget(self._lamp)

        status_text = QWidget()
        status_text.setStyleSheet("background:transparent;")
        st_lay = QVBoxLayout(status_text)
        st_lay.setContentsMargins(0, 0, 0, 0)
        st_lay.setSpacing(4)

        self._status_label = QLabel("IDLE")
        self._status_label.setStyleSheet(
            f"color:{C_MUTED}; font-size:28px; font-weight:bold; letter-spacing:3px; border:none;"
        )
        st_lay.addWidget(self._status_label)

        self._status_sub = QLabel("Not connected")
        self._status_sub.setStyleSheet(f"color:{C_MUTED}; font-size:12px; border:none;")
        st_lay.addWidget(self._status_sub)

        sr_lay.addWidget(status_text)
        sr_lay.addStretch()
        main_lay.addWidget(status_row)

        # Stat cards
        cards_row = QWidget()
        cards_row.setStyleSheet("background:transparent;")
        cards_lay = QHBoxLayout(cards_row)
        cards_lay.setContentsMargins(0, 0, 0, 0)
        cards_lay.setSpacing(12)

        self._card_blocks  = StatCard("Blocks", "—")
        self._card_size    = StatCard("Received", "—")
        self._card_elapsed = StatCard("Elapsed", "—")
        self._card_logs    = StatCard("Logs saved", "0")

        cards_lay.addWidget(self._card_blocks)
        cards_lay.addWidget(self._card_size)
        cards_lay.addWidget(self._card_elapsed)
        cards_lay.addWidget(self._card_logs)
        main_lay.addWidget(cards_row)

        # Logs saved list
        saved_label = QLabel("SAVED LOGS")
        saved_label.setStyleSheet(
            f"color:{C_MUTED}; font-size:10px; letter-spacing:1.5px; border:none;"
        )
        main_lay.addWidget(saved_label)

        self._logs_scroll = QScrollArea()
        self._logs_scroll.setWidgetResizable(True)
        self._logs_container = QWidget()
        self._logs_layout = QVBoxLayout(self._logs_container)
        self._logs_layout.setContentsMargins(0, 0, 0, 0)
        self._logs_layout.setSpacing(6)
        self._logs_layout.addStretch()
        self._logs_scroll.setWidget(self._logs_container)
        self._logs_scroll.setStyleSheet(
            f"background:transparent; border:1px solid {C_BORDER}; border-radius:4px;"
        )
        self._logs_scroll.setMinimumHeight(120)
        self._logs_scroll.setMaximumHeight(220)
        main_lay.addWidget(self._logs_scroll)

        self._no_logs_lbl = QLabel("No logs saved this session")
        self._no_logs_lbl.setStyleSheet(
            f"color:{C_MUTED}; font-size:12px; padding:12px; border:none;"
        )
        self._no_logs_lbl.setAlignment(Qt.AlignCenter)
        self._logs_layout.insertWidget(0, self._no_logs_lbl)

        # Message log
        msg_label = QLabel("CONSOLE")
        msg_label.setStyleSheet(
            f"color:{C_MUTED}; font-size:10px; letter-spacing:1.5px; border:none;"
        )
        main_lay.addWidget(msg_label)

        self._msg_log = MessageLog()
        self._msg_log.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        main_lay.addWidget(self._msg_log, 1)

        root_lay.addWidget(main, 1)

        self._log_count = 0

    def _field_label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f"color:{C_MUTED}; font-size:10px; letter-spacing:1.2px; border:none;"
        )
        return lbl

    def _browse_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Select output directory",
                                             self._dir_input.text())
        if d:
            self._dir_input.setText(d)

    def _toggle_connection(self):
        if self._thread is None or not self._thread.isRunning():
            self._start()

    def _start(self):
        udp = self._udp_input.text().strip() or "14550"
        out = Path(self._dir_input.text().strip() or "./bin_logs").expanduser()

        self._thread = ReceiverThread(udp, out)
        self._thread.status_changed.connect(self._on_status)
        self._thread.block_received.connect(self._on_block)
        self._thread.log_saved.connect(self._on_log_saved)
        self._thread.message.connect(self._msg_log.append)
        self._thread.error.connect(self._on_error)
        self._thread.finished.connect(self._on_finished)

        self._thread.start()
        self._connect_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        self._udp_input.setEnabled(False)
        self._dir_input.setEnabled(False)

    def _disconnect(self):
        if self._thread:
            self._thread.stop()
            self._thread.wait(5000)

    def _on_status(self, state: str):
        self._lamp.set_state(state)
        self._status_label.setText(STATUS_LABELS.get(state, state.upper()))
        self._status_label.setStyleSheet(
            f"color:{STATUS_COLORS.get(state, C_TEXT)}; font-size:28px; "
            f"font-weight:bold; letter-spacing:3px; border:none;"
        )
        subs = {
            "idle":       "Not connected",
            "connecting": f"Opening {self._udp_input.text()} …",
            "connected":  f"Listening on {self._udp_input.text()}",
            "recording":  "Receiving log blocks",
            "saving":     "Writing BIN file …",
        }
        self._status_sub.setText(subs.get(state, ""))

        if state == "recording" and self._start_time is None:
            self._start_time = time.time()
            self._block_count = 0
            self._byte_total = 0
        if state in ("idle", "connected"):
            self._start_time = None

    def _on_block(self, count: int, total: int):
        self._block_count = count
        self._byte_total = total
        self._card_blocks.set_value(f"{count:,}")
        self._card_size.set_value(self._fmt(total))

    def _on_log_saved(self, path: str, size: int):
        self._log_count += 1
        self._card_logs.set_value(str(self._log_count))
        row = LogRow(path, size)
        if self._log_count == 1:
            self._no_logs_lbl.hide()
        self._logs_layout.insertWidget(self._logs_layout.count() - 1, row)
        QTimer.singleShot(10, lambda: self._logs_scroll.verticalScrollBar().setValue(
            self._logs_scroll.verticalScrollBar().maximum()
        ))

    def _on_error(self, msg: str):
        self._msg_log.append(f"<span style='color:{C_RED}'>ERROR: {msg}</span>")
        self._lamp.set_state("error")
        self._status_label.setText("ERROR")
        self._status_label.setStyleSheet(
            f"color:{C_RED}; font-size:28px; font-weight:bold; letter-spacing:3px; border:none;"
        )

    def _on_finished(self):
        self._connect_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        self._udp_input.setEnabled(True)
        self._dir_input.setEnabled(True)

    def _tick_elapsed(self):
        if self._start_time:
            s = int(time.time() - self._start_time)
            m, sec = divmod(s, 60)
            self._card_elapsed.set_value(f"{m:02d}:{sec:02d}")

    @staticmethod
    def _fmt(n: int) -> str:
        for u in ("B", "KB", "MB"):
            if n < 1024: return f"{n:.1f} {u}"
            n /= 1024
        return f"{n:.1f} GB"

    def closeEvent(self, event):
        if self._thread and self._thread.isRunning():
            self._thread.stop()
            self._thread.wait(3000)
        event.accept()


# ── entry point ───────────────────────────────────────────────────────────────

def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(APP_STYLESHEET)

    # Force dark palette so system widgets inherit correctly
    pal = QPalette()
    pal.setColor(QPalette.Window, QColor(C_BG))
    pal.setColor(QPalette.WindowText, QColor(C_TEXT))
    pal.setColor(QPalette.Base, QColor(C_INPUT))
    pal.setColor(QPalette.AlternateBase, QColor(C_PANEL))
    pal.setColor(QPalette.Text, QColor(C_TEXT))
    pal.setColor(QPalette.Button, QColor(C_PANEL))
    pal.setColor(QPalette.ButtonText, QColor(C_TEXT))
    pal.setColor(QPalette.Highlight, QColor(C_AMBER))
    pal.setColor(QPalette.HighlightedText, QColor(C_BG))
    app.setPalette(pal)

    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
