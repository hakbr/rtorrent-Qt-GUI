
**rtorrent-qt-gui**

*// debian · kde · screen · ssh*

# $ rtorrent-qt-gui_

A native Qt window onto the rtorrent running headless in your `screen` session. **No web UI, no reverse proxy, no ncurses squinting** — it speaks XML-RPC straight over the SSH key you already trust.

`[ View on GitHub ]` `git clone https://github.com/hakbt/rtorrent-qt-gui.git`


Legend: ● seeding ● downloading ● checking ● paused ● error

---

**$ why-not-a-web-ui**

## It talks to rtorrent, not through it.

rtorrent already exposes everything a GUI needs over XML-RPC — ruTorrent and pyrocore use the same interface. This app skips the middleman: no daemon to expose, no browser tab pinned forever, no separate login. It opens an SSH tunnel to a Unix socket that only your own account can reach, the same way you'd already reach the box to check on `screen -r`.

- **no attack surface added** — The RPC socket is never exposed on the network — only reachable by whoever can already open a shell on the box.
- **no passwords stored** — Auth is whatever your SSH key already does. The app's config holds a host, a user, and a path — nothing else.
- **real structured data** — Sorting by ratio or ETA actually works, because it's reading typed fields, not parsing a terminal frame.
- **rtorrent keeps running** — Closing the GUI doesn't touch your `screen` session. It's a window, not a replacement.

---

**$ how-it-works**

## One tunnel, one socket, one protocol.

On connect, the app forwards a local Unix socket to rtorrent's RPC socket over SSH, then speaks SCGI-wrapped XML-RPC through it — polling every few seconds for the fields behind each column above.

```
┌───────────────┐     ssh -L socket:socket      ┌────────────────┐    SCGI / XML-RPC    ┌─────────────────┐
│   Qt GUI      │ ─────────────────────────────▶│  local socket  │──────────────────────▶│  rtorrent        │
│  (your desk)  │      (your existing key)       │  (forwarded)   │     d.multicall2       │  inside screen   │
└───────────────┘                                └────────────────┘                       └─────────────────┘
```

Nothing rtorrent-side changes about how you run it. It keeps living in `screen`, restart-proof, exactly as before — this just adds a second way to look at it.

reads → Name · Status · Progress · Down speed · Up speed · ETA · Peers · Ratio · Size · Downloaded · Uploaded
controls → Start / Stop

---

**$ setup**

## Two steps. No new daemon.

**1. On the server — enable rtorrent's RPC socket**
Add one line to `~/.rtorrent.rc`, then restart rtorrent inside your `screen` session.
```
# ~/.rtorrent.rc
network.scgi.open_local = /home/YOURUSER/.rtorrent.sock
```

**2. On your desktop — install and run**
```
$ sudo apt install python3-pyqt5 openssh-client
$ python3 rtorrent_gui.py
```

Open **Settings**, point it at your host and socket path, hit **Connect**. Full walkthrough — including the TCP-port alternative — is in the repo's README.

---

rtorrent-qt-gui · GPL License — github.com/hakbr/rtorrent-qt-gui
