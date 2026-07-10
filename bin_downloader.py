#!/usr/bin/env python3
"""
ArduPilot BIN Block Receiver — real-time MAVLink log streaming GUI.

FC param:  LOG_BACKEND_TYPE = 2  (MAVLink only)
                             3  (SD + MAVLink)

Requires:  pip install pymavlink PySide6
"""

import sys
from pathlib import Path
from datetime import datetime

from PySide6.QtCore import Qt, QTimer, QSize
from PySide6.QtGui import (QFont, QColor, QPalette, QPainter, QBrush, QPen)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QLineEdit, QFileDialog, QScrollArea,
    QFrame, QSizePolicy,
)

try:
    from bin_receiver import ReceiverThread
except ImportError:
    from terrain_hud.bin_receiver import ReceiverThread

# ── palette ───────────────────────────────────────────────────────────────────

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
QLabel {{ background: transparent; }}
QLineEdit {{
    background-color: {C_INPUT};
    border: 1px solid {C_BORDER};
    border-radius: 4px;
    color: {C_TEXT};
    padding: 6px 10px;
    selection-background-color: {C_AMBER_D};
}}
QLineEdit:focus {{ border-color: {C_AMBER}; }}
QPushButton {{
    background-color: {C_PANEL};
    border: 1px solid {C_BORDER};
    border-radius: 4px;
    color: {C_TEXT};
    padding: 7px 16px;
    font-family: "JetBrains Mono", "Consolas", monospace;
    font-size: 13px;
}}
QPushButton:hover {{ border-color: {C_AMBER}; color: {C_AMBER}; }}
QPushButton:pressed {{ background-color: {C_AMBER_D}; }}
QPushButton:disabled {{ color: {C_MUTED}; border-color: {C_BORDER}; }}
QPushButton#primary {{
    background-color: {C_AMBER}; border-color: {C_AMBER};
    color: {C_BG}; font-weight: bold;
}}
QPushButton#primary:hover {{ background-color: #F5B840; color: {C_BG}; }}
QPushButton#primary:disabled {{
    background-color: {C_AMBER_D}; border-color: {C_AMBER_D}; color: #3A2A08;
}}
QPushButton#danger {{ border-color: {C_RED}; color: {C_RED}; }}
QPushButton#danger:hover {{ background-color: {C_RED}; color: white; }}
QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{
    background: {C_PANEL}; width: 6px; border: none;
}}
QScrollBar::handle:vertical {{
    background: {C_BORDER}; border-radius: 3px; min-height: 20px;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; }}
"""

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

# ── custom widgets ─────────────────────────────────────────────────────────────

class StatusLamp(QWidget):
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
        t = QTimer(self)
        t.timeout.connect(self._tick)
        t.start(40)

    def set_state(self, state: str):
        self._state = state
        self.update()

    def _tick(self):
        if self._state in ("recording", "connecting"):
            self._pulse += 0.06 * self._pulse_dir
            if self._pulse >= 1.0: self._pulse_dir = -1
            elif self._pulse <= 0.0: self._pulse_dir = 1
        else:
            self._pulse = 0.0
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        core_hex, glow_hex = self.COLORS.get(self._state, self.COLORS["idle"])
        cx, cy, r = self.width() // 2, self.height() // 2, 34
        if self._state in ("recording", "connecting", "saving"):
            glow = QColor(glow_hex)
            glow.setAlpha(int(30 + 60 * self._pulse))
            p.setBrush(QBrush(glow))
            p.setPen(Qt.NoPen)
            gr = r + 10 + int(8 * self._pulse)
            p.drawEllipse(cx - gr, cy - gr, gr * 2, gr * 2)
        p.setBrush(QBrush(QColor(core_hex)))
        p.setPen(QPen(QColor(glow_hex), 2))
        p.drawEllipse(cx - r, cy - r, r * 2, r * 2)
        inner = QColor(glow_hex)
        inner.setAlpha(160)
        p.setBrush(QBrush(inner))
        p.setPen(Qt.NoPen)
        ir = r - 8
        p.drawEllipse(cx - ir, cy - ir, ir * 2, ir * 2)
        p.end()


class StatCard(QWidget):
    def __init__(self, label: str, initial: str = "—"):
        super().__init__()
        self.setStyleSheet(
            f"background:{C_PANEL}; border:1px solid {C_BORDER}; border-radius:6px;"
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(4)
        lbl = QLabel(label.upper())
        lbl.setStyleSheet(f"color:{C_MUTED}; font-size:10px; letter-spacing:1.5px; border:none;")
        lay.addWidget(lbl)
        self._val = QLabel(initial)
        self._val.setStyleSheet(f"color:{C_AMBER}; font-size:22px; font-weight:bold; border:none;")
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
        name_lbl = QLabel(Path(path).name)
        name_lbl.setStyleSheet(f"color:{C_TEXT}; border:none;")
        lay.addWidget(name_lbl, 1)
        size_lbl = QLabel(self._fmt(size))
        size_lbl.setStyleSheet(f"color:{C_MUTED}; border:none; font-size:12px;")
        size_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        lay.addWidget(size_lbl)

    @staticmethod
    def _fmt(n: int) -> str:
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
        self._layout.insertWidget(self._layout.count() - 1, lbl)
        QTimer.singleShot(10, lambda: self.verticalScrollBar().setValue(
            self.verticalScrollBar().maximum()
        ))

# ── main window ───────────────────────────────────────────────────────────────

import time

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("BIN Block Receiver")
        self.setMinimumSize(860, 620)
        self._thread: ReceiverThread | None = None
        self._start_time: float | None = None
        self._log_count = 0

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

        # Sidebar
        sidebar = QWidget()
        sidebar.setFixedWidth(260)
        sidebar.setStyleSheet(f"background:{C_PANEL}; border-right:1px solid {C_BORDER};")
        sb = QVBoxLayout(sidebar)
        sb.setContentsMargins(20, 20, 20, 20)
        sb.setSpacing(16)

        title = QLabel("BIN BLOCK\nRECEIVER")
        title.setStyleSheet(f"color:{C_AMBER}; font-size:18px; font-weight:bold; letter-spacing:2px; border:none;")
        sb.addWidget(title)

        subtitle = QLabel("ArduPilot MAVLink log streaming")
        subtitle.setStyleSheet(f"color:{C_MUTED}; font-size:11px; border:none;")
        sb.addWidget(subtitle)

        sb.addWidget(self._divider())

        sb.addWidget(self._field_label("UDP ADDRESS"))
        self._udp_input = QLineEdit("14550")
        self._udp_input.setPlaceholderText("host:port  or  port")
        sb.addWidget(self._udp_input)

        sb.addWidget(self._field_label("OUTPUT DIRECTORY"))
        dir_row = QWidget(); dir_row.setStyleSheet("background:transparent;")
        dr = QHBoxLayout(dir_row); dr.setContentsMargins(0,0,0,0); dr.setSpacing(6)
        self._dir_input = QLineEdit(str(Path.home() / "bin_logs"))
        dr.addWidget(self._dir_input, 1)
        browse = QPushButton("…"); browse.setFixedWidth(34)
        browse.clicked.connect(self._browse_dir)
        dr.addWidget(browse)
        sb.addWidget(dir_row)

        sb.addWidget(self._divider())

        self._connect_btn = QPushButton("CONNECT")
        self._connect_btn.setObjectName("primary")
        self._connect_btn.clicked.connect(self._start)
        sb.addWidget(self._connect_btn)

        self._stop_btn = QPushButton("DISCONNECT")
        self._stop_btn.setObjectName("danger")
        self._stop_btn.setEnabled(False)
        self._stop_btn.clicked.connect(self._disconnect)
        sb.addWidget(self._stop_btn)

        sb.addWidget(self._divider())

        hint = QLabel("FC PARAM\nLOG_BACKEND_TYPE\n  2 → MAVLink only\n  3 → SD + MAVLink")
        hint.setStyleSheet(f"color:{C_MUTED}; font-size:11px; line-height:160%; border:none;")
        sb.addWidget(hint)
        sb.addStretch()
        sb.addWidget(QLabel("Remote Aerospace") if False else self._footer())
        root_lay.addWidget(sidebar)

        # Main panel
        main = QWidget()
        main.setStyleSheet("background:transparent;")
        ml = QVBoxLayout(main)
        ml.setContentsMargins(24, 24, 24, 24)
        ml.setSpacing(20)

        # Status row
        sr = QWidget(); sr.setStyleSheet("background:transparent;")
        srl = QHBoxLayout(sr); srl.setContentsMargins(0,0,0,0); srl.setSpacing(20)
        srl.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self._lamp = StatusLamp()
        srl.addWidget(self._lamp)
        stc = QWidget(); stc.setStyleSheet("background:transparent;")
        stcl = QVBoxLayout(stc); stcl.setContentsMargins(0,0,0,0); stcl.setSpacing(4)
        self._status_label = QLabel("IDLE")
        self._status_label.setStyleSheet(
            f"color:{C_MUTED}; font-size:28px; font-weight:bold; letter-spacing:3px; border:none;"
        )
        self._status_sub = QLabel("Not connected")
        self._status_sub.setStyleSheet(f"color:{C_MUTED}; font-size:12px; border:none;")
        stcl.addWidget(self._status_label)
        stcl.addWidget(self._status_sub)
        srl.addWidget(stc)
        srl.addStretch()
        ml.addWidget(sr)

        # Stat cards
        cr = QWidget(); cr.setStyleSheet("background:transparent;")
        crl = QHBoxLayout(cr); crl.setContentsMargins(0,0,0,0); crl.setSpacing(12)
        self._card_blocks  = StatCard("Blocks", "—")
        self._card_size    = StatCard("Received", "—")
        self._card_elapsed = StatCard("Elapsed", "—")
        self._card_logs    = StatCard("Logs saved", "0")
        for c in (self._card_blocks, self._card_size, self._card_elapsed, self._card_logs):
            crl.addWidget(c)
        ml.addWidget(cr)

        # Saved logs
        ml.addWidget(self._section_label("SAVED LOGS"))
        self._logs_scroll = QScrollArea(); self._logs_scroll.setWidgetResizable(True)
        self._logs_container = QWidget()
        self._logs_layout = QVBoxLayout(self._logs_container)
        self._logs_layout.setContentsMargins(0,0,0,0); self._logs_layout.setSpacing(6)
        self._logs_layout.addStretch()
        self._logs_scroll.setWidget(self._logs_container)
        self._logs_scroll.setStyleSheet(
            f"background:transparent; border:1px solid {C_BORDER}; border-radius:4px;"
        )
        self._logs_scroll.setMinimumHeight(100); self._logs_scroll.setMaximumHeight(200)
        self._no_logs_lbl = QLabel("No logs saved this session")
        self._no_logs_lbl.setStyleSheet(f"color:{C_MUTED}; font-size:12px; padding:12px; border:none;")
        self._no_logs_lbl.setAlignment(Qt.AlignCenter)
        self._logs_layout.insertWidget(0, self._no_logs_lbl)
        ml.addWidget(self._logs_scroll)

        # Console
        ml.addWidget(self._section_label("CONSOLE"))
        self._msg_log = MessageLog()
        self._msg_log.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        ml.addWidget(self._msg_log, 1)

        root_lay.addWidget(main, 1)

    # ── helpers ──────────────────────────────────────────────────────────────

    def _divider(self):
        f = QFrame(); f.setFrameShape(QFrame.HLine)
        f.setStyleSheet(f"color:{C_BORDER}; background:{C_BORDER};"); f.setFixedHeight(1)
        return f

    def _field_label(self, text):
        l = QLabel(text)
        l.setStyleSheet(f"color:{C_MUTED}; font-size:10px; letter-spacing:1.2px; border:none;")
        return l

    def _section_label(self, text):
        l = QLabel(text)
        l.setStyleSheet(f"color:{C_MUTED}; font-size:10px; letter-spacing:1.5px; border:none;")
        return l

    def _footer(self):
        l = QLabel("Remote Aerospace")
        l.setStyleSheet(f"color:{C_MUTED}; font-size:10px; border:none;")
        return l

    @staticmethod
    def _fmt(n: int) -> str:
        for u in ("B", "KB", "MB"):
            if n < 1024: return f"{n:.1f} {u}"
            n /= 1024
        return f"{n:.1f} GB"

    # ── actions ───────────────────────────────────────────────────────────────

    def _browse_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Select output directory", self._dir_input.text())
        if d: self._dir_input.setText(d)

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

    # ── slots ─────────────────────────────────────────────────────────────────

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
        if state in ("idle", "connected"):
            self._start_time = None

    def _on_block(self, count: int, total: int):
        self._card_blocks.set_value(f"{count:,}")
        self._card_size.set_value(self._fmt(total))

    def _on_log_saved(self, path: str, size: int):
        self._log_count += 1
        self._card_logs.set_value(str(self._log_count))
        if self._log_count == 1:
            self._no_logs_lbl.hide()
        row = LogRow(path, size)
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

    def closeEvent(self, event):
        if self._thread and self._thread.isRunning():
            self._thread.stop()
            self._thread.wait(3000)
        event.accept()


# ── entry point ───────────────────────────────────────────────────────────────

def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(APP_STYLESHEET)
    pal = QPalette()
    pal.setColor(QPalette.Window,          QColor(C_BG))
    pal.setColor(QPalette.WindowText,      QColor(C_TEXT))
    pal.setColor(QPalette.Base,            QColor(C_INPUT))
    pal.setColor(QPalette.AlternateBase,   QColor(C_PANEL))
    pal.setColor(QPalette.Text,            QColor(C_TEXT))
    pal.setColor(QPalette.Button,          QColor(C_PANEL))
    pal.setColor(QPalette.ButtonText,      QColor(C_TEXT))
    pal.setColor(QPalette.Highlight,       QColor(C_AMBER))
    pal.setColor(QPalette.HighlightedText, QColor(C_BG))
    app.setPalette(pal)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
