# VyOS WAN Emulator

A sleek single-page web app to control a VyOS router's QoS **network-emulator**
policies, interface IP addresses, interface counters, static routes, and to
watch the live ARP table. Light and dark mode included.

![stack](https://img.shields.io/badge/stack-Flask%20%2B%20netmiko%20%2B%20vanilla%20JS-blue)

## Features

- **Router connection bar** — enter the VyOS IP, username and password in the
  page; credentials are cached in `router_cache.json` (chmod 600) and prefilled
  on the next visit. Green/red status dot shows connection state.
- **Management interface** — `eth0` is treated as the management interface:
  its card only allows IP configuration (no network emulation), and the
  backend rejects netem requests for it.
- **Interface cards** (one per ethernet interface discovered on the router) —
  name, MAC, link state, IP address with **Edit IP**, and the WAN-emulation
  parameters: Bandwidth (+unit), Delay (ms), Corruption %, Reordering %,
  Duplication %, Packet Loss % (sliders *and* text fields), Queue Limit.
  **Apply Change** builds the `qos policy network-emulator` config, binds it to
  the interface (egress, plus ingress when the VyOS build allows it), then
  `commit` + `save`. Clearing every field and applying removes the emulation
  policy from that interface.
- **Interface Counters table** — live, refreshes every 5 s, honors VyOS
  cleared-counter offsets, with a per-row **Clear** button
  (`clear interfaces counters ethernet <if>`).
- **Static Routes table** — refreshes every 10 s and immediately after any
  change. **Add Route** button plus per-row **Edit** and **Delete**. The
  egress interface column is resolved live from the kernel routing table.
- **Live ARP table** — refreshes every 5 s.
- **Light / Dark mode** toggle (persisted in the browser).

## Run

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py          # serves http://127.0.0.1:5050
```

Open http://127.0.0.1:5050, enter the router IP / username / password and hit
**Connect**.

## Layout

| File | Purpose |
|---|---|
| `app.py` | Flask backend + REST API |
| `vyos_client.py` | netmiko SSH wrapper, VyOS command runners and parsers |
| `templates/index.html` | the single page |
| `static/app.js` | UI logic, polling, modals |
| `static/style.css` | sleek theme, light + dark palettes |
| `router_cache.json` | cached router IP + credentials (created on first connect) |

## Notes

- All configuration changes are committed **and saved** on the router. The
  config session drives the VyOS `[edit]` prompt directly, so a full
  apply/commit/save cycle takes ~3 s.
- The app talks to VyOS over SSH (netmiko `vyos` driver); one persistent
  session is shared and serialized behind a lock, with automatic reconnect.
- Editing the IP of the interface you are connected through will drop the SSH
  session — the app warns about this in the Edit IP dialog.
- Counters/ARP poll every 5 s and routes every 10 s, matching the spec.
- Credentials are stored in plain text in `router_cache.json` for convenience
  (file permissions are restricted to the current user). Delete the file to
  forget them.
