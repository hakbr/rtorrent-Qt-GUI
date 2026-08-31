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
    QSystemTrayIcon, QStyle
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer, QProcess, QProcessEnvironment, QEvent
from PyQt6.QtGui import QIcon, QAction

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
            "Passordinnlogging er valt, men det er ikkje lagra noko passord. "
            "Set eitt i innstillingane."
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
            "Passordinnlogging krev verktøyet 'sshpass', som ikkje er "
            "installert. Installer det med: sudo apt install sshpass"
        )
    if not get_saved_password(cfg):
        raise RuntimeError(
            "Passordinnlogging er valt, men det er ikkje lagra noko passord. "
            "Set eitt i innstillingane."
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
            raise RPCError("Misforma SCGI-svar frå rtorrent")
        body = raw[sep + 4:]
        try:
            result, _ = xmlrpclib.loads(body)
        except xmlrpclib.Fault as e:
            raise RPCError(f"rtorrent RPC-feil: {e}")
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
        return "Sjekkar"
    # Check active/complete state BEFORE looking at d.message: rtorrent does
    # not reliably clear that field once a torrent recovers, so a torrent
    # that's happily seeding or downloading right now can still be carrying
    # a stale tracker/IO message from hours ago. Only treat it as an error
    # once we know the torrent isn't actually active.
    if t["is_active"] and t["state"]:
        return "Deler" if t["complete"] else "Lastar ned"
    if t["message"]:
        return "Feil"
    return "Pausa"


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
            QProcess.ProcessError.FailedToStart: "ssh klarte ikkje å starte (er openssh-client installert og på PATH?)",
            QProcess.ProcessError.Crashed: "ssh-prosessen krasja",
            QProcess.ProcessError.Timedout: "ssh-prosessen fekk tidsavbrot",
        }
        msg = names.get(err, f"ssh-prosessfeil ({err})")
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
                            f"Nektar å slette '{name}': rtorrent rapporterte ikkje ei "
                            f"trygg absolutt sti (fekk {base_path!r})."
                        )

                self.progress.emit(f"Fjernar {name}...")
                self.rpc.call("d.erase", item["hash"])

                if self.delete_files:
                    self.progress.emit(f"Slettar filer for {name}...")
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
                            f"Fjerna '{name}' frå rtorrent, men sletting av filene "
                            f"feila: {result.stderr.strip() or 'ukjend feil'}"
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
                raise RuntimeError(result.stderr.strip() or "df-kommandoen feila")
            # df prints a header row even with --output; the data is the
            # last non-empty line (defensive against unexpected extra lines).
            lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
            if len(lines) < 2:
                raise RuntimeError(f"Uventa df-utdata: {result.stdout!r}")
            parts = lines[-1].split()
            if len(parts) < 2:
                raise RuntimeError(f"Uventa df-datalinje: {lines[-1]!r}")
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
        self.setWindowTitle("Tilkoplingsinnstillingar")
        self.cfg = dict(cfg)

        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["Unix-socket (tilrådd)", "TCP-port"])
        self.mode_combo.setCurrentIndex(0 if cfg["mode"] == "unix" else 1)
        self.mode_combo.currentIndexChanged.connect(self.update_mode_fields)

        self.ssh_host_edit = QLineEdit(cfg["ssh_host"])
        self.ssh_port_spin = QSpinBox()
        self.ssh_port_spin.setRange(1, 65535)
        self.ssh_port_spin.setValue(cfg["ssh_port"])
        self.ssh_user_edit = QLineEdit(cfg["ssh_user"])

        self.auth_combo = QComboBox()
        self.auth_combo.addItems(["SSH-nøkkel (utan passord, tilrådd)", "Passord"])
        self.auth_combo.setCurrentIndex(1 if cfg.get("auth_method") == "password" else 0)
        self.auth_combo.currentIndexChanged.connect(self.update_auth_fields)

        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        has_saved = bool(cfg.get("ssh_password_saved"))
        if has_saved:
            self.password_edit.setPlaceholderText("Lagra — lat stå tomt for å behalde det")
        else:
            self.password_edit.setPlaceholderText("Skriv inn passord")
        self.show_password_check = QCheckBox("Vis")
        self.show_password_check.toggled.connect(
            lambda on: self.password_edit.setEchoMode(QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password)
        )
        self.forget_password_btn = QPushButton("Gløym lagra passord")
        self.forget_password_btn.setEnabled(has_saved)
        self.forget_password_btn.clicked.connect(self.forget_password)
        self._forget_password = False

        password_row = QHBoxLayout()
        password_row.addWidget(self.password_edit)
        password_row.addWidget(self.show_password_check)
        password_row_w = QWidget()
        password_row_w.setLayout(password_row)

        auth_note_text = (
            "Passordet vert lagra i nøkkelringen til operativsystemet via "
            "'keyring'-pakken."
            if KEYRING_AVAILABLE else
            "MERK: 'keyring'-pakken er ikkje installert, så passordet vert "
            "lagra i klartekst i config.json (køyr pip install keyring, eller "
            "apt install python3-keyring, for å lagre det trygt i staden)."
        )
        self.auth_note_label = QLabel(auth_note_text)
        self.auth_note_label.setWordWrap(True)
        self.sshpass_note_label = QLabel(
            "MERK: passordinnlogging krev òg verktøyet 'sshpass' "
            "(sudo apt install sshpass)."
        )
        if sshpass_available():
            self.sshpass_note_label.hide()

        self.remote_socket_edit = QLineEdit(cfg["remote_socket_path"])
        self.remote_socket_edit.setPlaceholderText("/home/dinbrukar/.rtorrent.sock")

        self.remote_host_edit = QLineEdit(cfg["remote_host"])
        self.remote_port_spin = QSpinBox()
        self.remote_port_spin.setRange(1, 65535)
        self.remote_port_spin.setValue(cfg["remote_port"])

        self.poll_spin = QSpinBox()
        self.poll_spin.setRange(1, 60)
        self.poll_spin.setValue(cfg["poll_interval"])
        self.poll_spin.setSuffix(" s")

        self.disk_path_edit = QLineEdit(cfg.get("disk_path", "/"))
        self.disk_path_edit.setPlaceholderText("/ (eller eit monteringspunkt, t.d. /mnt/downloads)")

        self.download_dir_edit = QLineEdit(cfg.get("download_dir", ""))
        self.download_dir_edit.setPlaceholderText(
            "~/downloads (lat stå tomt for å bruke rtorrent sin eigen "
            "standardmappe)"
        )

        form = QFormLayout()
        form.addRow("RPC-transport:", self.mode_combo)
        form.addRow(QLabel("<b>SSH</b>"))
        form.addRow("Vert / IP:", self.ssh_host_edit)
        form.addRow("SSH-port:", self.ssh_port_spin)
        form.addRow("Brukarnamn:", self.ssh_user_edit)
        form.addRow("Autentisering:", self.auth_combo)
        form.addRow("Passord:", password_row_w)
        form.addRow("", self.forget_password_btn)
        form.addRow("", self.auth_note_label)
        form.addRow("", self.sshpass_note_label)
        form.addRow(QLabel("<b>rtorrent RPC (unix-socket)</b>"))
        form.addRow("Ekstern socket-sti:", self.remote_socket_edit)
        form.addRow(QLabel("<b>rtorrent RPC (TCP, berre om du ikkje brukar unix-socket)</b>"))
        form.addRow("Ekstern bind-vert:", self.remote_host_edit)
        form.addRow("Ekstern port:", self.remote_port_spin)
        form.addRow(QLabel("<b>Oppdatering</b>"))
        form.addRow("Oppdater kvart:", self.poll_spin)
        form.addRow("Sti for diskbruk:", self.disk_path_edit)
        form.addRow(QLabel("<b>Nye torrentar</b>"))
        form.addRow("Standard nedlastingsmappe:", self.download_dir_edit)

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
        self.password_edit.setPlaceholderText("Skriv inn passord")
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

        new_password = self.password_edit.text()
        if self._forget_password and not new_password:
            set_saved_password(cfg, None)
        elif new_password:
            set_saved_password(cfg, new_password)
        # else: keep whatever was already saved (ssh_password_saved untouched)
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
    "Namn", "Status", "Framdrift", "Nedlastingsfart", "Opplastingsfart",
    "Tid att", "Medbrukarar", "Delingsforhold", "Storleik", "Lasta ned", "Lasta opp",
]


# --------------------------------------------------------------------------
# Tracker / peer info dialog
# --------------------------------------------------------------------------

TRACKER_COLUMNS = ["URL", "Type", "Slått på", "Delarar", "Nedlastarar"]
PEER_COLUMNS = ["Adresse", "Klient", "Nedlastingsfart", "Opplastingsfart", "Framdrift", "Kryptert"]

TRACKER_TYPES = {0: "-", 1: "HTTP", 2: "UDP", 3: "DHT"}


class TrackerPeerDialog(QDialog):
    """Shows trackers and connected peers for one torrent, refreshing itself
    on a timer for as long as it's open."""

    def __init__(self, parent, rpc, torrent_hash, torrent_name, poll_interval):
        super().__init__(parent)
        self.setWindowTitle(f"Sporar- og medbrukarinfo — {torrent_name}")
        self.resize(720, 480)
        self.rpc = rpc
        self.torrent_hash = torrent_hash
        self.worker = None

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("<b>Sporarar</b>"))
        self.tracker_table = QTableWidget(0, len(TRACKER_COLUMNS))
        self.tracker_table.setHorizontalHeaderLabels(TRACKER_COLUMNS)
        self.tracker_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tracker_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tracker_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        layout.addWidget(self.tracker_table, 1)

        layout.addWidget(QLabel("<b>Medbrukarar</b>"))
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
        self.status_label.setText(f"{len(trackers)} sporarar, {len(peers)} medbrukarar")

        self.tracker_table.setRowCount(len(trackers))
        for row, t in enumerate(trackers):
            self.tracker_table.setItem(row, 0, QTableWidgetItem(t["url"]))
            self.tracker_table.setItem(row, 1, QTableWidgetItem(TRACKER_TYPES.get(t["type"], str(t["type"]))))
            self.tracker_table.setItem(row, 2, QTableWidgetItem("Ja" if t["is_enabled"] else "Nei"))
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
            self.peer_table.setItem(row, 5, QTableWidgetItem("Ja" if p["is_encrypted"] else "Nei"))

    def on_error(self, msg):
        self.status_label.setText(f"Oppdatering feila: {msg}")

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

        self.build_ui()
        self.build_tray_icon()
        self.update_connect_ui(connected=False)

    def build_ui(self):
        central = QWidget()
        layout = QVBoxLayout(central)

        top_row = QHBoxLayout()
        self.connect_btn = QPushButton("Kople til")
        self.connect_btn.clicked.connect(self.toggle_connection)
        settings_btn = QPushButton("Innstillingar...")
        settings_btn.clicked.connect(self.open_settings)
        refresh_btn = QPushButton("Oppdater no")
        refresh_btn.clicked.connect(self.poll_now)
        top_row.addWidget(self.connect_btn)
        top_row.addWidget(settings_btn)
        top_row.addWidget(refresh_btn)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Filtrer etter namn...")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self.apply_filter)
        self.search_edit.setMinimumWidth(220)
        top_row.addWidget(self.search_edit)
        top_row.addStretch()
        self.conn_label = QLabel("Fråkopla")
        top_row.addWidget(self.conn_label)
        layout.addLayout(top_row)

        magnet_row = QHBoxLayout()
        magnet_label = QLabel("Legg til magnet:")
        self.magnet_edit = QLineEdit()
        self.magnet_edit.setPlaceholderText("magnet:?xt=urn:btih:...")
        self.magnet_edit.returnPressed.connect(self.add_magnet)
        add_magnet_btn = QPushButton("Legg til torrent")
        add_magnet_btn.clicked.connect(self.add_magnet)
        add_file_btn = QPushButton("Legg til .torrent-fil...")
        add_file_btn.clicked.connect(self.add_torrent_file)
        magnet_row.addWidget(magnet_label)
        magnet_row.addWidget(self.magnet_edit)
        magnet_row.addWidget(add_magnet_btn)
        magnet_row.addWidget(add_file_btn)
        layout.addLayout(magnet_row)

        limits_row = QHBoxLayout()
        limits_row.addWidget(QLabel("Globale grenser (KiB/s, 0 = uavgrensa):"))
        limits_row.addWidget(QLabel("Ned:"))
        self.down_limit_spin = QSpinBox()
        self.down_limit_spin.setRange(0, 999999)
        self.down_limit_spin.setSpecialValueText("Uavgrensa")
        self.down_limit_spin.editingFinished.connect(lambda: self.apply_global_limit("down"))
        limits_row.addWidget(self.down_limit_spin)
        limits_row.addWidget(QLabel("Opp:"))
        self.up_limit_spin = QSpinBox()
        self.up_limit_spin.setRange(0, 999999)
        self.up_limit_spin.setSpecialValueText("Uavgrensa")
        self.up_limit_spin.editingFinished.connect(lambda: self.apply_global_limit("up"))
        limits_row.addWidget(self.up_limit_spin)
        self.down_limit_spin.setEnabled(False)
        self.up_limit_spin.setEnabled(False)
        limits_row.addStretch()
        layout.addLayout(limits_row)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        base_col_width = 90
        self.table.setColumnWidth(0, base_col_width * 2)  # Name: twice the width of the rest
        for col in range(1, len(COLUMNS)):
            self.table.setColumnWidth(col, base_col_width)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSortingEnabled(True)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.show_context_menu)
        self.table.cellDoubleClicked.connect(self.show_tracker_peer_info)
        self.table.installEventFilter(self)
        layout.addWidget(self.table)

        self.setCentralWidget(central)

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.disk_space_label = QLabel("Ledig: \u2013")
        self.disk_space_label.setContentsMargins(0, 0, 8, 0)
        self.status_bar.addPermanentWidget(self.disk_space_label)

    def build_tray_icon(self):
        icon = QIcon.fromTheme("network-transmit-receive")
        if icon.isNull():
            icon = self.style().standardIcon(QStyle.StandardPixmap.SP_DriveNetIcon)
        self.tray_icon = QSystemTrayIcon(icon, self)
        self.tray_icon.setToolTip("rtorrent GUI")

        tray_menu = QMenu()
        show_action = tray_menu.addAction("Vis")
        show_action.triggered.connect(self.showNormal)
        hide_action = tray_menu.addAction("Gøym")
        hide_action.triggered.connect(self.hide)
        tray_menu.addSeparator()
        quit_action = tray_menu.addAction("Avslutt")
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
            QMessageBox.warning(self, "Manglar innstillingar", "Fyll inn SSH-vert og brukarnamn i innstillingane først.")
            self.open_settings()
            return
        if cfg["mode"] == "unix" and not cfg["remote_socket_path"]:
            QMessageBox.warning(self, "Manglar innstillingar", "Set den eksterne socket-stien i innstillingane.")
            self.open_settings()
            return
        try:
            check_password_auth_ready(cfg)
        except RuntimeError as e:
            QMessageBox.warning(self, "Passordinnlogging er ikkje klar", str(e))
            self.open_settings()
            return

        self.conn_label.setText("Koplar til...")
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
                output = ("(ingen utdata fanga opp — tunnelen kom aldri opp innan "
                           "tidsgrensa, men ssh melde heller ikkje frå om nokon feil. "
                           "Dette kan tyde på at han framleis forhandlar, eller at ein "
                           "gamal tunnelprosess frå ei tidlegare køyring okkuperer den "
                           "lokale socketen/porten. Prøv å køyre kommandoen nedanfor for "
                           "hand i ein terminal for å sjå kva som eigentleg skjer.)")
            QMessageBox.warning(
                self, "Tilkopling feila",
                "Klarte ikkje å opprette SSH-tunnelen til RPC-socketen til rtorrent.\n\n"
                "Sjekk: at SSH-nøkkelinnlogging fungerer for denne verten, at rtorrent "
                "køyrer med RPC slått på, og at stien til ekstern socket/port er rett.\n\n"
                f"Kommando køyrd:\n{command}\n\n"
                f"ssh-utdata:\n{output}"
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
            "SSH-tunnelen avslutta uventa — sambandet ser ut til å ha vorte brote "
            "i andre enden (t.d. ei nettverksendring, at maskina gjekk i dvale, "
            "eller at tenaren starta på nytt).",
            output,
        )

    def handle_connection_lost(self, reason, detail=""):
        self.poll_timer.stop()
        self.disk_timer.stop()
        self.disk_space_label.setText("Ledig: \u2013")
        self.rpc = None
        if self.tunnel:
            self.tunnel.stop()
        self.tunnel = None
        self.poll_fail_count = 0
        self.update_connect_ui(connected=False)
        self.down_limit_spin.setEnabled(False)
        self.up_limit_spin.setEnabled(False)
        self.status_bar.showMessage("Mista sambandet", 5000)
        msg = reason
        if detail:
            msg += f"\n\nssh-utdata:\n{detail}"
        msg += "\n\nTrykk Kople til for å kople til på nytt."
        QMessageBox.warning(self, "Mista sambandet", msg)

    def disconnect_all(self):
        self.poll_timer.stop()
        self.disk_timer.stop()
        self.disk_space_label.setText("Ledig: \u2013")
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
        self.connect_btn.setText("Kople frå" if connected else "Kople til")
        self.conn_label.setText("Tilkopla" if connected else "Fråkopla")

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
        self.disk_space_label.setText(f"Ledig: {human_bytes(avail)} / {human_bytes(total)}")
        self.disk_space_label.setToolTip(f"Spurt sti: {self.cfg.get('disk_path', '/')}")

    def on_disk_space_error(self, msg):
        self.disk_space_label.setText("Ledig: utilgjengeleg")
        self.disk_space_label.setToolTip(f"Klarte ikkje å lese diskplass: {msg}")
        self.status_bar.showMessage(f"Sjekk av diskplass feila: {msg}", 5000)

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
        self.status_bar.showMessage(f"Klarte ikkje å lese fartsgrenser: {msg}", 5000)

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
        label = "Nedlastings" if direction == "down" else "Opplastings"
        shown = "uavgrensa" if bytes_per_sec == 0 else human_speed(bytes_per_sec)
        self.status_bar.showMessage(f"{label}grensa sett til {shown}", 4000)

    def on_limit_set_error(self, msg):
        self.status_bar.showMessage(f"Klarte ikkje å setje fartsgrense: {msg}", 6000)

    def on_poll_error(self, msg):
        self.poll_fail_count += 1
        self.status_bar.showMessage(f"Feil ved oppdatering: {msg}", 5000)
        if self.poll_fail_count >= 3:
            self.handle_connection_lost(
                "Mista kontakten med rtorrent over tunnelen — sambandet ser ut til å "
                "vere brote i andre enden, sjølv om ssh-prosessen framleis køyrer "
                "lokalt (t.d. etter dvale eller ei nettverksendring).",
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
        self.status_bar.showMessage(f"Nedlasting fullført: {name}", 6000)
        if QSystemTrayIcon.isSystemTrayAvailable() and self.tray_icon.isVisible():
            self.tray_icon.showMessage(
                "Nedlasting fullført", name, QSystemTrayIcon.MessageIcon.Information, 6000
            )

    def on_data_ready(self, torrents):
        self.poll_fail_count = 0
        self.torrents_by_hash = {t["hash"]: t for t in torrents}
        self.check_completions(torrents)

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
            name_item.setData(Qt.ItemDataRole.UserRole, t["hash"])
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
                if item and item.data(Qt.ItemDataRole.UserRole) in previously_selected:
                    self.table.selectRow(row)

        self.table.blockSignals(False)

        self.apply_filter()
        visible = sum(1 for row in range(self.table.rowCount()) if not self.table.isRowHidden(row))
        count_text = f"{len(torrents)} torrentar" if visible == len(torrents) else f"{visible} av {len(torrents)} torrentar"
        self.status_bar.showMessage(
            f"{count_text}  |  Totalt: \u2193 {human_speed(total_down)}  "
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
                hashes.append(item.data(Qt.ItemDataRole.UserRole))
        return hashes

    def show_context_menu(self, pos):
        if not self.rpc:
            return
        hashes = self.selected_hashes()
        if not hashes:
            return
        menu = QMenu(self)
        start_action = menu.addAction("Start")
        stop_action = menu.addAction("Stopp (pause)")
        rehash_action = menu.addAction("Sjekk hash på nytt (kontroller data)")
        menu.addSeparator()
        info_action = None
        if len(hashes) == 1:
            info_action = menu.addAction("Sporar- && medbrukarinfo...")
            menu.addSeparator()
        erase_action = menu.addAction("Fjern frå lista (behald filene)")
        delete_action = menu.addAction("Fjern og slett filer...")
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

    def show_tracker_peer_info(self, row, column):
        item = self.table.item(row, 0)
        if not item or not self.rpc:
            return
        self.open_tracker_peer_dialog(item.data(Qt.ItemDataRole.UserRole))

    def eventFilter(self, obj, event):
        if obj is self.table and event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Right:
            row = self.table.currentRow()
            if row >= 0 and self.rpc:
                item = self.table.item(row, 0)
                if item:
                    self.open_tracker_peer_dialog(item.data(Qt.ItemDataRole.UserRole))
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
        self.action_worker.error.connect(lambda msg: self.status_bar.showMessage(f"Handling feila: {msg}", 5000))
        self.action_worker.start()

    def rehash_torrents(self, hashes):
        names = []
        for h in hashes:
            t = self.torrents_by_hash.get(h)
            names.append(t["name"] if t else h)
        preview = "\n".join(f"  \u2022 {n}" for n in names[:10])
        if len(names) > 10:
            preview += f"\n  ...og {len(names) - 10} til"
        confirm = QMessageBox.question(
            self,
            "Sjekk torrent(ar) på nytt?",
            "Dette ber rtorrent om å kontrollere dataa på disken mot "
            "hash-verdiane til torrenten. Det kan ta ei stund for store "
            "torrentar, og torrenten vert markert som ufullstendig til "
            "kontrollen er ferdig.\n\n"
            f"{preview}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        count = len(hashes)
        label = "1 torrent" if count == 1 else f"{count} torrentar"
        self.rehash_worker = ActionWorker(self.rpc, "d.check_hash", hashes)
        self.rehash_worker.finished_ok.connect(
            lambda: self.status_bar.showMessage(f"Sjekk på nytt starta for {label}.", 5000)
        )
        self.rehash_worker.finished_ok.connect(self.poll_now)
        self.rehash_worker.error.connect(
            lambda msg: self.status_bar.showMessage(f"Sjekk på nytt feila: {msg}", 5000)
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
            names += f"\n  ...og {len(items) - 10} til"

        if delete_files:
            title = "Slette torrentar og filer?"
            text = (
                "Dette slettar dei nedlasta filene frå tenaren for godt, i "
                "tillegg til å fjerne torrenten(ane) frå rtorrent.\n\n"
                "Dette kan ikkje angrast.\n\n"
                f"{names}"
            )
        else:
            title = "Fjerne frå lista?"
            text = (
                "Dette fjernar torrenten(ane) frå lista til rtorrent. Dei "
                "nedlasta filene vert verande på disken.\n\n"
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
        self.status_bar.showMessage("Ferdig", 3000)
        self.poll_now()

    def on_delete_error(self, msg):
        self.status_bar.showMessage(f"Sletting feila: {msg}", 6000)
        QMessageBox.warning(self, "Sletting feila", msg)

    def add_magnet(self):
        magnet = self.magnet_edit.text().strip()
        if not self.rpc:
            QMessageBox.warning(self, "Ikkje tilkopla", "Kople til tenaren først.")
            return
        if not magnet.startswith("magnet:"):
            QMessageBox.warning(self, "Ugyldig magnetlenkje", "Skriv inn ei gyldig magnet:-lenkje.")
            return
        self.magnet_add_worker = AddMagnetWorker(self.rpc, magnet, start=True)
        self.magnet_add_worker.finished_ok.connect(self.on_magnet_added)
        self.magnet_add_worker.error.connect(self.on_magnet_add_error)
        self.magnet_add_worker.start()

    def on_magnet_added(self):
        self.magnet_edit.clear()
        self.status_bar.showMessage("Torrent lagt til", 4000)
        self.poll_now()

    def on_magnet_add_error(self, msg):
        self.status_bar.showMessage(f"Kunne ikkje leggje til torrent: {msg}", 6000)
        QMessageBox.warning(self, "Kunne ikkje leggje til torrent", msg)

    def add_torrent_file(self):
        if not self.rpc:
            QMessageBox.warning(self, "Ikkje tilkopla", "Kople til tenaren først.")
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Vel torrent-fil", str(Path.home()), "Torrent-filer (*.torrent);;Alle filer (*)"
        )
        if not path:
            return
        try:
            data = Path(path).read_bytes()
        except OSError as e:
            QMessageBox.warning(self, "Klarte ikkje å lese fila", str(e))
            return
        remote_dir = self.cfg.get("download_dir", "")
        self.file_add_worker = AddTorrentFileWorker(self.rpc, data, remote_dir, start=True)
        self.file_add_worker.finished_ok.connect(self.on_torrent_file_added)
        self.file_add_worker.error.connect(self.on_torrent_file_add_error)
        self.file_add_worker.start()

    def on_torrent_file_added(self):
        self.status_bar.showMessage("Torrent lagt til", 4000)
        self.poll_now()

    def on_torrent_file_add_error(self, msg):
        self.status_bar.showMessage(f"Kunne ikkje leggje til torrent-fil: {msg}", 6000)
        QMessageBox.warning(self, "Kunne ikkje leggje til torrent-fil", msg)

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
