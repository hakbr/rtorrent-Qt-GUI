# rtorrent-qt-gui

A lightweight PyQt5 GUI for monitoring and controlling an [rtorrent](https://github.com/rakshasa/rtorrent) instance running inside `screen`/`tmux` on a remote server, reached over your existing passwordless SSH key auth.


## Why this exists

If you run rtorrent headless inside a `screen` session on a VPS or home server, you normally have two options: SSH in and stare at the ncurses UI, or set up a full web front end like ruTorrent. This tool is a middle ground — a native desktop app that talks directly to rtorrent's XML-RPC interface over SCGI, tunneled through SSH. It does **not** scrape or parse the ncurses screen — it uses the same RPC mechanism ruTorrent and pyrocore use, so it's fast and reliable.

## Features

- Connects over an SSH tunnel using your existing key-based auth (no passwords, no extra open ports)
- Live torrent table: name, status, progress, download/upload speed, ETA, peers, ratio, size, totals
- Start / stop torrents via right-click context menu
- Auto-refreshing view with a configurable poll interval
- Settings dialog for SSH and RPC connection details, saved to a local config file
- Non-blocking UI — polling and actions run on background threads

## Requirements

- Linux with a desktop environment (tested on Debian/Ubuntu)
- Python 3
- `PyQt5`
- `openssh-client` (the `ssh` binary must be on your `PATH`)
- Passwordless SSH key auth already set up to your rtorrent server
- rtorrent configured with an RPC socket (see [Server-side setup](#server-side-setup) below)

### Install dependencies (Debian/Ubuntu)

```bash
sudo apt install python3-pyqt5 openssh-client
```

## Usage

```bash
python3 rtorrent_gui.py
```

On first launch, click **Settings...** and fill in:

| Field | Description |
|---|---|
| RPC transport | `Unix socket` (recommended) or `TCP port` |
| Host / IP | Your rtorrent server's SSH host |
| SSH port | Defaults to `22` |
| Username | SSH username |
| Remote socket path | Path to rtorrent's RPC socket on the server, e.g. `/home/youruser/.rtorrent.sock` (unix mode) |
| Remote bind host / port | Used only in TCP mode |
| Refresh every | Polling interval in seconds |

Click **Connect** to open the SSH tunnel and start polling. Right-click one or more torrents in the table to start or stop them.

Settings are saved to `~/.config/rtorrent-qt-gui/config.json`.

## Server-side setup

This tool needs rtorrent's XML-RPC interface enabled and bound to a local unix socket (recommended) or TCP port. Add the following to your `~/.rtorrent.rc` on the **server**, then restart rtorrent:

```
# Unix socket (recommended — no extra listening port)
network.scgi.open_local = /home/youruser/.rtorrent.sock
```

or, for TCP:

```
network.scgi.open_port = 127.0.0.1:5000
```

> **Note:** Only bind SCGI to `127.0.0.1` or a unix socket — never expose it directly on a public interface. This tool reaches it securely through the SSH tunnel.

## How it works

1. On connect, the app spawns `ssh -N -L <local>:<remote> user@host`, forwarding a local unix socket (or local TCP port) to rtorrent's RPC socket on the server.
2. It waits for the local end of the tunnel to accept connections.
3. It then speaks XML-RPC-over-SCGI directly to that local socket/port to fetch torrent stats (`d.multicall2`) and issue commands (`d.start`, `d.stop`, etc.), polling on a background thread so the UI never blocks.

## Troubleshooting

**"Could not establish the SSH tunnel"**
- Confirm `ssh user@host` works with no password prompt from a terminal.
- Confirm rtorrent is running and the RPC socket/port in `.rtorrent.rc` matches what you entered in Settings.
- Try running the exact `ssh` command shown in the error dialog by hand to see the real error output.

**Connected but no torrents show up**
- Check that rtorrent's `main` view isn't empty and that the socket path is correct.
- Older rtorrent versions without `d.multicall2` are supported via an automatic fallback to `d.multicall`.

## License

GPL
