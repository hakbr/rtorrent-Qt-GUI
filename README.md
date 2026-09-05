# rtorrent-remote

A desktop GUI for rtorrent over SSH, plus an optional server-side daemon that
auto-adds torrents from IRC announce channels and RSS feeds.

Two files, no build step, no runtime dependencies beyond PyQt6 on the desktop
side. The server-side piece is pure standard-library Python and **opens no
network listener of its own** — everything the GUI needs from it is read and
written as plain files over the SSH connection you already use for rtorrent.

| File | Runs on | Purpose |
|---|---|---|
| `rtorrent_gui.py` | Your desktop | PyQt6 GUI: connects to rtorrent over SSH, lists/manages torrents, configures automation |
| `rtorrent_autograb.py` | Your seedbox / server | Watches IRC + RSS, filters releases, adds matches to rtorrent |

---

## Contents

- [Architecture](#architecture)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [1. Server: rtorrent RPC](#1-server-rtorrent-rpc)
- [2. Server: rtorrent_autograb.py](#2-server-rtorrent_autogrampy)
- [3. Desktop: rtorrent_gui.py](#3-desktop-rtorrent_guipy)
- [4. Configure automation from the GUI](#4-configure-automation-from-the-gui)
- [Filter reference](#filter-reference)
- [Config file reference](#config-file-reference)
- [Security notes](#security-notes)
- [Troubleshooting](#troubleshooting)

---

## Architecture

```
┌─────────────────────────┐         SSH (one connection,          ┌───────────────────────────┐
│      Desktop / laptop    │         reused for everything)        │        Server / seedbox    │
│                          │ ─────────────────────────────────────▶│                            │
│  rtorrent_gui.py         │  1. forwards rtorrent's unix socket   │  rtorrent (SCGI on a       │
│  (PyQt6)                 │     locally, talks XML-RPC over it    │  local unix socket)        │
│                          │                                        │                            │
│                          │  2. `ssh ... cat <path>`   (read)      │  rtorrent_autograb.py      │
│                          │     `ssh ... cat > <path>` (write)     │  - config.json             │
│                          │     to read/edit automation config     │  - activity.json           │
│                          │     and activity log — no API, no      │  - watches IRC channels    │
│                          │     extra port                         │  - polls RSS feeds         │
└─────────────────────────┘                                        └───────────────────────────┘
```

`rtorrent_autograb.py` never binds a socket to listen on. It:

1. Makes **outbound** connections only (IRC servers, RSS feed URLs, and
   rtorrent's local RPC socket).
2. Watches its own config file's mtime and hot-reloads within a couple of
   seconds whenever the file changes — regardless of what changed it.

That second point is what lets the GUI "configure it remotely" without a
control API: the GUI just overwrites the config file over SSH, same as it
would `scp` any other file.

---

## Requirements

**Server** (wherever rtorrent runs):
- Python 3.8+ (standard library only — nothing to `pip install`)
- rtorrent, configured with SCGI enabled (see below)

**Desktop** (wherever you run the GUI):
- Python 3.9+
- [PyQt6](https://pypi.org/project/PyQt6/)
- An OpenSSH client (`ssh`) on your `PATH`
- `sshpass`, only if you want password auth instead of SSH keys

```bash
pip install PyQt6
# Debian/Ubuntu equivalents:
sudo apt install python3-pyqt6 openssh-client sshpass
```

---

## Quick start

```bash
# 1. On the server
scp rtorrent_autograb.py youruser@yourserver:~/
ssh youruser@yourserver
python3 rtorrent_autograb.py --init      # writes config, prints paths
# edit ~/.config/rtorrent-autograb/config.json -> rtorrent.socket_path
python3 rtorrent_autograb.py &           # or set up the systemd unit below

# 2. On your desktop
python3 rtorrent_gui.py
# Settings... -> fill in SSH + rtorrent socket + the two paths --init printed
# Connect
# Automation... -> add IRC/RSS sources and filters
```

The detailed version of each step follows.

---

## 1. Server: rtorrent RPC

rtorrent needs SCGI enabled so both the GUI and the daemon can talk to it. In
`~/.rtorrent.rc`:

```
network.scgi.open_local = /home/youruser/.rtorrent.sock
```

A local unix socket is strongly preferred over `network.scgi.open_port`
(TCP) — it can't be reached from anywhere but the same machine, so there's
nothing to firewall. Restart rtorrent (in your existing `screen`/`tmux`
session) after adding this line.

---

## 2. Server: rtorrent_autograb.py

Copy the single file over and initialize it:

```bash
scp rtorrent_autograb.py youruser@yourserver:~/
ssh youruser@yourserver
python3 rtorrent_autograb.py --init
```

This prints something like:

```
Config written to:      /home/youruser/.config/rtorrent-autograb/config.json
Activity log will be at: /home/youruser/.config/rtorrent-autograb/activity.json

Paste these paths into the GUI's Settings -> Automation section:
  Config path: /home/youruser/.config/rtorrent-autograb/config.json
  Log path:    /home/youruser/.config/rtorrent-autograb/activity.json
```

Keep those two paths — you'll need them in step 3.

Point it at rtorrent's socket (or leave this for the GUI's Automation dialog
to set later — it's the same file either way):

```bash
nano ~/.config/rtorrent-autograb/config.json
```

```json
"rtorrent": {
  "mode": "unix",
  "socket_path": "/home/youruser/.rtorrent.sock"
}
```

### Running it

Foreground, for testing:

```bash
python3 rtorrent_autograb.py --verbose
```

Detached, quick and dirty:

```bash
screen -dmS autograb python3 rtorrent_autograb.py
```

As a systemd user service (recommended for anything long-running):

```ini
# ~/.config/systemd/user/rtorrent-autograb.service
[Unit]
Description=rtorrent autograb (IRC/RSS -> rtorrent)
After=network-online.target

[Service]
ExecStart=/usr/bin/python3 /home/youruser/rtorrent_autograb.py
Restart=on-failure

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now rtorrent-autograb
systemctl --user status rtorrent-autograb
journalctl --user -u rtorrent-autograb -f   # watch it work
```

### CLI reference

```
python3 rtorrent_autograb.py [--config PATH] [--state PATH] [--log-file PATH] [--verbose] [--init]
```

| Flag | Default | Purpose |
|---|---|---|
| `--config` | `~/.config/rtorrent-autograb/config.json` | Main config file |
| `--state` | `~/.config/rtorrent-autograb/seen.json` | Dedupe store (which RSS items were already grabbed) |
| `--log-file` | `<config dir>/activity.json` | Recent-grabs activity log the GUI reads |
| `--verbose` | off | Debug-level logging |
| `--init` | — | Write/normalize the config file, print paths, exit |

---

## 3. Desktop: rtorrent_gui.py

```bash
python3 rtorrent_gui.py
```

Open **Settings...** and fill in:

| Field | Example | Notes |
|---|---|---|
| RPC transport | Unix socket | Matches how you set up rtorrent in step 1 |
| Host / IP | `yourserver` | |
| SSH port | `22` | |
| Username | `youruser` | |
| Authentication | SSH key / Password | Key is recommended; password auth needs `sshpass` installed locally |
| Remote socket path | `/home/youruser/.rtorrent.sock` | From step 1 |
| Remote config path | `/home/youruser/.config/rtorrent-autograb/config.json` | From step 2's `--init` output |
| Remote activity log path | `/home/youruser/.config/rtorrent-autograb/activity.json` | From step 2's `--init` output |

`~` in these paths is expanded correctly on the remote end (see
[Troubleshooting](#troubleshooting) if you're on an older copy of this file).

Click **Connect**. This opens the SSH tunnel to rtorrent's socket and starts
polling. The torrent table is built to stay smooth into the thousands of
torrents — it diffs each poll against what's already shown instead of
rebuilding the whole list.

---

## 4. Configure automation from the GUI

Click **Automation...**. It loads the config file over SSH into three tabs:

- **IRC Sources** — server/port/TLS, NickServ auth, channels to join, and a
  list of filters. Leave "Line regex" blank to use the built-in extractor
  (first magnet link or `.torrent` URL in the announcement line); set it if
  your tracker's announce bot needs a custom pattern with named groups
  `(?P<name>...)` / `(?P<url>...)`.
- **RSS Feeds** — feed URL and poll interval, same filter list.
- **Activity Log** — recent grabs (added / filtered out / duplicate / error),
  read straight from the server's activity log file.

Click **Save to server** to write changes back over SSH. The daemon notices
the file changed within a couple of seconds and restarts only the watchers
whose settings actually changed — no full restart needed.

---

## Filter reference

Each source (IRC or RSS) has a list of filters; a release is grabbed if it
matches **any one** of that source's filters.

| Field | Semantics |
|---|---|
| **Include keywords** | Comma-separated. Controlled by **Match mode**: `Any` (OR, default) means the release name needs just one of the words; `All` (AND) means it needs every one. E.g. `bike, car` in OR mode matches a release with either word; in AND mode it only matches one containing both. Leave blank to skip this check. |
| **Exclude keywords** | Always OR: the release is rejected if its name contains *any* of these. |
| **Regex** | Optional, applied in addition to the above (Python `re`, case-insensitive). |
| **Quality tags** | e.g. `1080p, 2160p`. Always OR — treated as acceptable alternatives, since a release is normally tagged with only one resolution anyway. |
| **Codec tags** | e.g. `x265, x264`. Same OR semantics as quality tags. |
| **Min / max size** | In MB. `0` means no bound. IRC announcements rarely carry a size up front, so size filters mostly apply to RSS items whose feed includes an `<enclosure length="...">`. |
| **Download dir override** | If set, overrides both the filter-less default and the source's own default for anything this filter matches. |

Use the **Test** box in the filter editor to check a sample release name
against the current filter settings instantly — it runs locally, no
round-trip to the server.

---

## Config file reference

`rtorrent_autograb.py`'s config (`config.json`) — this is the file the GUI's
Automation dialog reads and writes:

```jsonc
{
  "rtorrent": {
    "mode": "unix",              // "unix" or "tcp"
    "socket_path": "",           // local path on the SERVER, e.g. /home/user/.rtorrent.sock
    "host": "127.0.0.1",         // used when mode == "tcp"
    "port": 5000,
    "download_dir": ""           // default dir for new torrents; blank = rtorrent's own default
  },
  "user_agent": "rtorrent-autograb/1.0",
  "dedupe_max": 5000,             // how many RSS/IRC dedupe keys to remember
  "irc_sources": [
    {
      "id": "a1b2c3d4",
      "enabled": true,
      "name": "My Tracker Announce",
      "server": "irc.example.net",
      "port": 6697,
      "tls": true,
      "tls_verify": true,
      "nick": "autograb_ab12cd",
      "server_password": "",
      "nickserv_password": "",
      "channels": ["#announce"],
      "invite_command": "",
      "line_regex": "",
      "filters": [ /* see below */ ]
    }
  ],
  "rss_sources": [
    {
      "id": "e5f6a7b8",
      "enabled": true,
      "name": "My RSS Feed",
      "url": "https://example.com/rss?key=...",
      "poll_interval_sec": 300,
      "filters": [ /* see below */ ]
    }
  ]
}
```

Each filter object:

```jsonc
{
  "name": "1080p x265",
  "enabled": true,
  "include_keywords": ["bike", "car"],
  "include_mode": "any",       // "any" (OR) or "all" (AND)
  "exclude_keywords": ["cam", "telesync"],
  "regex": "",
  "quality": ["1080p", "2160p"],
  "codecs": ["x265", "x264"],
  "min_size_mb": 0,
  "max_size_mb": 0,
  "download_dir": ""
}
```

You're free to hand-edit this file directly on the server (with the daemon
running or not — it picks up changes on its own) instead of going through the
GUI.

The GUI's own local config (`~/.config/<app>/config.json` on the desktop,
per-user) stores your SSH connection details and the two remote paths above
— nothing from the daemon's config is duplicated there except those paths.

---

## Security notes

- **No listening socket on the server.** `rtorrent_autograb.py` makes only
  outbound connections (IRC, RSS/HTTP, and rtorrent's local RPC socket).
  There's nothing to firewall and no API surface to secure.
- All GUI ↔ server communication — rtorrent RPC, config read/write, and
  activity log reads — goes over the one SSH connection you already
  configured. Anyone who can already SSH into the box can do everything the
  GUI does; there's no separate credential to leak.
- Config writes are atomic: the GUI writes to `<path>.tmp` then `mv`s it into
  place, so a reader (or the daemon's file-watcher) never sees a
  half-written file.
- IRC server/NickServ passwords and rtorrent connection details are stored
  in plain JSON on both ends. Set file permissions accordingly
  (`chmod 600 ~/.config/rtorrent-autograb/config.json`) if the machine is
  shared.

---

## Troubleshooting

**"Config file not found" even though it exists on the server.**
Make sure you're on the current version of `rtorrent_gui.py`. An earlier
revision quoted remote paths in a way that broke `~` expansion over SSH
(`cat -- '~/.config/...'` looks for a file literally named `~/...` instead of
expanding to your home directory). The current version keeps a leading `~`
unquoted so the remote shell expands it correctly. If you'd rather not rely
on that, an absolute path (`/home/youruser/.config/...`) always works.

**GUI says the automation server isn't configured.**
Settings needs both **Remote config path** and **Remote activity log path**
filled in (the two paths `--init` printed on the server) — there's no API
URL/token in the current version; older revisions of this project had an
HTTP API and those fields, which no longer exist.

**Filter isn't matching what I expect.**
Check the **Match mode** on Include keywords — it defaults to "Any" (OR).
Multiple words there don't mean "must contain all of them" unless you switch
it to "All" (AND). Use the filter editor's **Test** box to check a sample
name before saving.

**IRC source connects but never grabs anything.**
Set `--verbose` on the server and watch the log — most commonly either the
channel's announce format doesn't match the built-in generic extractor (set
a custom `line_regex` with `(?P<name>...)` / `(?P<url>...)` groups) or the
filters are too narrow (temporarily add a filter with everything blank to
confirm releases are being seen at all, then narrow it down).

**RSS feed re-adds the same item repeatedly.**
Shouldn't happen — grabbed items are recorded in `--state` (default
`seen.json`) and skipped on future polls. If it's happening, check that the
process has write access to that file's directory.
