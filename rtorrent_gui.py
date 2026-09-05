#!/usr/bin/env python3
"""
rtorrent-qt-gui
===============
A PyQt6 GUI for monitoring and controlling an rtorrent instance that runs
inside `screen` on a remote server, reached over your existing passwordless
SSH key auth.

It does NOT scrape the ncurses screen session. Instead it talks to rtorrent's
XML-RPC interface (over SCGI) through an SSH port/socket-forward, which is
the standard, reliable way external tools control rtorrent (ruTorrent,
pyrocore, etc. all do the same thing).

See README.md for the one-time rtorrent.rc change needed on the server.

Dependencies (Debian):
    sudo apt install python3-pyqt6 openssh-client
    # If your distro's python3-pyqt6 is too old or missing, install via pip
    # instead: pip install --break-system-packages PyQt6

Run:
    python3 rtorrent_gui.py
"""

import sys
import os
import json
import re
import socket
import shlex
import shutil
import time
import subprocess
import xmlrpc.client as xmlrpclib
from datetime import timedelta
from pathlib import Path

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLineEdit, QPushButton, QTableWidget, QTableWidgetItem, QHeaderView,
    QMessageBox, QSpinBox, QDialog, QDialogButtonBox, QLabel, QComboBox,
    QMenu, QStatusBar, QAbstractItemView, QCheckBox, QFileDialog,
    QSystemTrayIcon, QStyle, QTableView, QTabWidget, QListWidget,
    QListWidgetItem, QPlainTextEdit, QSplitter, QToolButton, QDoubleSpinBox
)
from PyQt6.QtCore import (
    Qt, QThread, pyqtSignal, QTimer, QProcess, QProcessEnvironment, QEvent,
    QAbstractTableModel, QModelIndex, QSortFilterProxyModel
)
from PyQt6.QtGui import QIcon, QAction

import copy
import uuid

# Password auth stores the secret in the OS keychain via the `keyring`
# package when it's available (pip install keyring / apt install
# python3-keyring), so the plaintext password never has to sit in
# config.json. If keyring isn't installed, we fall back to storing the
# password directly in config.json and say so in the Settings dialog.
try:
    import keyring
    KEYRING_AVAILABLE = True
except ImportError:
    keyring = None
    KEYRING_AVAILABLE = False

KEYRING_SERVICE = "rtorrent-qt-gui"

CONFIG_DIR = Path.home() / ".config" / "rtorrent-qt-gui"
CONFIG_FILE = CONFIG_DIR / "config.json"

DEFAULT_CONFIG = {
    "mode": "unix",              # "unix" or "tcp"
    "ssh_host": "",
    "ssh_port": 22,
    "ssh_user": "",
    "auth_method": "key",        # "key" (passwordless SSH key auth) or "password"
    "ssh_password_saved": False,  # True if a password is stored (keyring or, as fallback, below)
    "ssh_password": "",           # only populated when keyring is unavailable
    "remote_socket_path": "",    # for unix mode, e.g. /home/user/.rtorrent.sock
    "remote_host": "127.0.0.1",  # for tcp mode
    "remote_port": 5000,         # for tcp mode
    "local_socket_path": str(Path.home() / ".cache" / "rtorrent-qt-gui" / "tunnel.sock"),
    "local_tcp_port": 15000,
    "poll_interval": 3,
    "disk_path": "/",  # remote path to report free disk space for
    "download_dir": "",  # remote directory new torrents are placed in; blank = rtorrent's own default
    # rtorrent_autograb.py runs no web server; the GUI reads/writes its
    # config and activity log by overwriting these files over the same
    # SSH connection used for rtorrent RPC (see AutomationDialog).
    "autograb_config_path": "~/.config/rtorrent-autograb/config.json",
    "autograb_log_path": "~/.config/rtorrent-autograb/activity.json",
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
# Password auth helpers
# --------------------------------------------------------------------------

def _keyring_account(cfg):
    return f"{cfg.get('ssh_user', '')}@{cfg.get('ssh_host', '')}"


def get_saved_password(cfg):
    """Returns the saved SSH password for this host/user, or None."""
    if KEYRING_AVAILABLE:
        try:
            return keyring.get_password(KEYRING_SERVICE, _keyring_account(cfg))
        except Exception:
            return None
    return cfg.get("ssh_password") or None


def set_saved_password(cfg, password):
    """Saves (or, if password is falsy, clears) the SSH password for the
    host/user in cfg, using the OS keychain when available. Mutates cfg's
    bookkeeping fields in place; caller is responsible for save_config()."""
    account = _keyring_account(cfg)
    if KEYRING_AVAILABLE:
        try:
            if password:
                keyring.set_password(KEYRING_SERVICE, account, password)
            else:
                try:
                    keyring.delete_password(KEYRING_SERVICE, account)
                except Exception:
                    pass
        except Exception:
            pass
        cfg["ssh_password"] = ""
    else:
        cfg["ssh_password"] = password or ""
    cfg["ssh_password_saved"] = bool(password)


def sshpass_available():
    return shutil.which("sshpass") is not None


def ssh_common_opts(cfg):
    """SSH CLI options shared by every ssh invocation (tunnel, df, rm)."""
    opts = ["-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=accept-new"]
    if cfg.get("auth_method") == "password":
        # Let ssh prompt for a password (sshpass answers it) but don't sit
        # there retrying forever if it's wrong.
        opts += ["-o", "NumberOfPasswordPrompts=1"]
    else:
        opts += ["-o", "BatchMode=yes"]
    return opts


def wrap_ssh_command(cfg, cmd):
    """Given a full ['ssh', ...] argv, prefix it with `sshpass -e` when
    password auth is configured, so the password is supplied without ever
    appearing on the command line (it travels via the SSHPASS env var)."""
    if cfg.get("auth_method") == "password":
        return ["sshpass", "-e"] + cmd
    return cmd


def ssh_subprocess_env(cfg):
    """Env dict for subprocess.run() calls that need the SSH password."""
    if cfg.get("auth_method") != "password":
        return None
    password = get_saved_password(cfg)
    if not password:
        raise RuntimeError(
            "Password authentication is selected but no password is saved. "
            "Set one in Settings."
        )
    env = os.environ.copy()
    env["SSHPASS"] = password
    return env


def check_password_auth_ready(cfg):
    """Raises RuntimeError with a user-facing message if password auth is
    selected but not actually usable yet (missing sshpass or password)."""
    if cfg.get("auth_method") != "password":
        return
    if not sshpass_available():
        raise RuntimeError(
            "Password authentication requires the 'sshpass' utility, which "
            "isn't installed. Install it with: sudo apt install sshpass"
        )
    if not get_saved_password(cfg):
        raise RuntimeError(
            "Password authentication is selected but no password is saved. "
            "Set one in Settings."
        )


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


# --------------------------------------------------------------------------
# Global speed limits
# --------------------------------------------------------------------------

def fetch_global_limits(rpc: RTorrentRPC):
    """Returns (down_bytes_per_sec, up_bytes_per_sec); 0 means unlimited."""
    down = rpc.call("throttle.global_down.max_rate")
    up = rpc.call("throttle.global_up.max_rate")
    return int(down), int(up)


def set_global_limit(rpc: RTorrentRPC, direction, bytes_per_sec):
    """direction is 'down' or 'up'."""
    method = f"throttle.global_{direction}.max_rate.set"
    rpc.call(method, "", int(bytes_per_sec))


# --------------------------------------------------------------------------
# Tracker / peer info
# --------------------------------------------------------------------------

TRACKER_FIELDS = ["t.url=", "t.type=", "t.is_enabled=", "t.scrape_complete=", "t.scrape_incomplete="]
TRACKER_KEYS = ["url", "type", "is_enabled", "seeders", "leechers"]

PEER_FIELDS = [
    "p.address=", "p.port=", "p.client_version=", "p.down_rate=", "p.up_rate=",
    "p.completed_percent=", "p.is_encrypted=",
]
PEER_KEYS = [
    "address", "port", "client", "down_rate", "up_rate", "completed_percent", "is_encrypted",
]


def fetch_trackers(rpc: RTorrentRPC, torrent_hash):
    rows = rpc.call("t.multicall", torrent_hash, "", *TRACKER_FIELDS)
    return [dict(zip(TRACKER_KEYS, row)) for row in rows]


def fetch_peers(rpc: RTorrentRPC, torrent_hash):
    rows = rpc.call("p.multicall", torrent_hash, "", *PEER_FIELDS)
    return [dict(zip(PEER_KEYS, row)) for row in rows]


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
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.process.readyReadStandardOutput.connect(self._on_ready_read)
        self.process.errorOccurred.connect(self._on_error_occurred)
        self._output = bytearray()
        self.command_str = ""

    def _on_ready_read(self):
        self._output += bytes(self.process.readAllStandardOutput())

    def _on_error_occurred(self, err):
        # e.g. QProcess.ProcessError.FailedToStart if the ssh binary isn't on PATH
        names = {
            QProcess.ProcessError.FailedToStart: "ssh failed to start (is openssh-client installed and on PATH?)",
            QProcess.ProcessError.Crashed: "ssh process crashed",
            QProcess.ProcessError.Timedout: "ssh process timed out",
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
            "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=3",
            *ssh_common_opts(cfg),
            "-p", str(cfg["ssh_port"]),
            "-L", forward,
            f"{cfg['ssh_user']}@{cfg['ssh_host']}",
        ]

        if cfg.get("auth_method") == "password":
            password = get_saved_password(cfg)
            env = QProcessEnvironment.systemEnvironment()
            env.insert("SSHPASS", password or "")
            self.process.setProcessEnvironment(env)
            program, full_args = "sshpass", ["-e", "ssh"] + args
        else:
            program, full_args = "ssh", args

        # Safe to log: with password auth the secret travels via the
        # SSHPASS env var, never as a CLI argument.
        self.command_str = program + " " + " ".join(full_args)
        self.process.start(program, full_args)

    def stop(self):
        if self.process.state() != QProcess.ProcessState.NotRunning:
            self.process.terminate()
            if not self.process.waitForFinished(3000):
                self.process.kill()
                self.process.waitForFinished(1000)

    def is_running(self):
        return self.process.state() != QProcess.ProcessState.NotRunning

    def output_text(self):
        return self._output.decode(errors="replace")

    def wait_ready(self, cfg, timeout=10):
        """Poll the local forward endpoint until it accepts a connection.
        Pumps the Qt event loop each iteration so QProcess actually receives
        the child's exit status and buffered stderr while we wait."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            QApplication.processEvents()
            if self.process.state() == QProcess.ProcessState.NotRunning:
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


class AddTorrentFileWorker(QThread):
    """Adds a torrent from a local .torrent file's raw bytes, using
    rtorrent's load.raw_start/load.raw RPC methods. This sends the file
    contents straight over the existing RPC tunnel — no separate SCP/SFTP
    upload step is needed, and it works whether or not rtorrent has a watch
    directory configured."""

    finished_ok = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, rpc, data, remote_dir="", start=True):
        super().__init__()
        self.rpc = rpc
        self.data = data
        self.remote_dir = remote_dir.strip()
        self.autostart = start

    def run(self):
        try:
            method = "load.raw_start" if self.autostart else "load.raw"
            extra_cmds = []
            if self.remote_dir:
                # Quote-escape the directory in case it contains a double quote.
                safe_dir = self.remote_dir.replace('"', '\\"')
                extra_cmds.append(f'd.directory.set="{safe_dir}"')
            self.rpc.call(method, "", xmlrpclib.Binary(self.data), *extra_cmds)
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
                    check_password_auth_ready(cfg)
                    cmd = wrap_ssh_command(cfg, [
                        "ssh",
                        *ssh_common_opts(cfg),
                        "-p", str(cfg["ssh_port"]),
                        f"{cfg['ssh_user']}@{cfg['ssh_host']}",
                        f"rm -rf -- {shlex.quote(base_path)}",
                    ])
                    result = subprocess.run(
                        cmd, capture_output=True, text=True, timeout=30,
                        env=ssh_subprocess_env(cfg),
                    )
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
        try:
            check_password_auth_ready(cfg)
        except RuntimeError as e:
            self.error.emit(str(e))
            return
        cmd = wrap_ssh_command(cfg, [
            "ssh",
            *ssh_common_opts(cfg),
            "-p", str(cfg["ssh_port"]),
            f"{cfg['ssh_user']}@{cfg['ssh_host']}",
            remote_cmd,
        ])
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=15,
                env=ssh_subprocess_env(cfg),
            )
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


class GlobalLimitsWorker(QThread):
    """Fetches the current global down/up speed limits."""
    result_ready = pyqtSignal(int, int)  # down_bytes, up_bytes
    error = pyqtSignal(str)

    def __init__(self, rpc):
        super().__init__()
        self.rpc = rpc

    def run(self):
        try:
            down, up = fetch_global_limits(self.rpc)
            self.result_ready.emit(down, up)
        except Exception as e:
            self.error.emit(str(e))


class SetGlobalLimitWorker(QThread):
    """Sets one global speed limit (direction is 'down' or 'up')."""
    finished_ok = pyqtSignal(str, int)  # direction, bytes_per_sec (echoed back)
    error = pyqtSignal(str)

    def __init__(self, rpc, direction, bytes_per_sec):
        super().__init__()
        self.rpc = rpc
        self.direction = direction
        self.bytes_per_sec = bytes_per_sec

    def run(self):
        try:
            set_global_limit(self.rpc, self.direction, self.bytes_per_sec)
            self.finished_ok.emit(self.direction, self.bytes_per_sec)
        except Exception as e:
            self.error.emit(str(e))


class TrackerPeerWorker(QThread):
    """Fetches tracker and peer info for a single torrent."""
    result_ready = pyqtSignal(list, list)  # trackers, peers
    error = pyqtSignal(str)

    def __init__(self, rpc, torrent_hash):
        super().__init__()
        self.rpc = rpc
        self.torrent_hash = torrent_hash

    def run(self):
        try:
            trackers = fetch_trackers(self.rpc, self.torrent_hash)
            peers = fetch_peers(self.rpc, self.torrent_hash)
            self.result_ready.emit(trackers, peers)
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

        self.auth_combo = QComboBox()
        self.auth_combo.addItems(["SSH key (passwordless, recommended)", "Password"])
        self.auth_combo.setCurrentIndex(1 if cfg.get("auth_method") == "password" else 0)
        self.auth_combo.currentIndexChanged.connect(self.update_auth_fields)

        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        has_saved = bool(cfg.get("ssh_password_saved"))
        if has_saved:
            self.password_edit.setPlaceholderText("Saved — leave blank to keep it")
        else:
            self.password_edit.setPlaceholderText("Enter password")
        self.show_password_check = QCheckBox("Show")
        self.show_password_check.toggled.connect(
            lambda on: self.password_edit.setEchoMode(QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password)
        )
        self.forget_password_btn = QPushButton("Forget saved password")
        self.forget_password_btn.setEnabled(has_saved)
        self.forget_password_btn.clicked.connect(self.forget_password)
        self._forget_password = False

        password_row = QHBoxLayout()
        password_row.addWidget(self.password_edit)
        password_row.addWidget(self.show_password_check)
        password_row_w = QWidget()
        password_row_w.setLayout(password_row)

        auth_note_text = (
            "Password is stored in your OS keychain via the 'keyring' package."
            if KEYRING_AVAILABLE else
            "NOTE: the 'keyring' package isn't installed, so the password will "
            "be stored in plain text in config.json (pip install keyring, or "
            "apt install python3-keyring, to store it securely instead)."
        )
        self.auth_note_label = QLabel(auth_note_text)
        self.auth_note_label.setWordWrap(True)
        self.sshpass_note_label = QLabel(
            "NOTE: password auth also requires the 'sshpass' utility "
            "(sudo apt install sshpass)."
        )
        if sshpass_available():
            self.sshpass_note_label.hide()

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

        self.download_dir_edit = QLineEdit(cfg.get("download_dir", ""))
        self.download_dir_edit.setPlaceholderText(
            "~/downloads (leave blank to use rtorrent's own default directory)"
        )

        self.autograb_config_edit = QLineEdit(cfg.get("autograb_config_path", ""))
        self.autograb_config_edit.setPlaceholderText("~/.config/rtorrent-autograb/config.json")
        self.autograb_log_edit = QLineEdit(cfg.get("autograb_log_path", ""))
        self.autograb_log_edit.setPlaceholderText("~/.config/rtorrent-autograb/activity.json")

        form = QFormLayout()
        form.addRow("RPC transport:", self.mode_combo)
        form.addRow(QLabel("<b>SSH</b>"))
        form.addRow("Host / IP:", self.ssh_host_edit)
        form.addRow("SSH port:", self.ssh_port_spin)
        form.addRow("Username:", self.ssh_user_edit)
        form.addRow("Authentication:", self.auth_combo)
        form.addRow("Password:", password_row_w)
        form.addRow("", self.forget_password_btn)
        form.addRow("", self.auth_note_label)
        form.addRow("", self.sshpass_note_label)
        form.addRow(QLabel("<b>rtorrent RPC (unix socket)</b>"))
        form.addRow("Remote socket path:", self.remote_socket_edit)
        form.addRow(QLabel("<b>rtorrent RPC (TCP, only if not using unix socket)</b>"))
        form.addRow("Remote bind host:", self.remote_host_edit)
        form.addRow("Remote port:", self.remote_port_spin)
        form.addRow(QLabel("<b>Polling</b>"))
        form.addRow("Refresh every:", self.poll_spin)
        form.addRow("Disk usage path:", self.disk_path_edit)
        form.addRow(QLabel("<b>New torrents</b>"))
        form.addRow("Default download dir:", self.download_dir_edit)
        form.addRow(QLabel("<b>Automation (rtorrent_autograb.py, reached over the SSH connection above --"
                            " it runs no web server of its own)</b>"))
        form.addRow("Remote config path:", self.autograb_config_edit)
        form.addRow("Remote activity log path:", self.autograb_log_edit)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)
        self.update_mode_fields()
        self.update_auth_fields()

    def update_mode_fields(self):
        is_unix = self.mode_combo.currentIndex() == 0
        self.remote_socket_edit.setEnabled(is_unix)
        self.remote_host_edit.setEnabled(not is_unix)
        self.remote_port_spin.setEnabled(not is_unix)

    def update_auth_fields(self):
        is_password = self.auth_combo.currentIndex() == 1
        self.password_edit.setEnabled(is_password)
        self.show_password_check.setEnabled(is_password)
        self.forget_password_btn.setEnabled(is_password and bool(self.cfg.get("ssh_password_saved")))
        self.auth_note_label.setVisible(is_password)
        self.sshpass_note_label.setVisible(is_password and not sshpass_available())

    def forget_password(self):
        self._forget_password = True
        self.password_edit.clear()
        self.password_edit.setPlaceholderText("Enter password")
        self.forget_password_btn.setEnabled(False)

    def get_config(self):
        cfg = dict(self.cfg)
        cfg["mode"] = "unix" if self.mode_combo.currentIndex() == 0 else "tcp"
        cfg["ssh_host"] = self.ssh_host_edit.text().strip()
        cfg["ssh_port"] = self.ssh_port_spin.value()
        cfg["ssh_user"] = self.ssh_user_edit.text().strip()
        cfg["auth_method"] = "password" if self.auth_combo.currentIndex() == 1 else "key"
        cfg["remote_socket_path"] = self.remote_socket_edit.text().strip()
        cfg["remote_host"] = self.remote_host_edit.text().strip()
        cfg["remote_port"] = self.remote_port_spin.value()
        cfg["poll_interval"] = self.poll_spin.value()
        cfg["disk_path"] = self.disk_path_edit.text().strip() or "/"
        cfg["download_dir"] = self.download_dir_edit.text().strip()
        cfg["autograb_config_path"] = self.autograb_config_edit.text().strip()
        cfg["autograb_log_path"] = self.autograb_log_edit.text().strip()

        new_password = self.password_edit.text()
        if self._forget_password and not new_password:
            set_saved_password(cfg, None)
        elif new_password:
            set_saved_password(cfg, new_password)
        # else: keep whatever was already saved (ssh_password_saved untouched)
        return cfg


# --------------------------------------------------------------------------
# Torrent table model
# --------------------------------------------------------------------------
#
# With thousands of torrents, rebuilding a QTableWidget from scratch on every
# poll (as the old code did: setRowCount() then setItem() for every cell)
# means allocating and re-laying-out tens of thousands of QTableWidgetItem
# objects several times a minute -- visibly janky at scale. Instead this is a
# QAbstractTableModel backing a QTableView: on each poll we diff the new
# torrent list against what's already shown, and only touch what changed --
# update in-place rows whose values actually differ (dataChanged, batched
# into contiguous ranges), insert genuinely new torrents, and remove ones
# that vanished. An unchanged torrent (the overwhelmingly common case between
# polls when nothing is up/downloading) costs a single dict comparison, not a
# widget rebuild. Sorting and name-filtering are handled by a
# QSortFilterProxyModel on top so the view never has to touch the raw list.

COLUMNS = [
    "Name", "Status", "Progress", "Down Speed", "Up Speed",
    "ETA", "Peers", "Ratio", "Size", "Downloaded", "Uploaded",
]

SORT_VALUE_ROLE = Qt.ItemDataRole.UserRole + 1
HASH_ROLE = Qt.ItemDataRole.UserRole


def _build_row(t):
    """Precomputes everything the model needs to display/sort a torrent, so
    data() is just dict lookups (no formatting work on every repaint)."""
    status = compute_status(t)
    progress = (t["bytes_done"] / t["size_bytes"] * 100) if t["size_bytes"] else 0.0
    eta_s = compute_eta_seconds(t)
    ratio = t["ratio"] / 1000.0
    display = [
        t["name"],
        status,
        f"{progress:.1f}%",
        human_speed(t["down_rate"]),
        human_speed(t["up_rate"]),
        format_eta(eta_s),
        f"{t['peers_connected']} / {t['peers_not_connected']}",
        f"{ratio:.2f}",
        human_bytes(t["size_bytes"]),
        human_bytes(t["down_total"]),
        human_bytes(t["up_total"]),
    ]
    sort_values = [
        t["name"].lower(),
        status,
        progress,
        t["down_rate"],
        t["up_rate"],
        eta_s if eta_s is not None else float("inf"),
        t["peers_connected"],
        ratio,
        t["size_bytes"],
        t["down_total"],
        t["up_total"],
    ]
    return {
        "hash": t["hash"],
        "message": t.get("message") or "",
        "down_rate": t["down_rate"],
        "up_rate": t["up_rate"],
        "display": display,
        "sort": sort_values,
    }


def _contiguous_ranges(sorted_indices):
    """[2,3,4,7,8] -> [(2,4), (7,8)] -- lets us batch dataChanged/removeRows
    calls instead of emitting one per row."""
    ranges = []
    start = prev = None
    for i in sorted_indices:
        if start is None:
            start = prev = i
        elif i == prev + 1:
            prev = i
        else:
            ranges.append((start, prev))
            start = prev = i
    if start is not None:
        ranges.append((start, prev))
    return ranges


class TorrentTableModel(QAbstractTableModel):
    def __init__(self):
        super().__init__()
        self._rows = []          # list of row dicts, see _build_row()
        self._row_by_hash = {}   # hash -> row index, for O(1) lookups

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return COLUMNS[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        col = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            return row["display"][col]
        if role == SORT_VALUE_ROLE:
            return row["sort"][col]
        if role == HASH_ROLE:
            return row["hash"]
        if role == Qt.ItemDataRole.ToolTipRole and col == 1:
            return row["message"] or None
        return None

    def hash_at(self, row_idx):
        if 0 <= row_idx < len(self._rows):
            return self._rows[row_idx]["hash"]
        return None

    def row_of_hash(self, h):
        return self._row_by_hash.get(h)

    def totals(self):
        down = sum(r["down_rate"] for r in self._rows)
        up = sum(r["up_rate"] for r in self._rows)
        return down, up

    def _rebuild_index(self):
        self._row_by_hash = {row["hash"]: i for i, row in enumerate(self._rows)}

    def update_torrents(self, torrents):
        """Diffs `torrents` against what's currently shown and applies only
        the minimal set of insert/update/remove operations. See the module
        comment above for why this matters at thousands-of-rows scale."""
        new_by_hash = {t["hash"]: t for t in torrents}
        old_hashes = set(self._row_by_hash.keys())
        new_hashes = set(new_by_hash.keys())

        removed = old_hashes - new_hashes
        added = new_hashes - old_hashes
        kept = old_hashes & new_hashes

        if removed:
            remove_rows = sorted(self._row_by_hash[h] for h in removed)
            # Remove highest-indexed ranges first so earlier indices stay valid.
            for start, end in reversed(_contiguous_ranges(remove_rows)):
                self.beginRemoveRows(QModelIndex(), start, end)
                del self._rows[start:end + 1]
                self.endRemoveRows()
            self._rebuild_index()

        changed_rows = []
        for h in kept:
            row_idx = self._row_by_hash[h]
            new_row = _build_row(new_by_hash[h])
            if new_row["display"] != self._rows[row_idx]["display"]:
                self._rows[row_idx] = new_row
                changed_rows.append(row_idx)
        for start, end in _contiguous_ranges(sorted(changed_rows)):
            top_left = self.index(start, 0)
            bottom_right = self.index(end, self.columnCount() - 1)
            self.dataChanged.emit(top_left, bottom_right)

        if added:
            new_rows = [_build_row(new_by_hash[h]) for h in added]
            start = len(self._rows)
            self.beginInsertRows(QModelIndex(), start, start + len(new_rows) - 1)
            self._rows.extend(new_rows)
            self.endInsertRows()
            self._rebuild_index()


class TorrentFilterProxy(QSortFilterProxyModel):
    """Sorts by the precomputed numeric sort value (not the display text, so
    e.g. '2 GiB' correctly sorts above '100 MiB'); filters by name substring."""

    def __init__(self):
        super().__init__()
        self.setSortRole(SORT_VALUE_ROLE)
        self.setFilterKeyColumn(0)
        self.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.setDynamicSortFilter(True)


# --------------------------------------------------------------------------
# Tracker / peer info dialog
# --------------------------------------------------------------------------

TRACKER_COLUMNS = ["URL", "Type", "Enabled", "Seeders", "Leechers"]
PEER_COLUMNS = ["Address", "Client", "Down Speed", "Up Speed", "Progress", "Encrypted"]

TRACKER_TYPES = {0: "-", 1: "HTTP", 2: "UDP", 3: "DHT"}


class TrackerPeerDialog(QDialog):
    """Shows trackers and connected peers for one torrent, refreshing itself
    on a timer for as long as it's open."""

    def __init__(self, parent, rpc, torrent_hash, torrent_name, poll_interval):
        super().__init__(parent)
        self.setWindowTitle(f"Tracker & Peer Info — {torrent_name}")
        self.resize(720, 480)
        self.rpc = rpc
        self.torrent_hash = torrent_hash
        self.worker = None

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("<b>Trackers</b>"))
        self.tracker_table = QTableWidget(0, len(TRACKER_COLUMNS))
        self.tracker_table.setHorizontalHeaderLabels(TRACKER_COLUMNS)
        self.tracker_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tracker_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tracker_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        layout.addWidget(self.tracker_table, 1)

        layout.addWidget(QLabel("<b>Peers</b>"))
        self.peer_table = QTableWidget(0, len(PEER_COLUMNS))
        self.peer_table.setHorizontalHeaderLabels(PEER_COLUMNS)
        self.peer_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.peer_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.peer_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        layout.addWidget(self.peer_table, 2)

        self.status_label = QLabel("")
        layout.addWidget(self.status_label)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.Close).clicked.connect(self.accept)
        layout.addWidget(buttons)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(max(poll_interval, 2) * 1000)
        self.refresh()

    def refresh(self):
        if self.worker is not None and self.worker.isRunning():
            return
        self.worker = TrackerPeerWorker(self.rpc, self.torrent_hash)
        self.worker.result_ready.connect(self.on_result)
        self.worker.error.connect(self.on_error)
        self.worker.start()

    def on_result(self, trackers, peers):
        self.status_label.setText(f"{len(trackers)} tracker(s), {len(peers)} peer(s)")

        self.tracker_table.setRowCount(len(trackers))
        for row, t in enumerate(trackers):
            self.tracker_table.setItem(row, 0, QTableWidgetItem(t["url"]))
            self.tracker_table.setItem(row, 1, QTableWidgetItem(TRACKER_TYPES.get(t["type"], str(t["type"]))))
            self.tracker_table.setItem(row, 2, QTableWidgetItem("Yes" if t["is_enabled"] else "No"))
            self.tracker_table.setItem(row, 3, QTableWidgetItem(str(t["seeders"])))
            self.tracker_table.setItem(row, 4, QTableWidgetItem(str(t["leechers"])))

        self.peer_table.setRowCount(len(peers))
        for row, p in enumerate(peers):
            address = f"{p['address']}:{p['port']}"
            self.peer_table.setItem(row, 0, QTableWidgetItem(address))
            self.peer_table.setItem(row, 1, QTableWidgetItem(p["client"]))
            self.peer_table.setItem(row, 2, QTableWidgetItem(human_speed(p["down_rate"])))
            self.peer_table.setItem(row, 3, QTableWidgetItem(human_speed(p["up_rate"])))
            self.peer_table.setItem(row, 4, QTableWidgetItem(f"{p['completed_percent']}%"))
            self.peer_table.setItem(row, 5, QTableWidgetItem("Yes" if p["is_encrypted"] else "No"))

    def on_error(self, msg):
        self.status_label.setText(f"Refresh failed: {msg}")

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Left:
            self.reject()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event):
        self.timer.stop()
        event.accept()

    def accept(self):
        self.timer.stop()
        super().accept()

    def reject(self):
        self.timer.stop()
        super().reject()


# --------------------------------------------------------------------------
# rtorrent_autograb.py remote config access (over SSH -- no web server)
# --------------------------------------------------------------------------
#
# rtorrent_autograb.py opens no network listener of its own. Its config and
# activity log are just files on the server, so the GUI reads and writes
# them the same way it already deletes torrent files: a plain `ssh
# user@host "..."` invocation (through sshpass when password auth is
# configured), reusing the exact SSH plumbing above. Writes go to a
# `<path>.tmp` then `mv` into place so a reader (or the daemon reloading on
# mtime change) never sees a half-written file.

class RemoteConfigError(Exception):
    pass


def _quote_remote_path(path):
    """shlex.quote() wraps the *entire* string in single quotes whenever it
    contains a '~', which disables the remote shell's tilde expansion --
    `cat -- '~/foo'` looks for a file literally named '~/foo' and fails,
    even though the real file is right there under the actual home
    directory. Keep a leading '~' or '~username' unquoted so the shell
    still expands it, and safely quote everything after it."""
    if path.startswith("~"):
        prefix, sep, rest = path.partition("/")
        if rest:
            return prefix + sep + shlex.quote(rest)
        return prefix
    return shlex.quote(path)


class RemoteConfigClient:
    def __init__(self, cfg, timeout=15):
        self.cfg = cfg
        self.timeout = timeout

    def _run(self, remote_cmd, input_bytes=None):
        try:
            check_password_auth_ready(self.cfg)
        except RuntimeError as e:
            raise RemoteConfigError(str(e))
        if not self.cfg.get("ssh_host") or not self.cfg.get("ssh_user"):
            raise RemoteConfigError("SSH connection isn't configured yet (see Settings).")
        cmd = wrap_ssh_command(self.cfg, [
            "ssh",
            *ssh_common_opts(self.cfg),
            "-p", str(self.cfg["ssh_port"]),
            f"{self.cfg['ssh_user']}@{self.cfg['ssh_host']}",
            remote_cmd,
        ])
        try:
            return subprocess.run(
                cmd, input=input_bytes, capture_output=True, timeout=self.timeout,
                env=ssh_subprocess_env(self.cfg),
            )
        except subprocess.TimeoutExpired:
            raise RemoteConfigError("SSH command timed out.")
        except RuntimeError as e:  # ssh_subprocess_env() missing password
            raise RemoteConfigError(str(e))

    def get_config(self, remote_path):
        if not remote_path:
            raise RemoteConfigError("No automation config path configured (see Settings).")
        result = self._run(f"cat -- {_quote_remote_path(remote_path)}")
        if result.returncode != 0:
            raise RemoteConfigError(
                result.stderr.decode("utf-8", "replace").strip()
                or f"Could not read {remote_path} on the server."
            )
        try:
            return json.loads(result.stdout.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise RemoteConfigError(f"Malformed config JSON on server: {e}")

    def put_config(self, remote_path, cfg_dict):
        if not remote_path:
            raise RemoteConfigError("No automation config path configured (see Settings).")
        data = json.dumps(cfg_dict, indent=2).encode("utf-8")
        quoted = _quote_remote_path(remote_path)
        tmp_quoted = _quote_remote_path(remote_path + ".tmp")
        # Assume the config's parent directory already exists (rtorrent_autograb.py
        # --init created it); this only needs to write the file itself.
        remote_cmd = f"cat > {tmp_quoted} && mv -- {tmp_quoted} {quoted}"
        result = self._run(remote_cmd, input_bytes=data)
        if result.returncode != 0:
            raise RemoteConfigError(
                result.stderr.decode("utf-8", "replace").strip()
                or "Failed to write config on server."
            )

    def get_log(self, remote_path, limit=200):
        if not remote_path:
            return []
        result = self._run(f"cat -- {_quote_remote_path(remote_path)} 2>/dev/null || true")
        raw = result.stdout.decode("utf-8", "replace").strip()
        if not raw:
            return []
        try:
            entries = json.loads(raw).get("entries", [])
        except json.JSONDecodeError:
            return []
        return list(entries[-limit:][::-1])


DEFAULT_FILTER = {
    "name": "New filter", "enabled": True, "include_keywords": [], "include_mode": "any",
    "exclude_keywords": [], "regex": "", "quality": [], "codecs": [], "min_size_mb": 0,
    "max_size_mb": 0, "download_dir": "",
}
DEFAULT_IRC_SOURCE = {
    "id": "", "enabled": True, "name": "New IRC source", "server": "", "port": 6697,
    "tls": True, "tls_verify": True, "nick": "", "server_password": "", "nickserv_password": "",
    "channels": [], "invite_command": "", "line_regex": "", "filters": [],
}
DEFAULT_RSS_SOURCE = {
    "id": "", "enabled": True, "name": "New RSS feed", "url": "", "poll_interval_sec": 300, "filters": [],
}


def _csv_to_list(text):
    return [p.strip() for p in text.split(",") if p.strip()]


def _list_to_csv(items):
    return ", ".join(items or [])


def filter_matches(name: str, size_bytes, filt: dict):
    """Local port of rtorrent_autograb.py's filter_matches(), used only for
    the "Test..." preview button below -- no network round-trip needed."""
    if not filt.get("enabled", True):
        return False
    lname = name.lower()
    include = [k.lower() for k in filt.get("include_keywords", []) if k and k.strip()]
    if include:
        if filt.get("include_mode", "any") == "all":
            if not all(k in lname for k in include):
                return False
        else:
            if not any(k in lname for k in include):
                return False
    exclude = [k.lower() for k in filt.get("exclude_keywords", []) if k and k.strip()]
    if any(k in lname for k in exclude):
        return False
    regex = (filt.get("regex") or "").strip()
    if regex:
        try:
            if not re.search(regex, name, re.IGNORECASE):
                return False
        except re.error:
            return False
    quality = [q.lower() for q in filt.get("quality", []) if q and q.strip()]
    if quality and not any(q in lname for q in quality):
        return False
    codecs = [c.lower() for c in filt.get("codecs", []) if c and c.strip()]
    if codecs and not any(c in lname for c in codecs):
        return False
    if size_bytes is not None:
        size_mb = size_bytes / (1024 * 1024)
        min_mb = filt.get("min_size_mb") or 0
        max_mb = filt.get("max_size_mb") or 0
        if min_mb and size_mb < min_mb:
            return False
        if max_mb and size_mb > max_mb:
            return False
    return True


class FilterEditorDialog(QDialog):
    """Edits a single autograb filter (name/quality/codec keywords, regex,
    size range, and a per-filter download-dir override)."""

    @staticmethod
    def _hint(text):
        label = QLabel(text)
        label.setWordWrap(True)
        label.setStyleSheet("color: palette(mid); font-style: italic;")
        return label

    def __init__(self, parent, filt):
        super().__init__(parent)
        self.setWindowTitle("Edit Filter")
        self.setMinimumWidth(460)
        f = dict(DEFAULT_FILTER)
        f.update(filt or {})

        self.name_edit = QLineEdit(f["name"])
        self.enabled_check = QCheckBox("Enabled")
        self.enabled_check.setChecked(bool(f["enabled"]))

        self.include_edit = QLineEdit(_list_to_csv(f["include_keywords"]))
        self.include_edit.setPlaceholderText("e.g. bike, car")
        self.include_mode_combo = QComboBox()
        # Item text spells out the example concretely, since "any" / "all"
        # alone is exactly the kind of thing that's ambiguous until you've
        # seen it applied to your own words.
        self.include_mode_combo.addItem(
            "Match ANY of these (OR) — \u201cbike, car\u201d adds releases with either word", "any")
        self.include_mode_combo.addItem(
            "Match ALL of these (AND) — \u201cbike, car\u201d only adds releases with both words", "all")
        include_mode_idx = self.include_mode_combo.findData(f.get("include_mode", "any"))
        self.include_mode_combo.setCurrentIndex(max(include_mode_idx, 0))

        self.exclude_edit = QLineEdit(_list_to_csv(f["exclude_keywords"]))
        self.exclude_edit.setPlaceholderText("e.g. cam, telesync")

        self.regex_edit = QLineEdit(f["regex"])
        self.regex_edit.setPlaceholderText("optional extra regex (Python re, case-insensitive)")

        self.quality_edit = QLineEdit(_list_to_csv(f["quality"]))
        self.quality_edit.setPlaceholderText("e.g. 1080p, 2160p")

        self.codecs_edit = QLineEdit(_list_to_csv(f["codecs"]))
        self.codecs_edit.setPlaceholderText("e.g. x265, x264")

        self.min_size_spin = QSpinBox()
        self.min_size_spin.setRange(0, 999999)
        self.min_size_spin.setSuffix(" MB")
        self.min_size_spin.setSpecialValueText("No minimum")
        self.min_size_spin.setValue(int(f["min_size_mb"]))
        self.max_size_spin = QSpinBox()
        self.max_size_spin.setRange(0, 999999)
        self.max_size_spin.setSuffix(" MB")
        self.max_size_spin.setSpecialValueText("No maximum")
        self.max_size_spin.setValue(int(f["max_size_mb"]))
        self.download_dir_edit = QLineEdit(f["download_dir"])
        self.download_dir_edit.setPlaceholderText("blank = server's default download dir")

        form = QFormLayout()
        form.addRow("Name:", self.name_edit)
        form.addRow("", self.enabled_check)
        form.addRow("Include keywords:", self.include_edit)
        form.addRow("Match mode:", self.include_mode_combo)
        form.addRow("", self._hint(
            "Comma-separated words or phrases. Leave blank to skip this check entirely "
            "(everything passes). With multiple words, \u201cMatch mode\u201d above decides "
            "whether ONE of them is enough (OR) or the release must contain EVERY one (AND)."
        ))
        form.addRow("Exclude keywords:", self.exclude_edit)
        form.addRow("", self._hint(
            "Always OR: the release is rejected if its name contains ANY one of these words."
        ))
        form.addRow("Regex (optional):", self.regex_edit)
        form.addRow("Quality tags:", self.quality_edit)
        form.addRow("Codec tags:", self.codecs_edit)
        form.addRow("", self._hint(
            "Quality and codec tags are always OR (treated as acceptable alternatives) — "
            "\u201c1080p, 2160p\u201d matches a release tagged with either one, since a release "
            "is normally only tagged with one resolution or codec anyway."
        ))
        form.addRow("Min size:", self.min_size_spin)
        form.addRow("Max size:", self.max_size_spin)
        form.addRow("Download dir override:", self.download_dir_edit)

        self.test_name_edit = QLineEdit()
        self.test_name_edit.setPlaceholderText("Paste a sample release name to test this filter against...")
        self.test_result_label = QLabel("")
        test_btn = QPushButton("Test")
        test_btn.clicked.connect(self.run_test)
        test_row = QHBoxLayout()
        test_row.addWidget(self.test_name_edit, 1)
        test_row.addWidget(test_btn)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(QLabel("<b>Test</b> (checked locally, doesn't touch the server)"))
        layout.addLayout(test_row)
        layout.addWidget(self.test_result_label)
        layout.addWidget(buttons)

    def run_test(self):
        name = self.test_name_edit.text().strip()
        if not name:
            self.test_result_label.setText("")
            return
        matches = filter_matches(name, None, self.get_filter())
        if matches:
            self.test_result_label.setText("✓ Matches (size range is skipped in this preview)")
        else:
            self.test_result_label.setText("✗ Does not match")

    def get_filter(self):
        return {
            "name": self.name_edit.text().strip() or "Unnamed filter",
            "enabled": self.enabled_check.isChecked(),
            "include_keywords": _csv_to_list(self.include_edit.text()),
            "include_mode": self.include_mode_combo.currentData() or "any",
            "exclude_keywords": _csv_to_list(self.exclude_edit.text()),
            "regex": self.regex_edit.text().strip(),
            "quality": _csv_to_list(self.quality_edit.text()),
            "codecs": _csv_to_list(self.codecs_edit.text()),
            "min_size_mb": self.min_size_spin.value(),
            "max_size_mb": self.max_size_spin.value(),
            "download_dir": self.download_dir_edit.text().strip(),
        }


class FilterListWidget(QWidget):
    """Reusable "list of filters" editor embedded in both the IRC and RSS
    source dialogs: Add / Edit / Remove buttons next to a QListWidget."""

    def __init__(self, parent, filters):
        super().__init__(parent)
        self.filters = [dict(DEFAULT_FILTER, **f) for f in (filters or [])]

        self.list_widget = QListWidget()
        self._refresh_list()

        add_btn = QPushButton("Add...")
        add_btn.clicked.connect(self.add_filter)
        edit_btn = QPushButton("Edit...")
        edit_btn.clicked.connect(self.edit_filter)
        remove_btn = QPushButton("Remove")
        remove_btn.clicked.connect(self.remove_filter)

        btn_col = QVBoxLayout()
        btn_col.addWidget(add_btn)
        btn_col.addWidget(edit_btn)
        btn_col.addWidget(remove_btn)
        btn_col.addStretch()

        row = QHBoxLayout(self)
        row.addWidget(self.list_widget, 1)
        btn_col_w = QWidget()
        btn_col_w.setLayout(btn_col)
        row.addWidget(btn_col_w)

    def _refresh_list(self):
        self.list_widget.clear()
        for f in self.filters:
            label = f["name"]
            if len(f.get("include_keywords") or []) > 1:
                label += "  [AND]" if f.get("include_mode", "any") == "all" else "  [OR]"
            if not f.get("enabled", True):
                label += "  (disabled)"
            self.list_widget.addItem(QListWidgetItem(label))

    def add_filter(self):
        dlg = FilterEditorDialog(self, {})
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.filters.append(dlg.get_filter())
            self._refresh_list()

    def edit_filter(self):
        row = self.list_widget.currentRow()
        if row < 0:
            return
        dlg = FilterEditorDialog(self, self.filters[row])
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.filters[row] = dlg.get_filter()
            self._refresh_list()

    def remove_filter(self):
        row = self.list_widget.currentRow()
        if row < 0:
            return
        del self.filters[row]
        self._refresh_list()


class IrcSourceDialog(QDialog):
    def __init__(self, parent, source):
        super().__init__(parent)
        self.setWindowTitle("IRC Announce Source")
        self.setMinimumWidth(460)
        s = dict(DEFAULT_IRC_SOURCE)
        s.update(source or {})
        self._id = s["id"]

        self.enabled_check = QCheckBox("Enabled")
        self.enabled_check.setChecked(bool(s["enabled"]))
        self.name_edit = QLineEdit(s["name"])
        self.server_edit = QLineEdit(s["server"])
        self.server_edit.setPlaceholderText("irc.example.net")
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(int(s["port"]))
        self.tls_check = QCheckBox("Use TLS")
        self.tls_check.setChecked(bool(s["tls"]))
        self.tls_verify_check = QCheckBox("Verify TLS certificate")
        self.tls_verify_check.setChecked(bool(s["tls_verify"]))
        self.nick_edit = QLineEdit(s["nick"])
        self.server_password_edit = QLineEdit(s["server_password"])
        self.server_password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.nickserv_password_edit = QLineEdit(s["nickserv_password"])
        self.nickserv_password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.channels_edit = QLineEdit(_list_to_csv(s["channels"]))
        self.channels_edit.setPlaceholderText("#announce, #announce2")
        self.invite_edit = QLineEdit(s["invite_command"])
        self.invite_edit.setPlaceholderText("optional, sent once after connecting")
        self.line_regex_edit = QLineEdit(s["line_regex"])
        self.line_regex_edit.setPlaceholderText("optional; blank = auto-detect magnet/.torrent links")

        form = QFormLayout()
        form.addRow("", self.enabled_check)
        form.addRow("Name:", self.name_edit)
        form.addRow("Server:", self.server_edit)
        form.addRow("Port:", self.port_spin)
        form.addRow("", self.tls_check)
        form.addRow("", self.tls_verify_check)
        form.addRow("Nick:", self.nick_edit)
        form.addRow("Server password:", self.server_password_edit)
        form.addRow("NickServ password:", self.nickserv_password_edit)
        form.addRow("Channels:", self.channels_edit)
        form.addRow("Invite command:", self.invite_edit)
        form.addRow("Line regex:", self.line_regex_edit)

        self.filter_list = FilterListWidget(self, s["filters"])

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(QLabel("<b>Filters</b> (a release is grabbed if it matches any one of these)"))
        layout.addWidget(self.filter_list)
        layout.addWidget(buttons)

    def get_source(self):
        return {
            "id": self._id or uuid.uuid4().hex[:8],
            "enabled": self.enabled_check.isChecked(),
            "name": self.name_edit.text().strip() or "Unnamed IRC source",
            "server": self.server_edit.text().strip(),
            "port": self.port_spin.value(),
            "tls": self.tls_check.isChecked(),
            "tls_verify": self.tls_verify_check.isChecked(),
            "nick": self.nick_edit.text().strip(),
            "server_password": self.server_password_edit.text(),
            "nickserv_password": self.nickserv_password_edit.text(),
            "channels": _csv_to_list(self.channels_edit.text()),
            "invite_command": self.invite_edit.text().strip(),
            "line_regex": self.line_regex_edit.text().strip(),
            "filters": self.filter_list.filters,
        }


class RssSourceDialog(QDialog):
    def __init__(self, parent, source):
        super().__init__(parent)
        self.setWindowTitle("RSS Feed Source")
        self.setMinimumWidth(460)
        s = dict(DEFAULT_RSS_SOURCE)
        s.update(source or {})
        self._id = s["id"]

        self.enabled_check = QCheckBox("Enabled")
        self.enabled_check.setChecked(bool(s["enabled"]))
        self.name_edit = QLineEdit(s["name"])
        self.url_edit = QLineEdit(s["url"])
        self.url_edit.setPlaceholderText("https://example.com/rss?key=...")
        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(30, 86400)
        self.interval_spin.setSuffix(" s")
        self.interval_spin.setValue(int(s["poll_interval_sec"]))

        form = QFormLayout()
        form.addRow("", self.enabled_check)
        form.addRow("Name:", self.name_edit)
        form.addRow("Feed URL:", self.url_edit)
        form.addRow("Poll every:", self.interval_spin)

        self.filter_list = FilterListWidget(self, s["filters"])

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(QLabel("<b>Filters</b> (an item is grabbed if it matches any one of these)"))
        layout.addWidget(self.filter_list)
        layout.addWidget(buttons)

    def get_source(self):
        return {
            "id": self._id or uuid.uuid4().hex[:8],
            "enabled": self.enabled_check.isChecked(),
            "name": self.name_edit.text().strip() or "Unnamed RSS feed",
            "url": self.url_edit.text().strip(),
            "poll_interval_sec": self.interval_spin.value(),
            "filters": self.filter_list.filters,
        }


class AutomationDialog(QDialog):
    """Views/edits rtorrent_autograb.py's IRC sources, RSS feeds, and their
    filters by reading/writing its config file over SSH (the daemon runs no
    web server), plus a read-only recent-activity log read the same way."""

    def __init__(self, parent, client: RemoteConfigClient, config_path: str, log_path: str):
        super().__init__(parent)
        self.setWindowTitle("Automation — IRC & RSS auto-add")
        self.resize(720, 520)
        self.client = client
        self.config_path = config_path
        self.log_path = log_path
        self.cfg = None  # loaded lazily

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)

        self.irc_table = QTableWidget(0, 3)
        self.irc_table.setHorizontalHeaderLabels(["Name", "Server", "Channels"])
        self.irc_table.horizontalHeader().setStretchLastSection(True)
        self.irc_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.irc_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.irc_table.doubleClicked.connect(lambda _: self.edit_irc_source())
        irc_tab = self._build_source_tab(
            self.irc_table, self.add_irc_source, self.edit_irc_source, self.remove_irc_source
        )

        self.rss_table = QTableWidget(0, 3)
        self.rss_table.setHorizontalHeaderLabels(["Name", "Feed URL", "Interval"])
        self.rss_table.horizontalHeader().setStretchLastSection(True)
        self.rss_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.rss_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.rss_table.doubleClicked.connect(lambda _: self.edit_rss_source())
        rss_tab = self._build_source_tab(
            self.rss_table, self.add_rss_source, self.edit_rss_source, self.remove_rss_source
        )

        self.log_table = QTableWidget(0, 5)
        self.log_table.setHorizontalHeaderLabels(["Time", "Source", "Name", "Status", "Detail"])
        self.log_table.horizontalHeader().setStretchLastSection(True)
        self.log_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        log_refresh_btn = QPushButton("Refresh log")
        log_refresh_btn.clicked.connect(self.refresh_log)
        log_tab = QWidget()
        log_layout = QVBoxLayout(log_tab)
        log_layout.addWidget(self.log_table)
        log_layout.addWidget(log_refresh_btn)

        tabs = QTabWidget()
        tabs.addTab(irc_tab, "IRC Sources")
        tabs.addTab(rss_tab, "RSS Feeds")
        tabs.addTab(log_tab, "Activity Log")

        reload_btn = QPushButton("Reload from server")
        reload_btn.clicked.connect(self.load_config)
        save_btn = QPushButton("Save to server")
        save_btn.clicked.connect(self.save_config)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)
        btn_row = QHBoxLayout()
        btn_row.addWidget(reload_btn)
        btn_row.addStretch()
        btn_row.addWidget(save_btn)
        btn_row.addWidget(close_btn)

        layout = QVBoxLayout(self)
        layout.addWidget(self.status_label)
        layout.addWidget(tabs)
        layout.addLayout(btn_row)

        self.load_config()

    def _build_source_tab(self, table, add_fn, edit_fn, remove_fn):
        add_btn = QPushButton("Add...")
        add_btn.clicked.connect(add_fn)
        edit_btn = QPushButton("Edit...")
        edit_btn.clicked.connect(edit_fn)
        remove_btn = QPushButton("Remove")
        remove_btn.clicked.connect(remove_fn)
        btn_row = QHBoxLayout()
        btn_row.addWidget(add_btn)
        btn_row.addWidget(edit_btn)
        btn_row.addWidget(remove_btn)
        btn_row.addStretch()
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.addWidget(table)
        layout.addLayout(btn_row)
        return tab

    # -- data loading / saving -------------------------------------------

    def load_config(self):
        try:
            self.cfg = self.client.get_config(self.config_path)
            self.status_label.setText(f"Loaded {self.config_path} over SSH.")
        except RemoteConfigError as e:
            self.cfg = {"irc_sources": [], "rss_sources": []}
            self.status_label.setText(f"⚠ {e}")
        self._refresh_irc_table()
        self._refresh_rss_table()
        self.refresh_log()

    def save_config(self):
        if self.cfg is None:
            return
        try:
            self.client.put_config(self.config_path, self.cfg)
            self.status_label.setText(
                "Saved over SSH. rtorrent_autograb.py picks up the change "
                "within a few seconds (it watches the file, no restart needed)."
            )
        except RemoteConfigError as e:
            QMessageBox.warning(self, "Save failed", str(e))

    def refresh_log(self):
        try:
            entries = self.client.get_log(self.log_path)
        except RemoteConfigError as e:
            self.status_label.setText(f"⚠ {e}")
            return
        self.log_table.setRowCount(len(entries))
        for row, e in enumerate(entries):
            self.log_table.setItem(row, 0, QTableWidgetItem(e.get("time", "")))
            self.log_table.setItem(row, 1, QTableWidgetItem(f"{e.get('source_type','')}: {e.get('source_name','')}"))
            self.log_table.setItem(row, 2, QTableWidgetItem(e.get("name", "")))
            self.log_table.setItem(row, 3, QTableWidgetItem(e.get("status", "")))
            self.log_table.setItem(row, 4, QTableWidgetItem(e.get("detail", "")))

    # -- IRC sources -------------------------------------------------------

    def _refresh_irc_table(self):
        sources = self.cfg.get("irc_sources", [])
        self.irc_table.setRowCount(len(sources))
        for row, s in enumerate(sources):
            name = s["name"] + ("" if s.get("enabled", True) else "  (disabled)")
            self.irc_table.setItem(row, 0, QTableWidgetItem(name))
            self.irc_table.setItem(row, 1, QTableWidgetItem(f"{s.get('server','')}:{s.get('port','')}"))
            self.irc_table.setItem(row, 2, QTableWidgetItem(_list_to_csv(s.get("channels", []))))

    def add_irc_source(self):
        dlg = IrcSourceDialog(self, {})
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.cfg.setdefault("irc_sources", []).append(dlg.get_source())
            self._refresh_irc_table()

    def edit_irc_source(self):
        row = self.irc_table.currentRow()
        sources = self.cfg.get("irc_sources", [])
        if row < 0 or row >= len(sources):
            return
        dlg = IrcSourceDialog(self, sources[row])
        if dlg.exec() == QDialog.DialogCode.Accepted:
            sources[row] = dlg.get_source()
            self._refresh_irc_table()

    def remove_irc_source(self):
        row = self.irc_table.currentRow()
        sources = self.cfg.get("irc_sources", [])
        if row < 0 or row >= len(sources):
            return
        del sources[row]
        self._refresh_irc_table()

    # -- RSS sources ---------------------------------------------------

    def _refresh_rss_table(self):
        sources = self.cfg.get("rss_sources", [])
        self.rss_table.setRowCount(len(sources))
        for row, s in enumerate(sources):
            name = s["name"] + ("" if s.get("enabled", True) else "  (disabled)")
            self.rss_table.setItem(row, 0, QTableWidgetItem(name))
            self.rss_table.setItem(row, 1, QTableWidgetItem(s.get("url", "")))
            self.rss_table.setItem(row, 2, QTableWidgetItem(f"{s.get('poll_interval_sec', 0)} s"))

    def add_rss_source(self):
        dlg = RssSourceDialog(self, {})
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.cfg.setdefault("rss_sources", []).append(dlg.get_source())
            self._refresh_rss_table()

    def edit_rss_source(self):
        row = self.rss_table.currentRow()
        sources = self.cfg.get("rss_sources", [])
        if row < 0 or row >= len(sources):
            return
        dlg = RssSourceDialog(self, sources[row])
        if dlg.exec() == QDialog.DialogCode.Accepted:
            sources[row] = dlg.get_source()
            self._refresh_rss_table()

    def remove_rss_source(self):
        row = self.rss_table.currentRow()
        sources = self.cfg.get("rss_sources", [])
        if row < 0 or row >= len(sources):
            return
        del sources[row]
        self._refresh_rss_table()


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
        self.disk_timer.timeout.connect(self.on_disk_timer)
        self.limits_worker = None
        self.set_limit_worker = None
        self.tracker_peer_dialog = None
        self.prev_complete = {}  # hash -> bool, used to detect completions
        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.timeout.connect(self.apply_filter)
        self.autograb_client = None

        self.build_ui()
        self.build_tray_icon()
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
        automation_btn = QPushButton("Automation...")
        automation_btn.clicked.connect(self.open_automation_dialog)
        top_row.addWidget(self.connect_btn)
        top_row.addWidget(settings_btn)
        top_row.addWidget(refresh_btn)
        top_row.addWidget(automation_btn)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Filter by name...")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self.on_search_text_changed)
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
        add_file_btn = QPushButton("Add .torrent file...")
        add_file_btn.clicked.connect(self.add_torrent_file)
        magnet_row.addWidget(magnet_label)
        magnet_row.addWidget(self.magnet_edit)
        magnet_row.addWidget(add_magnet_btn)
        magnet_row.addWidget(add_file_btn)
        layout.addLayout(magnet_row)

        limits_row = QHBoxLayout()
        limits_row.addWidget(QLabel("Global limits (KiB/s, 0 = unlimited):"))
        limits_row.addWidget(QLabel("Down:"))
        self.down_limit_spin = QSpinBox()
        self.down_limit_spin.setRange(0, 999999)
        self.down_limit_spin.setSpecialValueText("Unlimited")
        self.down_limit_spin.editingFinished.connect(lambda: self.apply_global_limit("down"))
        limits_row.addWidget(self.down_limit_spin)
        limits_row.addWidget(QLabel("Up:"))
        self.up_limit_spin = QSpinBox()
        self.up_limit_spin.setRange(0, 999999)
        self.up_limit_spin.setSpecialValueText("Unlimited")
        self.up_limit_spin.editingFinished.connect(lambda: self.apply_global_limit("up"))
        limits_row.addWidget(self.up_limit_spin)
        self.down_limit_spin.setEnabled(False)
        self.up_limit_spin.setEnabled(False)
        limits_row.addStretch()
        layout.addLayout(limits_row)

        self.torrent_model = TorrentTableModel()
        self.proxy_model = TorrentFilterProxy()
        self.proxy_model.setSourceModel(self.torrent_model)

        self.table = QTableView()
        self.table.setModel(self.proxy_model)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        base_col_width = 90
        self.table.setColumnWidth(0, base_col_width * 2)  # Name: twice the width of the rest
        for col in range(1, len(COLUMNS)):
            self.table.setColumnWidth(col, base_col_width)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        # ScrollPerPixel (vs. the default per-item) keeps scrolling smooth
        # once the list is thousands of rows tall.
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.show_context_menu)
        self.table.doubleClicked.connect(self.show_tracker_peer_info)
        self.table.installEventFilter(self)
        layout.addWidget(self.table)

        self.setCentralWidget(central)

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.disk_space_label = QLabel("Free: \u2013")
        self.disk_space_label.setContentsMargins(0, 0, 8, 0)
        self.status_bar.addPermanentWidget(self.disk_space_label)

    def build_tray_icon(self):
        icon = QIcon.fromTheme("network-transmit-receive")
        if icon.isNull():
            icon = self.style().standardIcon(QStyle.StandardPixmap.SP_DriveNetIcon)
        self.tray_icon = QSystemTrayIcon(icon, self)
        self.tray_icon.setToolTip("rtorrent GUI")

        tray_menu = QMenu()
        show_action = tray_menu.addAction("Show")
        show_action.triggered.connect(self.showNormal)
        hide_action = tray_menu.addAction("Hide")
        hide_action.triggered.connect(self.hide)
        tray_menu.addSeparator()
        quit_action = tray_menu.addAction("Quit")
        quit_action.triggered.connect(self.quit_app)
        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.activated.connect(self.on_tray_activated)

        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray_icon.show()

    def on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:  # left click
            self.setVisible(not self.isVisible())
            if self.isVisible():
                self.raise_()
                self.activateWindow()

    def quit_app(self):
        self.close()

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
        try:
            check_password_auth_ready(cfg)
        except RuntimeError as e:
            QMessageBox.warning(self, "Password authentication not ready", str(e))
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
        self.prev_complete = {}
        self.update_connect_ui(connected=True)
        self.down_limit_spin.setEnabled(True)
        self.up_limit_spin.setEnabled(True)
        self.poll_now()
        self.poll_timer.start(cfg["poll_interval"] * 1000)
        self.poll_disk_space()
        self.poll_global_limits()
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
        self.down_limit_spin.setEnabled(False)
        self.up_limit_spin.setEnabled(False)
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
        self.down_limit_spin.setEnabled(False)
        self.up_limit_spin.setEnabled(False)

    def update_connect_ui(self, connected):
        self.connect_btn.setText("Disconnect" if connected else "Connect")
        self.conn_label.setText("Connected" if connected else "Disconnected")

    def open_settings(self):
        dlg = SettingsDialog(self, self.cfg)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            was_connected = self.tunnel is not None and self.tunnel.is_running()
            if was_connected:
                self.disconnect_all()
            self.cfg = dlg.get_config()
            save_config(self.cfg)
            if was_connected:
                self.connect_all()

    def open_automation_dialog(self):
        if not self.cfg.get("autograb_config_path"):
            QMessageBox.information(
                self, "Automation not configured",
                "Set the remote config path (and activity log path) in Settings first "
                "-- run rtorrent_autograb.py --init on the server to see the defaults. "
                "It's reached over the same SSH connection as rtorrent, no server-side "
                "webserver required."
            )
            self.open_settings()
            if not self.cfg.get("autograb_config_path"):
                return
        client = RemoteConfigClient(self.cfg)
        dlg = AutomationDialog(
            self, client,
            self.cfg.get("autograb_config_path", ""),
            self.cfg.get("autograb_log_path", ""),
        )
        dlg.exec()

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

    def on_disk_timer(self):
        self.poll_disk_space()
        self.poll_global_limits()

    def on_disk_space_ready(self, avail, total):
        self.disk_space_label.setText(f"Free: {human_bytes(avail)} / {human_bytes(total)}")
        self.disk_space_label.setToolTip(f"Queried path: {self.cfg.get('disk_path', '/')}")

    def on_disk_space_error(self, msg):
        self.disk_space_label.setText("Free: n/a")
        self.disk_space_label.setToolTip(f"Could not read disk space: {msg}")
        self.status_bar.showMessage(f"Disk space check failed: {msg}", 5000)

    def poll_global_limits(self):
        if not self.rpc:
            return
        # Don't clobber a value the person is actively typing.
        if self.down_limit_spin.hasFocus() or self.up_limit_spin.hasFocus():
            return
        self.limits_worker = GlobalLimitsWorker(self.rpc)
        self.limits_worker.result_ready.connect(self.on_global_limits_ready)
        self.limits_worker.error.connect(self.on_global_limits_error)
        self.limits_worker.start()

    def on_global_limits_ready(self, down_bytes, up_bytes):
        self.down_limit_spin.blockSignals(True)
        self.up_limit_spin.blockSignals(True)
        self.down_limit_spin.setValue(down_bytes // 1024)
        self.up_limit_spin.setValue(up_bytes // 1024)
        self.down_limit_spin.blockSignals(False)
        self.up_limit_spin.blockSignals(False)

    def on_global_limits_error(self, msg):
        self.status_bar.showMessage(f"Could not read speed limits: {msg}", 5000)

    def apply_global_limit(self, direction):
        if not self.rpc:
            return
        spin = self.down_limit_spin if direction == "down" else self.up_limit_spin
        bytes_per_sec = spin.value() * 1024
        self.set_limit_worker = SetGlobalLimitWorker(self.rpc, direction, bytes_per_sec)
        self.set_limit_worker.finished_ok.connect(self.on_limit_set)
        self.set_limit_worker.error.connect(self.on_limit_set_error)
        self.set_limit_worker.start()

    def on_limit_set(self, direction, bytes_per_sec):
        label = "Download" if direction == "down" else "Upload"
        shown = "unlimited" if bytes_per_sec == 0 else human_speed(bytes_per_sec)
        self.status_bar.showMessage(f"{label} limit set to {shown}", 4000)

    def on_limit_set_error(self, msg):
        self.status_bar.showMessage(f"Setting speed limit failed: {msg}", 6000)

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

    def check_completions(self, torrents):
        """Compares each torrent's complete/incomplete state against the
        previous poll and fires a tray notification for any that just
        finished. Torrents already complete at startup (before we have a
        baseline) are not reported, only transitions seen while running."""
        have_baseline = bool(self.prev_complete)
        new_state = {}
        for t in torrents:
            h = t["hash"]
            complete = bool(t["complete"])
            new_state[h] = complete
            if have_baseline and complete and not self.prev_complete.get(h, False):
                self.notify_download_complete(t["name"])
        self.prev_complete = new_state

    def notify_download_complete(self, name):
        self.status_bar.showMessage(f"Download complete: {name}", 6000)
        if QSystemTrayIcon.isSystemTrayAvailable() and self.tray_icon.isVisible():
            self.tray_icon.showMessage(
                "Download complete", name, QSystemTrayIcon.MessageIcon.Information, 6000
            )

    def on_data_ready(self, torrents):
        self.poll_fail_count = 0
        self.torrents_by_hash = {t["hash"]: t for t in torrents}
        self.check_completions(torrents)

        # Selection is tracked by hash rather than row position, since row
        # indices can shift as the diff-update inserts/removes/reorders rows.
        previously_selected = set(self.selected_hashes())

        self.torrent_model.update_torrents(torrents)

        if previously_selected:
            self.restore_selection(previously_selected)

        total_down, total_up = self.torrent_model.totals()
        total_count = len(torrents)
        visible = self.proxy_model.rowCount()
        count_text = f"{total_count} torrents" if visible == total_count else f"{visible} of {total_count} torrents"
        self.status_bar.showMessage(
            f"{count_text}  |  Total: \u2193 {human_speed(total_down)}  "
            f"\u2191 {human_speed(total_up)}"
        )

    def restore_selection(self, hashes):
        selection_model = self.table.selectionModel()
        selection_model.blockSignals(True)
        selection_model.clearSelection()
        for h in hashes:
            source_row = self.torrent_model.row_of_hash(h)
            if source_row is None:
                continue
            proxy_index = self.proxy_model.mapFromSource(self.torrent_model.index(source_row, 0))
            if proxy_index.isValid():
                selection_model.select(
                    proxy_index,
                    selection_model.SelectionFlag.Select | selection_model.SelectionFlag.Rows,
                )
        selection_model.blockSignals(False)

    # ---------------- actions ----------------

    def on_search_text_changed(self, text):
        # Debounced: re-filtering thousands of rows on every single keystroke
        # is wasted work, so wait for a short pause in typing instead.
        self._filter_timer.start(250)

    def apply_filter(self):
        query = self.search_edit.text().strip()
        self.proxy_model.setFilterFixedString(query)

    def selected_hashes(self):
        hashes = []
        for proxy_index in self.table.selectionModel().selectedRows(0):
            source_index = self.proxy_model.mapToSource(proxy_index)
            h = self.torrent_model.data(source_index, HASH_ROLE)
            if h:
                hashes.append(h)
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
        rehash_action = menu.addAction("Rehash (recheck data)")
        menu.addSeparator()
        info_action = None
        if len(hashes) == 1:
            info_action = menu.addAction("Tracker && Peer Info...")
            menu.addSeparator()
        erase_action = menu.addAction("Remove from list (keep files)")
        delete_action = menu.addAction("Remove and delete files...")
        action = menu.exec(self.table.viewport().mapToGlobal(pos))
        if action == start_action:
            self.run_action("d.start", hashes)
        elif action == stop_action:
            self.run_action("d.stop", hashes)
        elif action == rehash_action:
            self.rehash_torrents(hashes)
        elif info_action is not None and action == info_action:
            self.open_tracker_peer_dialog(hashes[0])
        elif action == erase_action:
            self.erase_torrents(hashes, delete_files=False)
        elif action == delete_action:
            self.erase_torrents(hashes, delete_files=True)

    def show_tracker_peer_info(self, proxy_index):
        if not self.rpc or not proxy_index.isValid():
            return
        source_index = self.proxy_model.mapToSource(proxy_index)
        h = self.torrent_model.data(source_index, HASH_ROLE)
        if h:
            self.open_tracker_peer_dialog(h)

    def eventFilter(self, obj, event):
        if obj is self.table and event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Right:
            proxy_index = self.table.currentIndex()
            if proxy_index.isValid() and self.rpc:
                source_index = self.proxy_model.mapToSource(proxy_index)
                h = self.torrent_model.data(source_index, HASH_ROLE)
                if h:
                    self.open_tracker_peer_dialog(h)
                    return True
        return super().eventFilter(obj, event)

    def open_tracker_peer_dialog(self, torrent_hash):
        t = self.torrents_by_hash.get(torrent_hash)
        name = t["name"] if t else torrent_hash
        dlg = TrackerPeerDialog(self, self.rpc, torrent_hash, name, self.cfg.get("poll_interval", 3))
        dlg.exec()

    def run_action(self, method, hashes):
        self.action_worker = ActionWorker(self.rpc, method, hashes)
        self.action_worker.finished_ok.connect(self.poll_now)
        self.action_worker.error.connect(lambda msg: self.status_bar.showMessage(f"Action failed: {msg}", 5000))
        self.action_worker.start()

    def rehash_torrents(self, hashes):
        names = []
        for h in hashes:
            t = self.torrents_by_hash.get(h)
            names.append(t["name"] if t else h)
        preview = "\n".join(f"  \u2022 {n}" for n in names[:10])
        if len(names) > 10:
            preview += f"\n  ...and {len(names) - 10} more"
        confirm = QMessageBox.question(
            self,
            "Rehash torrent(s)?",
            "This tells rtorrent to recheck the data on disk against the "
            "torrent's hashes. It can take a while for large torrents and "
            "the torrent will be marked incomplete until the check finishes.\n\n"
            f"{preview}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        count = len(hashes)
        label = "1 torrent" if count == 1 else f"{count} torrents"
        self.rehash_worker = ActionWorker(self.rpc, "d.check_hash", hashes)
        self.rehash_worker.finished_ok.connect(
            lambda: self.status_bar.showMessage(f"Rehash started for {label}.", 5000)
        )
        self.rehash_worker.finished_ok.connect(self.poll_now)
        self.rehash_worker.error.connect(
            lambda msg: self.status_bar.showMessage(f"Rehash failed: {msg}", 5000)
        )
        self.rehash_worker.start()

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
            self, title, text, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No
        )
        if confirm != QMessageBox.StandardButton.Yes:
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

    def add_torrent_file(self):
        if not self.rpc:
            QMessageBox.warning(self, "Not connected", "Connect to the server first.")
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Select torrent file", str(Path.home()), "Torrent files (*.torrent);;All files (*)"
        )
        if not path:
            return
        try:
            data = Path(path).read_bytes()
        except OSError as e:
            QMessageBox.warning(self, "Could not read file", str(e))
            return
        remote_dir = self.cfg.get("download_dir", "")
        self.file_add_worker = AddTorrentFileWorker(self.rpc, data, remote_dir, start=True)
        self.file_add_worker.finished_ok.connect(self.on_torrent_file_added)
        self.file_add_worker.error.connect(self.on_torrent_file_add_error)
        self.file_add_worker.start()

    def on_torrent_file_added(self):
        self.status_bar.showMessage("Torrent added", 4000)
        self.poll_now()

    def on_torrent_file_add_error(self, msg):
        self.status_bar.showMessage(f"Add torrent file failed: {msg}", 6000)
        QMessageBox.warning(self, "Add torrent file failed", msg)

    def closeEvent(self, event):
        self.disconnect_all()
        self.tray_icon.hide()
        event.accept()
        QApplication.instance().quit()


def main():
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
