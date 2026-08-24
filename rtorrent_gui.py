#!/usr/bin/env python3
"""
rtorrent-qt-gui
===============
A PyQt5 GUI for monitoring and controlling an rtorrent instance that runs
inside `screen` on a remote server, reached over your existing passwordless
SSH key auth.

It does NOT scrape the ncurses screen session. Instead it talks to rtorrent's
XML-RPC interface (over SCGI) through an SSH port/socket-forward, which is
the standard, reliable way external tools control rtorrent (ruTorrent,
pyrocore, etc. all do the same thing).

See README.md for the one-time rtorrent.rc change needed on the server.

Dependencies (Debian):
    sudo apt install python3-pyqt5 openssh-client

Run:
    python3 rtorrent_gui.py
"""

import sys
import os
import json
import socket
import shlex
import time
import subprocess
import xmlrpc.client as xmlrpclib
from datetime import timedelta
from pathlib import Path

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLineEdit, QPushButton, QTableWidget, QTableWidgetItem, QHeaderView,
    QMessageBox, QSpinBox, QDialog, QDialogButtonBox, QLabel, QComboBox,
    QMenu, QAction, QStatusBar, QAbstractItemView
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer, QProcess

CONFIG_DIR = Path.home() / ".config" / "rtorrent-qt-gui"
CONFIG_FILE = CONFIG_DIR / "config.json"

DEFAULT_CONFIG = {
    "mode": "unix",              # "unix" or "tcp"
    "ssh_host": "",
    "ssh_port": 22,
    "ssh_user": "",
    "remote_socket_path": "",    # for unix mode, e.g. /home/user/.rtorrent.sock
    "remote_host": "127.0.0.1",  # for tcp mode
    "remote_port": 5000,         # for tcp mode
    "local_socket_path": str(Path.home() / ".cache" / "rtorrent-qt-gui" / "tunnel.sock"),
    "local_tcp_port": 15000,
    "poll_interval": 3,
    "disk_path": "/",  # remote path to report free disk space for
}


def load_config():
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r") as f:
                data = json.load(f)
            cfg = dict(DEFAULT_CONFIG)
            cfg.update(data)
            return cfg
        except (json.JSONDecodeError, OSError):
            pass
    return dict(DEFAULT_CONFIG)


def save_config(cfg):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


# --------------------------------------------------------------------------
# SCGI / XML-RPC client
# --------------------------------------------------------------------------

class RPCError(Exception):
    pass


def _scgi_wrap(body: bytes) -> bytes:
    headers = [("CONTENT_LENGTH", str(len(body))), ("SCGI", "1")]
    header_str = "".join(f"{k}\0{v}\0" for k, v in headers)
    return f"{len(header_str)}:{header_str}".encode("utf-8") + b"," + body


class RTorrentRPC:
    """Minimal XML-RPC-over-SCGI client for rtorrent, talking to a local
    unix socket or local TCP port (the near end of an SSH tunnel)."""

    def __init__(self, mode, unix_path=None, tcp_host=None, tcp_port=None, timeout=8):
        self.mode = mode
        self.unix_path = unix_path
        self.tcp_host = tcp_host
        self.tcp_port = tcp_port
        self.timeout = timeout

    def _connect(self):
        if self.mode == "unix":
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(self.timeout)
            s.connect(self.unix_path)
        else:
            s = socket.create_connection((self.tcp_host, self.tcp_port), timeout=self.timeout)
        return s

    def call(self, method, *args):
        body = xmlrpclib.dumps(args, methodname=method).encode("utf-8")
        request = _scgi_wrap(body)
        sock = self._connect()
        try:
            sock.sendall(request)
            try:
                sock.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            chunks = []
            while True:
                data = sock.recv(65536)
                if not data:
                    break
                chunks.append(data)
        finally:
            sock.close()
        raw = b"".join(chunks)
        sep = raw.find(b"\r\n\r\n")
        if sep == -1:
            raise RPCError("Malformed SCGI response from rtorrent")
        body = raw[sep + 4:]
        try:
            result, _ = xmlrpclib.loads(body)
        except xmlrpclib.Fault as e:
            raise RPCError(f"rtorrent RPC fault: {e}")
        return result[0] if result else None


# Fields pulled per torrent, in a fixed order matched to KEYS below.
FIELDS = [
    "d.hash=",
    "d.name=",
    "d.state=",
    "d.is_active=",
    "d.is_hash_checking=",
    "d.complete=",
    "d.down.rate=",
    "d.up.rate=",
    "d.peers_connected=",
    "d.peers_not_connected=",
    "d.ratio=",
    "d.size_bytes=",
    "d.bytes_done=",
    "d.down.total=",
    "d.up.total=",
    "d.message=",
    "d.base_path=",
]
KEYS = [
    "hash", "name", "state", "is_active", "is_hash_checking", "complete",
    "down_rate", "up_rate", "peers_connected", "peers_not_connected",
    "ratio", "size_bytes", "bytes_done", "down_total", "up_total", "message",
    "base_path",
]


def fetch_torrents(rpc: RTorrentRPC):
    try:
        rows = rpc.call("d.multicall2", "", "main", *FIELDS)
    except RPCError:
        # Fallback for older rtorrent versions without d.multicall2
        rows = rpc.call("d.multicall", "main", *FIELDS)
    torrents = []
    for row in rows:
        torrents.append(dict(zip(KEYS, row)))
    return torrents


def human_bytes(n):
    n = float(n)
    for unit in ["B", "KiB", "MiB", "GiB", "TiB"]:
        if n < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TiB"


def human_speed(n):
    return human_bytes(n) + "/s"


def compute_status(t):
    if t["message"]:
        return "Error"
    if t["is_hash_checking"]:
        return "Checking"
    # Check active/complete state BEFORE looking at d.message: rtorrent does
    # not reliably clear that field once a torrent recovers, so a torrent
    # that's happily seeding or downloading right now can still be carrying
    # a stale tracker/IO message from hours ago. Only treat it as an error
    # once we know the torrent isn't actually active.
    if t["is_active"] and t["state"]:
        return "Seeding" if t["complete"] else "Downloading"
    if t["message"]:
        return "Error"
    return "Paused"


def compute_eta_seconds(t):
    if t["complete"] or t["down_rate"] <= 0:
        return None
    remaining = max(t["size_bytes"] - t["bytes_done"], 0)
    return remaining / t["down_rate"]


def format_eta(seconds):
    if seconds is None:
        return "-"
    try:
        return str(timedelta(seconds=int(seconds)))
    except (OverflowError, ValueError):
        return "-"


# --------------------------------------------------------------------------
# SSH tunnel management
# --------------------------------------------------------------------------

class SSHTunnel:
    def __init__(self, cfg):
        self.cfg = cfg
        self.process = QProcess()
        self.process.setProcessChannelMode(QProcess.MergedChannels)
        self.process.readyReadStandardOutput.connect(self._on_ready_read)
        self.process.errorOccurred.connect(self._on_error_occurred)
        self._output = bytearray()
        self.command_str = ""

    def _on_ready_read(self):
        self._output += bytes(self.process.readAllStandardOutput())

    def _on_error_occurred(self, err):
        # e.g. QProcess.FailedToStart if the ssh binary isn't on PATH
        names = {
            QProcess.FailedToStart: "ssh failed to start (is openssh-client installed and on PATH?)",
            QProcess.Crashed: "ssh process crashed",
            QProcess.Timedout: "ssh process timed out",
        }
        msg = names.get(err, f"ssh process error ({err})")
        self._output += (msg + "\n").encode()

    def start(self):
        cfg = self.cfg
        self._output = bytearray()
        if cfg["mode"] == "unix":
            local_path = cfg["local_socket_path"]
            Path(local_path).parent.mkdir(parents=True, exist_ok=True)
            try:
                if Path(local_path).exists():
                    os.remove(local_path)
            except OSError:
                pass
            forward = f"{local_path}:{cfg['remote_socket_path']}"
        else:
            forward = f"127.0.0.1:{cfg['local_tcp_port']}:{cfg['remote_host']}:{cfg['remote_port']}"

        args = [
            "-N",
            "-v",
            "-o", "BatchMode=yes",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=3",
            "-o", "StrictHostKeyChecking=accept-new",
            "-p", str(cfg["ssh_port"]),
            "-L", forward,
            f"{cfg['ssh_user']}@{cfg['ssh_host']}",
        ]
        self.command_str = "ssh " + " ".join(args)
        self.process.start("ssh", args)

    def stop(self):
        if self.process.state() != QProcess.NotRunning:
            self.process.terminate()
            if not self.process.waitForFinished(3000):
                self.process.kill()
                self.process.waitForFinished(1000)

    def is_running(self):
        return self.process.state() != QProcess.NotRunning

    def output_text(self):
        return self._output.decode(errors="replace")

    def wait_ready(self, cfg, timeout=10):
        """Poll the local forward endpoint until it accepts a connection.
        Pumps the Qt event loop each iteration so QProcess actually receives
        the child's exit status and buffered stderr while we wait."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            QApplication.processEvents()
            if self.process.state() == QProcess.NotRunning:
                return False
            try:
                if cfg["mode"] == "unix":
                    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    s.settimeout(1)
                    s.connect(cfg["local_socket_path"])
                else:
                    s = socket.create_connection(("127.0.0.1", cfg["local_tcp_port"]), timeout=1)
                s.close()
                return True
            except OSError:
                time.sleep(0.2)
        return False


# --------------------------------------------------------------------------
# Polling worker
# --------------------------------------------------------------------------

class PollWorker(QThread):
    data_ready = pyqtSignal(list)
    error = pyqtSignal(str)

    def __init__(self, rpc):
        super().__init__()
        self.rpc = rpc

    def run(self):
        try:
            torrents = fetch_torrents(self.rpc)
            self.data_ready.emit(torrents)
        except Exception as e:
            self.error.emit(str(e))


class ActionWorker(QThread):
    finished_ok = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, rpc, method, hashes):
        super().__init__()
        self.rpc = rpc
        self.method = method
        self.hashes = hashes

    def run(self):
        try:
            for h in self.hashes:
                self.rpc.call(self.method, h)
            self.finished_ok.emit()
        except Exception as e:
            self.error.emit(str(e))


class AddMagnetWorker(QThread):
    finished_ok = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, rpc, magnet_uri, start=True):
        super().__init__()
        self.rpc = rpc
        self.magnet_uri = magnet_uri
        # NOTE: do not name this self.start — QThread already has a start()
        # method to launch the thread, and assigning a plain attribute with
        # that name shadows it, breaking worker.start() with
        # "TypeError: 'bool' object is not callable".
        self.autostart = start

    def run(self):
        try:
            # load.start adds the torrent and begins downloading immediately;
            # load.normal adds it stopped so it can be started later.
            method = "load.start" if self.autostart else "load.normal"
            self.rpc.call(method, "", self.magnet_uri)
            self.finished_ok.emit()
        except Exception as e:
            self.error.emit(str(e))


class DeleteWorker(QThread):
    """Erases torrents from rtorrent, and optionally deletes the downloaded
    files from disk over a plain SSH command (not the RPC tunnel) using the
    exact path rtorrent itself reports via d.base_path."""

    progress = pyqtSignal(str)
    finished_ok = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, rpc, cfg, items, delete_files):
        super().__init__()
        self.rpc = rpc
        self.cfg = cfg
        self.items = items  # list of dicts: hash, name, base_path
        self.delete_files = delete_files

    def run(self):
        cfg = self.cfg
        try:
            for item in self.items:
                name = item["name"]
                base_path = (item.get("base_path") or "").strip()

                if self.delete_files:
                    if not base_path.startswith("/") or base_path in ("/", ""):
                        raise RuntimeError(
                            f"Refusing to delete '{name}': rtorrent did not report a "
                            f"safe absolute path (got {base_path!r})."
                        )

                self.progress.emit(f"Removing {name}...")
                self.rpc.call("d.erase", item["hash"])

                if self.delete_files:
                    self.progress.emit(f"Deleting files for {name}...")
                    cmd = [
                        "ssh",
                        "-o", "BatchMode=yes",
                        "-o", "ConnectTimeout=10",
                        "-o", "StrictHostKeyChecking=accept-new",
                        "-p", str(cfg["ssh_port"]),
                        f"{cfg['ssh_user']}@{cfg['ssh_host']}",
                        f"rm -rf -- {shlex.quote(base_path)}",
                    ]
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                    if result.returncode != 0:
                        raise RuntimeError(
                            f"Removed '{name}' from rtorrent, but deleting its files failed: "
                            f"{result.stderr.strip() or 'unknown error'}"
                        )
            self.finished_ok.emit()
        except Exception as e:
            self.error.emit(str(e))


class DiskSpaceWorker(QThread):
    """Reports free/total disk space for a path on the remote server via a
    plain `df` over SSH. rtorrent's RPC interface doesn't reliably expose
    this across versions, so a direct shell command is more dependable."""

    # NOTE: pyqtSignal(int, int) would marshal these as a 32-bit signed C
    # int (max ~2.1 GB), which silently overflows/wraps for any disk with
    # more than ~2 GB free — exactly the bug that caused wildly wrong
    # numbers here. `object` preserves Python's arbitrary-precision int.
    result_ready = pyqtSignal(object, object)  # avail_bytes, total_bytes
    error = pyqtSignal(str)

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg

    def run(self):
        cfg = self.cfg
        path = (cfg.get("disk_path") or "/").strip() or "/"
        remote_cmd = f"df -B1 --output=avail,size -- {shlex.quote(path)}"
        cmd = [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=10",
            "-o", "StrictHostKeyChecking=accept-new",
            "-p", str(cfg["ssh_port"]),
            f"{cfg['ssh_user']}@{cfg['ssh_host']}",
            remote_cmd,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or "df command failed")
            # df prints a header row even with --output; the data is the
            # last non-empty line (defensive against unexpected extra lines).
            lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
            if len(lines) < 2:
                raise RuntimeError(f"Unexpected df output: {result.stdout!r}")
            parts = lines[-1].split()
            if len(parts) < 2:
                raise RuntimeError(f"Unexpected df data line: {lines[-1]!r}")
            avail, total = int(parts[0]), int(parts[1])
            self.result_ready.emit(avail, total)
        except Exception as e:
            self.error.emit(str(e))


# --------------------------------------------------------------------------
# Settings dialog
# --------------------------------------------------------------------------

class SettingsDialog(QDialog):
    def __init__(self, parent, cfg):
        super().__init__(parent)
        self.setWindowTitle("Connection Settings")
        self.cfg = dict(cfg)

        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["Unix socket (recommended)", "TCP port"])
        self.mode_combo.setCurrentIndex(0 if cfg["mode"] == "unix" else 1)
        self.mode_combo.currentIndexChanged.connect(self.update_mode_fields)

        self.ssh_host_edit = QLineEdit(cfg["ssh_host"])
        self.ssh_port_spin = QSpinBox()
        self.ssh_port_spin.setRange(1, 65535)
        self.ssh_port_spin.setValue(cfg["ssh_port"])
        self.ssh_user_edit = QLineEdit(cfg["ssh_user"])

        self.remote_socket_edit = QLineEdit(cfg["remote_socket_path"])
        self.remote_socket_edit.setPlaceholderText("/home/youruser/.rtorrent.sock")

        self.remote_host_edit = QLineEdit(cfg["remote_host"])
        self.remote_port_spin = QSpinBox()
        self.remote_port_spin.setRange(1, 65535)
        self.remote_port_spin.setValue(cfg["remote_port"])

        self.poll_spin = QSpinBox()
        self.poll_spin.setRange(1, 60)
        self.poll_spin.setValue(cfg["poll_interval"])
        self.poll_spin.setSuffix(" s")

        self.disk_path_edit = QLineEdit(cfg.get("disk_path", "/"))
        self.disk_path_edit.setPlaceholderText("/ (or a mount point, e.g. /mnt/downloads)")

        form = QFormLayout()
        form.addRow("RPC transport:", self.mode_combo)
        form.addRow(QLabel("<b>SSH</b>"))
        form.addRow("Host / IP:", self.ssh_host_edit)
        form.addRow("SSH port:", self.ssh_port_spin)
        form.addRow("Username:", self.ssh_user_edit)
        form.addRow(QLabel("<b>rtorrent RPC (unix socket)</b>"))
        form.addRow("Remote socket path:", self.remote_socket_edit)
        form.addRow(QLabel("<b>rtorrent RPC (TCP, only if not using unix socket)</b>"))
        form.addRow("Remote bind host:", self.remote_host_edit)
        form.addRow("Remote port:", self.remote_port_spin)
        form.addRow(QLabel("<b>Polling</b>"))
        form.addRow("Refresh every:", self.poll_spin)
        form.addRow("Disk usage path:", self.disk_path_edit)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)
        self.update_mode_fields()

    def update_mode_fields(self):
        is_unix = self.mode_combo.currentIndex() == 0
        self.remote_socket_edit.setEnabled(is_unix)
        self.remote_host_edit.setEnabled(not is_unix)
        self.remote_port_spin.setEnabled(not is_unix)

    def get_config(self):
        cfg = dict(self.cfg)
        cfg["mode"] = "unix" if self.mode_combo.currentIndex() == 0 else "tcp"
        cfg["ssh_host"] = self.ssh_host_edit.text().strip()
        cfg["ssh_port"] = self.ssh_port_spin.value()
        cfg["ssh_user"] = self.ssh_user_edit.text().strip()
        cfg["remote_socket_path"] = self.remote_socket_edit.text().strip()
        cfg["remote_host"] = self.remote_host_edit.text().strip()
        cfg["remote_port"] = self.remote_port_spin.value()
        cfg["poll_interval"] = self.poll_spin.value()
        cfg["disk_path"] = self.disk_path_edit.text().strip() or "/"
        return cfg


# --------------------------------------------------------------------------
# Table item with proper numeric sorting
# --------------------------------------------------------------------------

class NumericItem(QTableWidgetItem):
    def __init__(self, text, sort_value):
        super().__init__(text)
        self.sort_value = sort_value

    def __lt__(self, other):
        if isinstance(other, NumericItem):
            return self.sort_value < other.sort_value
        return super().__lt__(other)


COLUMNS = [
    "Name", "Status", "Progress", "Down Speed", "Up Speed",
    "ETA", "Peers", "Ratio", "Size", "Downloaded", "Uploaded",
]


# --------------------------------------------------------------------------
# Main window
# --------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("rtorrent GUI")
        self.resize(1150, 560)

        self.cfg = load_config()
        self.tunnel = None
        self.rpc = None
        self.poll_worker = None
        self.action_worker = None
        self.poll_fail_count = 0
        self.poll_timer = QTimer()
        self.poll_timer.timeout.connect(self.poll_now)
        self.disk_worker = None
        self.disk_timer = QTimer()
        self.disk_timer.timeout.connect(self.poll_disk_space)

        self.build_ui()
        self.update_connect_ui(connected=False)

    def build_ui(self):
        central = QWidget()
        layout = QVBoxLayout(central)

        top_row = QHBoxLayout()
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.clicked.connect(self.toggle_connection)
        settings_btn = QPushButton("Settings...")
        settings_btn.clicked.connect(self.open_settings)
        refresh_btn = QPushButton("Refresh now")
        refresh_btn.clicked.connect(self.poll_now)
        top_row.addWidget(self.connect_btn)
        top_row.addWidget(settings_btn)
        top_row.addWidget(refresh_btn)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Filter by name...")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self.apply_filter)
        self.search_edit.setMinimumWidth(220)
        top_row.addWidget(self.search_edit)
        top_row.addStretch()
        self.conn_label = QLabel("Disconnected")
        top_row.addWidget(self.conn_label)
        layout.addLayout(top_row)

        magnet_row = QHBoxLayout()
        magnet_label = QLabel("Add magnet:")
        self.magnet_edit = QLineEdit()
        self.magnet_edit.setPlaceholderText("magnet:?xt=urn:btih:...")
        self.magnet_edit.returnPressed.connect(self.add_magnet)
        add_magnet_btn = QPushButton("Add torrent")
        add_magnet_btn.clicked.connect(self.add_magnet)
        magnet_row.addWidget(magnet_label)
        magnet_row.addWidget(self.magnet_edit)
        magnet_row.addWidget(add_magnet_btn)
        layout.addLayout(magnet_row)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        base_col_width = 90
        self.table.setColumnWidth(0, base_col_width * 2)  # Name: twice the width of the rest
        for col in range(1, len(COLUMNS)):
            self.table.setColumnWidth(col, base_col_width)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSortingEnabled(True)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.show_context_menu)
        layout.addWidget(self.table)

        self.setCentralWidget(central)

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.disk_space_label = QLabel("Free: \u2013")
        self.disk_space_label.setContentsMargins(0, 0, 8, 0)
        self.status_bar.addPermanentWidget(self.disk_space_label)

    # ---------------- connection lifecycle ----------------

    def toggle_connection(self):
        if self.tunnel and self.tunnel.is_running():
            self.disconnect_all()
        else:
            self.connect_all()

    def connect_all(self):
        cfg = self.cfg
        if not cfg["ssh_host"] or not cfg["ssh_user"]:
            QMessageBox.warning(self, "Missing settings", "Please fill in SSH host and username in Settings first.")
            self.open_settings()
            return
        if cfg["mode"] == "unix" and not cfg["remote_socket_path"]:
            QMessageBox.warning(self, "Missing settings", "Please set the remote socket path in Settings.")
            self.open_settings()
            return

        self.conn_label.setText("Connecting...")
        QApplication.processEvents()

        self.tunnel = SSHTunnel(cfg)
        self.tunnel.start()
        ready = self.tunnel.wait_ready(cfg, timeout=10)
        if not ready:
            # give any last buffered output a moment to arrive
            self.tunnel.process.waitForReadyRead(300)
            QApplication.processEvents()
            output = self.tunnel.output_text().strip()
            command = self.tunnel.command_str
            self.tunnel.stop()
            self.tunnel = None
            if not output:
                output = ("(no output captured — the tunnel never came up within the "
                           "timeout, but ssh didn't report an error either. This can mean "
                           "it's still negotiating, or a stale tunnel process from a previous "
                           "run is holding the local socket/port. Try running the command "
                           "below by hand in a terminal to see what actually happens.)")
            QMessageBox.warning(
                self, "Connection failed",
                "Could not establish the SSH tunnel to rtorrent's RPC socket.\n\n"
                "Check: SSH key auth works for this host, rtorrent is running with "
                "RPC enabled, and the remote socket/port path is correct.\n\n"
                f"Command run:\n{command}\n\n"
                f"ssh output:\n{output}"
            )
            self.update_connect_ui(connected=False)
            return

        if cfg["mode"] == "unix":
            self.rpc = RTorrentRPC("unix", unix_path=cfg["local_socket_path"])
        else:
            self.rpc = RTorrentRPC("tcp", tcp_host="127.0.0.1", tcp_port=cfg["local_tcp_port"])

        self.tunnel.process.finished.connect(self.on_tunnel_finished)
        self.poll_fail_count = 0
        self.update_connect_ui(connected=True)
        self.poll_now()
        self.poll_timer.start(cfg["poll_interval"] * 1000)
        self.poll_disk_space()
        self.disk_timer.start(max(cfg["poll_interval"], 30) * 1000)

    def on_tunnel_finished(self, exit_code, exit_status):
        # Fires when the ssh -N process exits on its own — network change,
        # laptop sleep, server reboot, or ServerAliveCountMax giving up on a
        # dead link. Only act on this if we still think we're connected;
        # a normal disconnect_all() already tore the tunnel down itself.
        if self.rpc is None:
            return
        output = self.tunnel.output_text().strip() if self.tunnel else ""
        self.handle_connection_lost(
            "The SSH tunnel exited unexpectedly — the connection appears to have "
            "been broken at the other end (e.g. a network change, the machine went "
            "to sleep, or the server rebooted).",
            output,
        )

    def handle_connection_lost(self, reason, detail=""):
        self.poll_timer.stop()
        self.disk_timer.stop()
        self.disk_space_label.setText("Free: \u2013")
        self.rpc = None
        if self.tunnel:
            self.tunnel.stop()
        self.tunnel = None
        self.poll_fail_count = 0
        self.update_connect_ui(connected=False)
        self.status_bar.showMessage("Connection lost", 5000)
        msg = reason
        if detail:
            msg += f"\n\nssh output:\n{detail}"
        msg += "\n\nClick Connect to reconnect."
        QMessageBox.warning(self, "Connection lost", msg)

    def disconnect_all(self):
        self.poll_timer.stop()
        self.disk_timer.stop()
        self.disk_space_label.setText("Free: \u2013")
        if self.tunnel:
            try:
                self.tunnel.process.finished.disconnect(self.on_tunnel_finished)
            except TypeError:
                pass
            self.tunnel.stop()
        self.tunnel = None
        self.rpc = None
        self.poll_fail_count = 0
        self.update_connect_ui(connected=False)

    def update_connect_ui(self, connected):
        self.connect_btn.setText("Disconnect" if connected else "Connect")
        self.conn_label.setText("Connected" if connected else "Disconnected")

    def open_settings(self):
        dlg = SettingsDialog(self, self.cfg)
        if dlg.exec_() == QDialog.Accepted:
            was_connected = self.tunnel is not None and self.tunnel.is_running()
            if was_connected:
                self.disconnect_all()
            self.cfg = dlg.get_config()
            save_config(self.cfg)
            if was_connected:
                self.connect_all()

    # ---------------- polling & display ----------------

    def poll_now(self):
        if not self.rpc:
            return
        self.poll_worker = PollWorker(self.rpc)
        self.poll_worker.data_ready.connect(self.on_data_ready)
        self.poll_worker.error.connect(self.on_poll_error)
        self.poll_worker.start()

    def poll_disk_space(self):
        if not self.tunnel or not self.tunnel.is_running():
            return
        self.disk_worker = DiskSpaceWorker(self.cfg)
        self.disk_worker.result_ready.connect(self.on_disk_space_ready)
        self.disk_worker.error.connect(self.on_disk_space_error)
        self.disk_worker.start()

    def on_disk_space_ready(self, avail, total):
        self.disk_space_label.setText(f"Free: {human_bytes(avail)} / {human_bytes(total)}")
        self.disk_space_label.setToolTip(f"Queried path: {self.cfg.get('disk_path', '/')}")

    def on_disk_space_error(self, msg):
        self.disk_space_label.setText("Free: n/a")
        self.disk_space_label.setToolTip(f"Could not read disk space: {msg}")
        self.status_bar.showMessage(f"Disk space check failed: {msg}", 5000)

    def on_poll_error(self, msg):
        self.poll_fail_count += 1
        self.status_bar.showMessage(f"Poll error: {msg}", 5000)
        if self.poll_fail_count >= 3:
            self.handle_connection_lost(
                "Lost contact with rtorrent over the tunnel — the connection appears to "
                "be broken at the other end, even though the ssh process is still "
                "running locally (e.g. after sleep or a network change).",
                msg,
            )

    def on_data_ready(self, torrents):
        self.poll_fail_count = 0
        self.torrents_by_hash = {t["hash"]: t for t in torrents}

        # Row indices shift on every refresh (rows are recreated, and sorting
        # can reorder them as values change), so remember what was selected
        # by torrent hash rather than by row position and restore it after
        # rebuilding — otherwise the highlight silently drifts onto whatever
        # torrent happens to land in that row next.
        previously_selected = set(self.selected_hashes())

        self.table.blockSignals(True)
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(torrents))

        total_down = total_up = 0
        for row, t in enumerate(torrents):
            status = compute_status(t)
            progress = (t["bytes_done"] / t["size_bytes"] * 100) if t["size_bytes"] else 0.0
            eta_s = compute_eta_seconds(t)
            ratio = t["ratio"] / 1000.0
            total_down += t["down_rate"]
            total_up += t["up_rate"]

            name_item = QTableWidgetItem(t["name"])
            name_item.setData(Qt.UserRole, t["hash"])
            self.table.setItem(row, 0, name_item)
            status_item = QTableWidgetItem(status)
            if t["message"]:
                status_item.setToolTip(t["message"])
            self.table.setItem(row, 1, status_item)
            self.table.setItem(row, 2, NumericItem(f"{progress:.1f}%", progress))
            self.table.setItem(row, 3, NumericItem(human_speed(t["down_rate"]), t["down_rate"]))
            self.table.setItem(row, 4, NumericItem(human_speed(t["up_rate"]), t["up_rate"]))
            self.table.setItem(row, 5, NumericItem(format_eta(eta_s), eta_s if eta_s is not None else float("inf")))
            self.table.setItem(row, 6, NumericItem(
                f"{t['peers_connected']} / {t['peers_not_connected']}", t["peers_connected"]))
            self.table.setItem(row, 7, NumericItem(f"{ratio:.2f}", ratio))
            self.table.setItem(row, 8, NumericItem(human_bytes(t["size_bytes"]), t["size_bytes"]))
            self.table.setItem(row, 9, NumericItem(human_bytes(t["down_total"]), t["down_total"]))
            self.table.setItem(row, 10, NumericItem(human_bytes(t["up_total"]), t["up_total"]))

        self.table.setSortingEnabled(True)

        if previously_selected:
            self.table.clearSelection()
            for row in range(self.table.rowCount()):
                item = self.table.item(row, 0)
                if item and item.data(Qt.UserRole) in previously_selected:
                    self.table.selectRow(row)

        self.table.blockSignals(False)

        self.apply_filter()
        visible = sum(1 for row in range(self.table.rowCount()) if not self.table.isRowHidden(row))
        count_text = f"{len(torrents)} torrents" if visible == len(torrents) else f"{visible} of {len(torrents)} torrents"
        self.status_bar.showMessage(
            f"{count_text}  |  Total: \u2193 {human_speed(total_down)}  "
            f"\u2191 {human_speed(total_up)}"
        )

    # ---------------- actions ----------------

    def apply_filter(self):
        query = self.search_edit.text().strip().lower()
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            name = item.text().lower() if item else ""
            self.table.setRowHidden(row, bool(query) and query not in name)

    def selected_hashes(self):
        rows = {idx.row() for idx in self.table.selectedIndexes()}
        hashes = []
        for row in rows:
            item = self.table.item(row, 0)
            if item:
                hashes.append(item.data(Qt.UserRole))
        return hashes

    def show_context_menu(self, pos):
        if not self.rpc:
            return
        hashes = self.selected_hashes()
        if not hashes:
            return
        menu = QMenu(self)
        start_action = menu.addAction("Start")
        stop_action = menu.addAction("Stop (pause)")
        menu.addSeparator()
        erase_action = menu.addAction("Remove from list (keep files)")
        delete_action = menu.addAction("Remove and delete files...")
        action = menu.exec_(self.table.viewport().mapToGlobal(pos))
        if action == start_action:
            self.run_action("d.start", hashes)
        elif action == stop_action:
            self.run_action("d.stop", hashes)
        elif action == erase_action:
            self.erase_torrents(hashes, delete_files=False)
        elif action == delete_action:
            self.erase_torrents(hashes, delete_files=True)

    def run_action(self, method, hashes):
        self.action_worker = ActionWorker(self.rpc, method, hashes)
        self.action_worker.finished_ok.connect(self.poll_now)
        self.action_worker.error.connect(lambda msg: self.status_bar.showMessage(f"Action failed: {msg}", 5000))
        self.action_worker.start()

    def erase_torrents(self, hashes, delete_files):
        items = []
        for h in hashes:
            t = self.torrents_by_hash.get(h)
            if not t:
                continue
            items.append({"hash": h, "name": t["name"], "base_path": t.get("base_path", "")})
        if not items:
            return

        names = "\n".join(f"  \u2022 {i['name']}" for i in items[:10])
        if len(items) > 10:
            names += f"\n  ...and {len(items) - 10} more"

        if delete_files:
            title = "Delete torrents and files?"
            text = (
                "This permanently deletes the downloaded files from the server, in "
                "addition to removing the torrent(s) from rtorrent.\n\n"
                "This cannot be undone.\n\n"
                f"{names}"
            )
        else:
            title = "Remove from list?"
            text = (
                "This removes the torrent(s) from rtorrent's list. The downloaded "
                "files stay on disk.\n\n"
                f"{names}"
            )

        confirm = QMessageBox.question(
            self, title, text, QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if confirm != QMessageBox.Yes:
            return

        self.delete_worker = DeleteWorker(self.rpc, self.cfg, items, delete_files)
        self.delete_worker.progress.connect(lambda msg: self.status_bar.showMessage(msg, 3000))
        self.delete_worker.finished_ok.connect(self.on_delete_done)
        self.delete_worker.error.connect(self.on_delete_error)
        self.delete_worker.start()

    def on_delete_done(self):
        self.status_bar.showMessage("Done", 3000)
        self.poll_now()

    def on_delete_error(self, msg):
        self.status_bar.showMessage(f"Delete failed: {msg}", 6000)
        QMessageBox.warning(self, "Delete failed", msg)

    def add_magnet(self):
        magnet = self.magnet_edit.text().strip()
        if not self.rpc:
            QMessageBox.warning(self, "Not connected", "Connect to the server first.")
            return
        if not magnet.startswith("magnet:"):
            QMessageBox.warning(self, "Invalid magnet link", "Please enter a valid magnet: link.")
            return
        self.magnet_add_worker = AddMagnetWorker(self.rpc, magnet, start=True)
        self.magnet_add_worker.finished_ok.connect(self.on_magnet_added)
        self.magnet_add_worker.error.connect(self.on_magnet_add_error)
        self.magnet_add_worker.start()

    def on_magnet_added(self):
        self.magnet_edit.clear()
        self.status_bar.showMessage("Torrent added", 4000)
        self.poll_now()

    def on_magnet_add_error(self, msg):
        self.status_bar.showMessage(f"Add torrent failed: {msg}", 6000)
        QMessageBox.warning(self, "Add torrent failed", msg)

    def closeEvent(self, event):
        self.disconnect_all()
        event.accept()


def main():
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
