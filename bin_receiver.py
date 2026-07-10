"""
bin_receiver.py
---------------
MAVLink remote log block receiver — runs in a QThread and streams
REMOTE_LOG_DATA_BLOCK packets from an ArduPilot FC to a local BIN file.

FC param:  LOG_BACKEND_TYPE = 2 (MAVLink only) or 3 (SD + MAVLink)
"""

from __future__ import annotations

import threading
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QThread, Signal

try:
    from pymavlink import mavutil
except ImportError:
    mavutil = None  # type: ignore

BLOCK_SIZE = 200
MAV_REMOTE_LOG_DATA_BLOCK_STOP  = 0xFFFFFFFF
MAV_REMOTE_LOG_DATA_BLOCK_START = 0xFFFFFFFE
ACK = 1


class ReceiverThread(QThread):
    status_changed = Signal(str)    # "idle" | "connecting" | "connected" | "recording" | "saving"
    block_received = Signal(int, int)  # (block_count, byte_total)
    log_saved      = Signal(str, int)  # (file_path, byte_size)
    message        = Signal(str)
    error          = Signal(str)

    def __init__(self, udp: str, out_dir: Path, idle_timeout: int = 10):
        super().__init__()
        self._udp = udp
        self._out = out_dir
        self._idle_timeout = idle_timeout
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        if mavutil is None:
            self.error.emit("pymavlink is not installed — run: pip install pymavlink")
            self.status_changed.emit("idle")
            return

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
            mav.close()
            return
        if not hb:
            self.error.emit("No heartbeat received — check FC is sending MAVLink to this address.")
            self.status_changed.emit("idle")
            mav.close()
            return

        self.message.emit(f"Heartbeat from sys={mav.target_system}")
        self.status_changed.emit("connected")

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

            self.block_received.emit(len(blocks), (highest_seq + 1) * BLOCK_SIZE)

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
