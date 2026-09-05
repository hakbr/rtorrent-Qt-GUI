#!/usr/bin/env python3
"""
rtorrent-autograb
==================
A single-file server-side daemon that:

  1. Watches one or more IRC "announce" channels for new-release messages.
  2. Polls one or more RSS/Atom feeds for new items.
  3. Runs each candidate release through user-defined filters (name
     keywords, quality tags, codec tags, regex, size range).
  4. Adds anything that matches straight into rtorrent over its XML-RPC/SCGI
     interface (the same interface ruTorrent/pyrocore/rtorrent_gui.py use).

This process does NOT open any network listener of its own -- no web
server, no extra port to firewall. Its config is a plain JSON file, and it
watches that file's mtime and hot-reloads whenever it changes, so it can be
edited by hand, by a script, or (see rtorrent_gui.py's "Automation..."
dialog) remotely by overwriting the file over the SSH connection you
already use to reach rtorrent -- the same `ssh user@host "cat > path"`
mechanism the GUI uses for everything else. Recent grabs are appended to a
small JSON activity log next to the config, for the same reason: the GUI
reads it with a plain `ssh ... cat`, no API needed.

Runs entirely on the same machine as rtorrent (or anywhere with RPC access
to it). No third-party dependencies -- standard library only.

Quick start
-----------
    python3 rtorrent_autograb.py --init
        # writes ~/.config/rtorrent-autograb/config.json and prints the
        # paths to paste into the GUI's Settings dialog (Automation
        # section). The GUI reaches them over the SSH connection it
        # already has configured -- nothing else to set up.

    # edit the rtorrent connection section (socket path / host+port), or
    # just do everything from the GUI's Automation dialog afterwards.

    python3 rtorrent_autograb.py
        # runs in the foreground; Ctrl-C to stop. Put it behind systemd
        # or `screen -dm` for a real deployment (see --help).

Config lives in a single JSON file (default: ~/.config/rtorrent-autograb/
config.json, override with --config). Whenever that file's contents
change on disk -- from any source -- this process notices within a few
seconds and restarts only the watchers whose settings actually changed.
"""

import argparse
import copy
import hashlib
import json
import logging
import re
import secrets
import socket
import ssl
import threading
import time
import uuid
import xml.etree.ElementTree as ET
import xmlrpc.client as xmlrpclib
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

# --------------------------------------------------------------------------
# Paths & logging
# --------------------------------------------------------------------------

DEFAULT_CONFIG_DIR = Path.home() / ".config" / "rtorrent-autograb"
DEFAULT_CONFIG_FILE = DEFAULT_CONFIG_DIR / "config.json"
DEFAULT_STATE_FILE = DEFAULT_CONFIG_DIR / "seen.json"

log = logging.getLogger("autograb")


def setup_logging(verbose=False):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s [%(threadName)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

DEFAULT_FILTER = {
    "name": "New filter",
    "enabled": True,
    # OR by default: name must contain at least one of these. Set
    # include_mode to "all" to require every keyword to be present instead
    # (AND) -- e.g. "bike, car" with mode "any" matches a release mentioning
    # either word; with mode "all" it only matches one mentioning both.
    "include_keywords": [],
    "include_mode": "any",    # "any" (OR, default) or "all" (AND)
    "exclude_keywords": [],   # always OR: release is rejected if it contains ANY of these
    "regex": "",              # optional extra regex the name must match
    "quality": [],            # e.g. ["1080p", "2160p"] - OR: name must contain at least one (if any given)
    "codecs": [],             # e.g. ["x265", "x264"] - OR: name must contain at least one (if any given)
    "min_size_mb": 0,         # 0 = no minimum
    "max_size_mb": 0,         # 0 = no maximum
    "download_dir": "",       # override rtorrent's default download dir for matches
}

DEFAULT_IRC_SOURCE = {
    "id": "",
    "enabled": True,
    "name": "New IRC source",
    "server": "",
    "port": 6697,
    "tls": True,
    "tls_verify": True,
    "nick": "autograb_" + secrets.token_hex(3),
    "server_password": "",
    "nickserv_password": "",
    "channels": [],           # ["#announce"]
    "invite_command": "",     # e.g. "PRIVMSG BotServ :INVITE #announce" - sent once after connecting
    # Optional regex with named groups (?P<name>...) and (?P<url>...) applied
    # to each channel message. Leave blank to use the built-in generic
    # extractor (first magnet: link or *.torrent URL in the line, name =
    # rest of the line).
    "line_regex": "",
    "filters": [],
}

DEFAULT_RSS_SOURCE = {
    "id": "",
    "enabled": True,
    "name": "New RSS feed",
    "url": "",
    "poll_interval_sec": 300,
    "filters": [],
}

DEFAULT_CONFIG = {
    "rtorrent": {
        "mode": "unix",             # "unix" or "tcp"
        "socket_path": "",          # e.g. /home/user/.rtorrent.sock (local path, this runs on the server)
        "host": "127.0.0.1",
        "port": 5000,
        "download_dir": "",         # default dir for new torrents; blank = rtorrent's own default
    },
    "user_agent": "rtorrent-autograb/1.0",
    "dedupe_max": 5000,
    "irc_sources": [],
    "rss_sources": [],
}


class ConfigStore:
    """Thread-safe holder for the live config, backed by a JSON file. There
    is no API mutating this in-process: the file is expected to be edited
    externally (by hand, or by the GUI overwriting it over SSH), so every
    read checks the file's mtime and transparently reloads + bumps a
    version counter when it has changed, which is what the watcher threads
    poll to know when to reconnect with new settings."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self._version = 0
        self._mtime = None
        self._cfg = self._load()
        self._record_mtime_locked()

    def _load(self):
        if self.path.exists():
            try:
                with open(self.path, "r") as f:
                    data = json.load(f)
                cfg = copy.deepcopy(DEFAULT_CONFIG)
                _deep_update(cfg, data)
                return _normalize_config(cfg)
            except (json.JSONDecodeError, OSError) as e:
                log.error("Failed to read config %s (%s); using defaults", self.path, e)
        return copy.deepcopy(DEFAULT_CONFIG)

    def _record_mtime_locked(self):
        try:
            self._mtime = self.path.stat().st_mtime
        except OSError:
            self._mtime = None

    def _save_locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(self._cfg, f, indent=2)
        tmp.replace(self.path)
        self._record_mtime_locked()

    def _maybe_reload_locked(self):
        try:
            current_mtime = self.path.stat().st_mtime
        except OSError:
            return
        if self._mtime is None or current_mtime != self._mtime:
            self._cfg = self._load()
            self._mtime = current_mtime
            self._version += 1
            log.info("Config file changed on disk; reloaded.")

    def get(self):
        with self._lock:
            self._maybe_reload_locked()
            return copy.deepcopy(self._cfg)

    def version(self):
        with self._lock:
            self._maybe_reload_locked()
            return self._version

    def replace(self, new_cfg):
        """Validates and writes a whole new config. Used by --init; a
        running daemon picks up file changes on its own (see above), so
        nothing else needs to call this."""
        new_cfg = _normalize_config(new_cfg)
        with self._lock:
            self._cfg = new_cfg
            self._version += 1
            self._save_locked()


    def mutate(self, fn):
        """Runs fn(cfg_dict) to modify the config in place, then persists."""
        with self._lock:
            fn(self._cfg)
            self._cfg = _normalize_config(self._cfg)
            self._version += 1
            self._save_locked()


def _deep_update(base, overrides):
    for k, v in overrides.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_update(base[k], v)
        else:
            base[k] = v


def _normalize_filter(f):
    out = copy.deepcopy(DEFAULT_FILTER)
    _deep_update(out, f if isinstance(f, dict) else {})
    return out


def _normalize_irc_source(s):
    out = copy.deepcopy(DEFAULT_IRC_SOURCE)
    _deep_update(out, s if isinstance(s, dict) else {})
    if not out["id"]:
        out["id"] = uuid.uuid4().hex[:8]
    out["filters"] = [_normalize_filter(f) for f in out.get("filters") or []]
    return out


def _normalize_rss_source(s):
    out = copy.deepcopy(DEFAULT_RSS_SOURCE)
    _deep_update(out, s if isinstance(s, dict) else {})
    if not out["id"]:
        out["id"] = uuid.uuid4().hex[:8]
    out["filters"] = [_normalize_filter(f) for f in out.get("filters") or []]
    return out


def _normalize_config(cfg):
    out = copy.deepcopy(DEFAULT_CONFIG)
    _deep_update(out, cfg if isinstance(cfg, dict) else {})
    out["irc_sources"] = [_normalize_irc_source(s) for s in out.get("irc_sources") or []]
    out["rss_sources"] = [_normalize_rss_source(s) for s in out.get("rss_sources") or []]
    return out


# --------------------------------------------------------------------------
# Grab log (recent matches). Persisted to disk so the GUI's Automation
# dialog can show it by reading the file over SSH -- no API needed.
# --------------------------------------------------------------------------

class GrabLog:
    def __init__(self, path: Path, cap=500):
        self.path = path
        self.cap = cap
        self._lock = threading.RLock()
        self._entries = self._load()

    def _load(self):
        if self.path.exists():
            try:
                with open(self.path, "r") as f:
                    data = json.load(f)
                return list(data.get("entries", []))[-self.cap:]
            except (json.JSONDecodeError, OSError):
                pass
        return []

    def _save_locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump({"entries": self._entries}, f, indent=2)
        tmp.replace(self.path)

    def add(self, source_type, source_name, name, status, detail=""):
        with self._lock:
            self._entries.append({
                "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "source_type": source_type,
                "source_name": source_name,
                "name": name,
                "status": status,   # "added" | "filtered_out" | "error" | "duplicate"
                "detail": detail,
            })
            if len(self._entries) > self.cap:
                self._entries = self._entries[-self.cap:]
            self._save_locked()

    def recent(self, limit=200):
        with self._lock:
            return list(self._entries[-limit:][::-1])


# Set up for real in cmd_run() once we know the log path; Watcher methods
# reference this module-level name via `grab_log.add(...)`.
grab_log = None


# --------------------------------------------------------------------------
# Dedupe store (persisted so RSS items we've already grabbed don't get
# re-added every poll; IRC doesn't strictly need this but shares the store)
# --------------------------------------------------------------------------

class SeenStore:
    def __init__(self, path: Path, cap=5000):
        self.path = path
        self.cap = cap
        self._lock = threading.RLock()
        self._seen = []          # ordered list, oldest first, for eviction
        self._seen_set = set()
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                with open(self.path, "r") as f:
                    data = json.load(f)
                self._seen = list(data.get("seen", []))
                self._seen_set = set(self._seen)
            except (json.JSONDecodeError, OSError):
                pass

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump({"seen": self._seen}, f)
        tmp.replace(self.path)

    def check_and_add(self, key):
        """Returns True if key was NOT seen before (and marks it seen now)."""
        with self._lock:
            if key in self._seen_set:
                return False
            self._seen.append(key)
            self._seen_set.add(key)
            if len(self._seen) > self.cap:
                drop = self._seen[: len(self._seen) - self.cap]
                self._seen = self._seen[len(self._seen) - self.cap:]
                self._seen_set.difference_update(drop)
            self._save()
            return True


# --------------------------------------------------------------------------
# Filter matching
# --------------------------------------------------------------------------

def filter_matches(name: str, size_bytes, filt: dict):
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
        except re.error as e:
            log.warning("Invalid regex in filter %r: %s", filt.get("name"), e)
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


def first_matching_filter(name, size_bytes, filters):
    for f in filters:
        if filter_matches(name, size_bytes, f):
            return f
    return None


# --------------------------------------------------------------------------
# rtorrent XML-RPC/SCGI client (local -- this daemon runs on the same box)
# --------------------------------------------------------------------------

class RPCError(Exception):
    pass


def _scgi_wrap(body: bytes) -> bytes:
    headers = [("CONTENT_LENGTH", str(len(body))), ("SCGI", "1")]
    header_str = "".join(f"{k}\0{v}\0" for k, v in headers)
    return f"{len(header_str)}:{header_str}".encode("utf-8") + b"," + body


class RTorrentRPC:
    def __init__(self, mode, socket_path=None, host=None, port=None, timeout=10):
        self.mode = mode
        self.socket_path = socket_path
        self.host = host
        self.port = port
        self.timeout = timeout

    def _connect(self):
        if self.mode == "unix":
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(self.timeout)
            s.connect(self.socket_path)
        else:
            s = socket.create_connection((self.host, self.port), timeout=self.timeout)
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

    def ping(self):
        self.call("system.client_version")


def make_rpc(rt_cfg):
    if rt_cfg["mode"] == "unix":
        return RTorrentRPC("unix", socket_path=rt_cfg["socket_path"])
    return RTorrentRPC("tcp", host=rt_cfg["host"], port=rt_cfg["port"])


def add_to_rtorrent(rpc: RTorrentRPC, uri: str, download_dir: str, user_agent: str):
    """Adds a magnet URI directly, or downloads a .torrent file over HTTP(S)
    first and injects its raw bytes. Returns nothing; raises on failure."""
    extra_cmds = []
    if download_dir:
        safe_dir = download_dir.replace('"', '\\"')
        extra_cmds.append(f'd.directory.set="{safe_dir}"')

    if uri.startswith("magnet:"):
        rpc.call("load.start", "", uri, *extra_cmds)
        return

    req = Request(uri, headers={"User-Agent": user_agent})
    with urlopen(req, timeout=30) as resp:
        data = resp.read()
    rpc.call("load.raw_start", "", xmlrpclib.Binary(data), *extra_cmds)


# --------------------------------------------------------------------------
# RSS / Atom parsing (stdlib xml.etree only, no feedparser dependency)
# --------------------------------------------------------------------------

ATOM_NS = "{http://www.w3.org/2005/Atom}"


def parse_feed(raw_bytes):
    """Returns a list of dicts: {guid, title, url, size_bytes}. Handles
    basic RSS 2.0 <item> and Atom <entry> shapes; good enough for the vast
    majority of tracker/announce feeds without pulling in a dependency."""
    root = ET.fromstring(raw_bytes)
    items = []

    # RSS 2.0: <rss><channel><item>...
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        guid = (item.findtext("guid") or link or title).strip()
        size_bytes = None
        enclosure = item.find("enclosure")
        if enclosure is not None:
            url_attr = enclosure.get("url")
            if url_attr:
                link = url_attr.strip()
            length = enclosure.get("length")
            if length and length.isdigit():
                size_bytes = int(length)
        if not link:
            continue
        items.append({"guid": guid, "title": title, "url": link, "size_bytes": size_bytes})

    # Atom: <feed><entry>...
    for entry in root.findall(f".//{ATOM_NS}entry"):
        title = (entry.findtext(f"{ATOM_NS}title") or "").strip()
        guid = (entry.findtext(f"{ATOM_NS}id") or title).strip()
        link = ""
        for link_el in entry.findall(f"{ATOM_NS}link"):
            rel = link_el.get("rel", "alternate")
            if rel in ("alternate", "enclosure") and link_el.get("href"):
                link = link_el.get("href").strip()
                if rel == "enclosure":
                    break
        if not link:
            continue
        items.append({"guid": guid, "title": title, "url": link, "size_bytes": None})

    return items


# --------------------------------------------------------------------------
# Generic announcement-line extractor for IRC (used when a source doesn't
# provide its own line_regex)
# --------------------------------------------------------------------------

_URL_RE = re.compile(r"(magnet:\?[^\s\x02\x03]+|https?://[^\s\x02\x03]+\.torrent(?:\?[^\s\x02\x03]*)?)")
_IRC_COLOR_RE = re.compile(r"\x03(?:\d{1,2}(?:,\d{1,2})?)?|[\x02\x0f\x16\x1d\x1f]")


def strip_irc_formatting(text):
    return _IRC_COLOR_RE.sub("", text)


def extract_release(line, line_regex=""):
    """Returns (name, url) or None. `line` is a single already-decoded IRC
    message (mIRC color/bold codes stripped)."""
    line = strip_irc_formatting(line).strip()
    if line_regex:
        try:
            m = re.search(line_regex, line)
        except re.error as e:
            log.warning("Invalid line_regex %r: %s", line_regex, e)
            m = None
        if m:
            gd = m.groupdict()
            url = gd.get("url")
            name = gd.get("name")
            if url:
                return (name or line).strip(), url.strip()
        # fall through to generic extraction if the custom regex didn't match

    m = _URL_RE.search(line)
    if not m:
        return None
    url = m.group(0)
    name = (line[: m.start()] + line[m.end():]).strip(" -:|\t")
    if not name:
        name = line
    return name, url


# --------------------------------------------------------------------------
# Watcher base
# --------------------------------------------------------------------------

class Watcher(threading.Thread):
    """Common shell for IRC/RSS watcher threads: cooperative stop flag +
    process-a-match helper that applies filters, dedupes, and hands off to
    rtorrent, logging the outcome either way."""

    def __init__(self, source, source_type, cfg_store: ConfigStore, seen: SeenStore, name=None):
        super().__init__(daemon=True, name=name or f"{source_type}:{source.get('name', '?')}")
        self.source = source
        self.source_type = source_type  # "irc" or "rss"
        self.cfg_store = cfg_store
        self.seen = seen
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def stopped(self):
        return self._stop_event.is_set()

    def handle_candidate(self, dedupe_key, name, url, size_bytes):
        rt_cfg = self.cfg_store.get()["rtorrent"]
        user_agent = self.cfg_store.get()["user_agent"]
        source_name = self.source.get("name", self.source.get("id", "?"))

        if dedupe_key is not None and not self.seen.check_and_add(dedupe_key):
            grab_log.add(self.source_type, source_name, name, "duplicate")
            return

        filt = first_matching_filter(name, size_bytes, self.source.get("filters") or [])
        if filt is None:
            grab_log.add(self.source_type, source_name, name, "filtered_out")
            return

        download_dir = filt.get("download_dir") or rt_cfg.get("download_dir") or ""
        try:
            rpc = make_rpc(rt_cfg)
            add_to_rtorrent(rpc, url, download_dir, user_agent)
            grab_log.add(self.source_type, source_name, name, "added", detail=f"matched filter '{filt.get('name')}'")
            log.info("[%s] Added: %s (filter: %s)", source_name, name, filt.get("name"))
        except Exception as e:
            grab_log.add(self.source_type, source_name, name, "error", detail=str(e))
            log.error("[%s] Failed to add '%s': %s", source_name, name, e)


# --------------------------------------------------------------------------
# IRC watcher
# --------------------------------------------------------------------------

class IrcWatcher(Watcher):
    RECONNECT_BACKOFF = [5, 15, 30, 60, 120, 300]

    def __init__(self, source, cfg_store, seen):
        super().__init__(source, "irc", cfg_store, seen)
        self._sock = None
        self._buf = b""

    def run(self):
        attempt = 0
        while not self.stopped():
            try:
                self._connect_and_loop()
                attempt = 0  # clean exit (stop requested) -- don't backoff
            except Exception as e:
                if self.stopped():
                    break
                delay = self.RECONNECT_BACKOFF[min(attempt, len(self.RECONNECT_BACKOFF) - 1)]
                log.warning("[irc:%s] connection error (%s); reconnecting in %ss",
                            self.source.get("name"), e, delay)
                attempt += 1
                self._stop_event.wait(delay)
        self._close()

    def _close(self):
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def _send(self, line):
        self._sock.sendall((line + "\r\n").encode("utf-8", errors="replace"))

    def _connect_and_loop(self):
        src = self.source
        raw = socket.create_connection((src["server"], src["port"]), timeout=30)
        raw.settimeout(300)  # server should PING at least this often
        if src.get("tls", True):
            ctx = ssl.create_default_context()
            if not src.get("tls_verify", True):
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            self._sock = ctx.wrap_socket(raw, server_hostname=src["server"])
        else:
            self._sock = raw

        log.info("[irc:%s] connected to %s:%s", src.get("name"), src["server"], src["port"])
        if src.get("server_password"):
            self._send(f"PASS {src['server_password']}")
        nick = src.get("nick") or "autograb"
        self._send(f"NICK {nick}")
        self._send(f"USER {nick} 0 * :{nick}")

        joined = False
        self._buf = b""
        while not self.stopped():
            try:
                chunk = self._sock.recv(65536)
            except socket.timeout:
                raise ConnectionError("read timed out (no PING from server)")
            if not chunk:
                raise ConnectionError("connection closed by server")
            self._buf += chunk
            while b"\r\n" in self._buf:
                line, self._buf = self._buf.split(b"\r\n", 1)
                self._handle_line(line.decode("utf-8", errors="replace"), src)
                if not joined and self._welcomed:
                    self._after_welcome(src)
                    joined = True

    _welcomed = False

    def _after_welcome(self, src):
        if src.get("nickserv_password"):
            self._send(f"PRIVMSG NickServ :IDENTIFY {src['nickserv_password']}")
            time.sleep(2)
        if src.get("invite_command"):
            self._send(src["invite_command"])
            time.sleep(1)
        for chan in src.get("channels") or []:
            self._send(f"JOIN {chan}")

    def _handle_line(self, line, src):
        if not line:
            return
        if line.startswith("PING"):
            self._send("PONG" + line[4:])
            return

        # 001 = RPL_WELCOME
        parts = line.split(" ", 3)
        if len(parts) >= 2 and parts[1] == "001":
            self._welcomed = True
            return

        # :nick!user@host PRIVMSG #channel :message text
        if " PRIVMSG " not in line:
            return
        try:
            prefix, rest = line.split(" PRIVMSG ", 1)
            target, _, text = rest.partition(" :")
        except ValueError:
            return
        if not text or not target.startswith("#"):
            return

        result = extract_release(text, src.get("line_regex", ""))
        if not result:
            return
        name, url = result
        dedupe_key = "irc:" + hashlib.sha1((src["id"] + "|" + url).encode("utf-8", "replace")).hexdigest()
        self.handle_candidate(dedupe_key, name, url, size_bytes=None)


# --------------------------------------------------------------------------
# RSS watcher
# --------------------------------------------------------------------------

class RssWatcher(Watcher):
    def __init__(self, source, cfg_store, seen):
        super().__init__(source, "rss", cfg_store, seen)

    def run(self):
        src = self.source
        interval = max(30, int(src.get("poll_interval_sec", 300)))
        # small stagger so a config reload doesn't hammer every feed at once
        self._stop_event.wait(min(5, interval))
        while not self.stopped():
            try:
                self._poll_once()
            except Exception as e:
                log.error("[rss:%s] poll failed: %s", src.get("name"), e)
                grab_log.add("rss", src.get("name", src.get("id")), "(feed fetch)", "error", detail=str(e))
            self._stop_event.wait(interval)

    def _poll_once(self):
        src = self.source
        user_agent = self.cfg_store.get()["user_agent"]
        req = Request(src["url"], headers={"User-Agent": user_agent})
        with urlopen(req, timeout=30) as resp:
            raw = resp.read()
        items = parse_feed(raw)
        for item in items:
            dedupe_key = "rss:" + hashlib.sha1((src["id"] + "|" + item["guid"]).encode("utf-8", "replace")).hexdigest()
            self.handle_candidate(dedupe_key, item["title"] or item["url"], item["url"], item["size_bytes"])


# --------------------------------------------------------------------------
# Source manager: (re)spawns watcher threads whenever config changes
# --------------------------------------------------------------------------

class SourceManager(threading.Thread):
    def __init__(self, cfg_store: ConfigStore, seen: SeenStore):
        super().__init__(daemon=True, name="source-manager")
        self.cfg_store = cfg_store
        self.seen = seen
        self._threads = {}   # source id -> Watcher
        self._last_version = -1
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def run(self):
        while not self._stop_event.is_set():
            version = self.cfg_store.version()
            if version != self._last_version:
                self._reconcile()
                self._last_version = version
            self._stop_event.wait(2)
        for w in self._threads.values():
            w.stop()

    def _reconcile(self):
        cfg = self.cfg_store.get()
        wanted = {}
        for s in cfg["irc_sources"]:
            if s.get("enabled", True) and s.get("server") and s.get("channels"):
                wanted[("irc", s["id"])] = s
        for s in cfg["rss_sources"]:
            if s.get("enabled", True) and s.get("url"):
                wanted[("rss", s["id"])] = s

        # Stop/replace anything removed, disabled, or whose settings changed.
        for key in list(self._threads.keys()):
            watcher = self._threads[key]
            new_source = wanted.get(key)
            if new_source is None or new_source != watcher.source:
                log.info("Stopping %s watcher %s", key[0], key[1])
                watcher.stop()
                del self._threads[key]

        # Start anything new.
        for key, source in wanted.items():
            if key in self._threads:
                continue
            kind, _id = key
            log.info("Starting %s watcher: %s", kind, source.get("name"))
            cls = IrcWatcher if kind == "irc" else RssWatcher
            w = cls(source, self.cfg_store, self.seen)
            w.start()
            self._threads[key] = w

    def status(self):
        return {
            f"{kind}:{sid}": {"alive": w.is_alive(), "name": w.source.get("name")}
            for (kind, sid), w in self._threads.items()
        }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def cmd_init(args):
    cfg_store = ConfigStore(args.config)
    cfg_store.replace(cfg_store.get())  # normalize + write the file if it didn't exist yet
    log_path = args.log_file or (args.config.parent / "activity.json")
    print(f"Config written to:      {args.config}")
    print(f"Activity log will be at: {log_path}")
    print()
    print("Paste these paths into the GUI's Settings -> Automation section")
    print("(the GUI reaches them over the same SSH connection it already")
    print("uses for rtorrent -- no port to open, no webserver here):")
    print(f"  Config path: {args.config}")
    print(f"  Log path:    {log_path}")
    print()
    print("Next, set rtorrent.socket_path (or host/port) in the config, or")
    print("via the GUI's Automation dialog, then run: python3 rtorrent_autograb.py")


def cmd_run(args):
    setup_logging(args.verbose)
    global grab_log
    log_path = args.log_file or (args.config.parent / "activity.json")
    grab_log = GrabLog(log_path, cap=500)

    cfg_store = ConfigStore(args.config)
    seen = SeenStore(args.state, cap=cfg_store.get().get("dedupe_max", 5000))

    rt_cfg = cfg_store.get()["rtorrent"]
    try:
        make_rpc(rt_cfg).ping()
        log.info("rtorrent RPC reachable.")
    except Exception as e:
        log.warning("Could not reach rtorrent RPC yet (%s). Will keep trying "
                     "when a torrent needs to be added; fix rtorrent.socket_path "
                     "/ host+port in the config if this persists.", e)

    manager = SourceManager(cfg_store, seen)
    manager.start()

    log.info("Watching %s for changes (edit it by hand, or via the GUI's "
             "Automation dialog over SSH); no network listener is opened by "
             "this process.", cfg_store.path)
    try:
        # No webserver, so nothing to serve_forever() -- just idle the main
        # thread while the manager/watcher threads do their work, and wake
        # up periodically so Ctrl-C / signals are handled promptly.
        while True:
            time.sleep(2)
    except KeyboardInterrupt:
        log.info("Shutting down...")
    finally:
        manager.stop()



def build_arg_parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG_FILE, help="Path to config.json")
    p.add_argument("--state", type=Path, default=DEFAULT_STATE_FILE, help="Path to the dedupe state file")
    p.add_argument("--log-file", type=Path, default=None,
                    help="Path to the activity/grab log JSON (default: activity.json next to --config)")
    p.add_argument("--verbose", action="store_true", help="Debug logging")
    p.add_argument("--init", action="store_true", help="Write a fresh config file, print its path, then exit")
    return p


def main():
    args = build_arg_parser().parse_args()
    if args.init:
        setup_logging(args.verbose)
        cmd_init(args)
    else:
        cmd_run(args)


if __name__ == "__main__":
    main()
