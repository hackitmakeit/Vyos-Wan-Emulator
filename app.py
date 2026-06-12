"""VyOS WAN Emulator control panel - Flask backend."""

import ipaddress
import json
import os
import re
import stat

from flask import Flask, jsonify, render_template, request

from vyos_client import NotConnectedError, VyosClient, VyosError

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(BASE_DIR, "router_cache.json")

NETEM_PARAMS = (
    "bandwidth",
    "delay",
    "corruption",
    "duplicate",
    "loss",
    "reordering",
    "queue-limit",
)
IFNAME_RE = re.compile(r"^eth\d+$")
MGMT_INTERFACE = "eth0"  # management interface: IP config only, no emulation
BANDWIDTH_RE = re.compile(r"^\d+(\.\d+)?(bit|kbit|mbit|gbit|tbit)?$")
NUMBER_RE = re.compile(r"^\d+(\.\d+)?$")

app = Flask(__name__)
client = VyosClient()


# ----------------------------------------------------------------- cache file

def load_cache():
    try:
        with open(CACHE_FILE) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_cache(data):
    with open(CACHE_FILE, "w") as fh:
        json.dump(data, fh, indent=2)
    os.chmod(CACHE_FILE, stat.S_IRUSR | stat.S_IWUSR)  # keep credentials private


# ------------------------------------------------------------------- helpers

def err(message, code=500):
    return jsonify({"error": str(message)}), code


def valid_ifname(name):
    return bool(IFNAME_RE.match(name or ""))


def valid_cidr(value):
    try:
        ipaddress.ip_network(value, strict=False)
        return "/" in value
    except ValueError:
        return False


def valid_ip(value):
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


@app.errorhandler(NotConnectedError)
def handle_not_connected(_):
    return err("Not connected to a router", 409)


@app.errorhandler(VyosError)
def handle_vyos_error(exc):
    return err(exc, 502)


# --------------------------------------------------------------------- pages

@app.get("/")
def index():
    return render_template("index.html")


# ---------------------------------------------------------------- connection

@app.get("/api/cached-credentials")
def cached_credentials():
    return jsonify(load_cache())


@app.post("/api/connect")
def connect():
    data = request.get_json(force=True)
    host = (data.get("host") or "").strip()
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not host or not username or not password:
        return err("Router IP, username and password are required", 400)
    if not valid_ip(host):
        return err(f"'{host}' is not a valid IP address", 400)
    try:
        client.connect(host, username, password)
    except Exception as exc:
        return err(f"Connection failed: {exc}", 502)
    save_cache({"host": host, "username": username, "password": password})
    return jsonify({"connected": True, "host": host})


@app.post("/api/disconnect")
def disconnect():
    client.disconnect()
    return jsonify({"connected": False})


@app.get("/api/status")
def status():
    return jsonify({"connected": client.is_connected(), "host": client.host})


# ---------------------------------------------------------------- interfaces

@app.get("/api/interfaces")
def interfaces():
    return jsonify({"interfaces": client.get_interfaces()})


@app.post("/api/interfaces/<ifname>/address")
def set_address(ifname):
    if not valid_ifname(ifname):
        return err("Invalid interface name", 400)
    address = (request.get_json(force=True).get("address") or "").strip()
    if not valid_cidr(address):
        return err("Address must be in CIDR form, e.g. 10.1.1.15/24", 400)
    cmds = [
        f"delete interfaces ethernet {ifname} address",
        f"set interfaces ethernet {ifname} address {address}",
    ]
    client.configure(cmds)
    return jsonify({"ok": True, "message": f"{ifname} address set to {address}"})


@app.post("/api/interfaces/<ifname>/netem")
def set_netem(ifname):
    if not valid_ifname(ifname):
        return err("Invalid interface name", 400)
    if ifname == MGMT_INTERFACE:
        return err(f"{MGMT_INTERFACE} is the management interface; "
                   "network emulation is not allowed on it", 400)
    data = request.get_json(force=True)

    params = {}
    for key in NETEM_PARAMS:
        value = str(data.get(key.replace("-", "_"), "") or "").strip()
        if not value:
            continue
        if key == "bandwidth":
            if not BANDWIDTH_RE.match(value):
                return err("Bandwidth must look like '20mbit' or '500kbit'", 400)
        elif not NUMBER_RE.match(value):
            return err(f"'{key}' must be a number", 400)
        params[key] = value

    # Reuse the policy already bound to this interface (e.g. eth1_policy),
    # otherwise create our own conventional name. The lookup is served from
    # cache (populated when the interface cards load) to avoid an extra
    # SSH round trip on every apply.
    bound = client.get_bound_policy(ifname)
    policy = bound if re.match(r"^[\w.-]+$", bound) else f"{ifname}-wanem"
    if not params:
        # Nothing set -> remove emulation from this interface entirely.
        cmds = [
            f"delete qos interface {ifname}",
            f"delete qos policy network-emulator {policy}",
        ]
        client.configure(cmds)
        return jsonify({"ok": True, "message": f"Network emulation removed from {ifname}"})

    cmds = [f"delete qos policy network-emulator {policy}"]
    cmds += [f"set qos policy network-emulator {policy} {k} {v}" for k, v in params.items()]
    # Egress only: netem policies are rejected on ingress by VyOS (only
    # limiter policies are allowed there), and attempting it costs a full
    # failed-commit + discard + retry cycle.
    cmds.append(f"set qos interface {ifname} egress {policy}")
    client.configure(cmds)
    return jsonify({"ok": True, "message": f"Policy {policy} applied to {ifname}"})


# ------------------------------------------------------------------ counters

@app.get("/api/live")
def live():
    """Combined poll target: interface counters + ARP table."""
    return jsonify({"counters": client.get_counters(), "arp": client.get_arp_table()})


@app.post("/api/interfaces/<ifname>/clear-counters")
def clear_counters(ifname):
    if not valid_ifname(ifname):
        return err("Invalid interface name", 400)
    client.clear_counters(ifname)
    return jsonify({"ok": True, "message": f"Counters cleared on {ifname}"})


# -------------------------------------------------------------------- routes

@app.get("/api/routes")
def routes():
    return jsonify({"routes": client.get_static_routes()})


def _route_cmds(network, next_hop, interface):
    cmds = []
    if next_hop:
        cmds.append(f"set protocols static route {network} next-hop {next_hop}")
    if interface:
        cmds.append(f"set protocols static route {network} interface {interface}")
    return cmds


def _validate_route(data):
    network = (data.get("network") or "").strip()
    next_hop = (data.get("next_hop") or "").strip()
    interface = (data.get("interface") or "").strip()
    if not valid_cidr(network):
        return None, err("Network must be in CIDR form, e.g. 10.1.1.0/24", 400)
    if not next_hop:
        return None, err("Next-hop IP is required", 400)
    if next_hop and not valid_ip(next_hop):
        return None, err("Next-hop must be a valid IP address", 400)
    if interface and not valid_ifname(interface):
        return None, err("Interface must look like eth0", 400)
    return (network, next_hop, interface), None


@app.post("/api/routes")
def add_route():
    parsed, failure = _validate_route(request.get_json(force=True))
    if failure:
        return failure
    network, next_hop, interface = parsed
    client.configure(_route_cmds(network, next_hop, interface))
    return jsonify({"ok": True, "message": f"Route {network} added"})


@app.put("/api/routes")
def edit_route():
    data = request.get_json(force=True)
    original = (data.get("original_network") or "").strip()
    if not valid_cidr(original):
        return err("Missing original route network", 400)
    parsed, failure = _validate_route(data)
    if failure:
        return failure
    network, next_hop, interface = parsed
    cmds = [f"delete protocols static route {original}"]
    cmds += _route_cmds(network, next_hop, interface)
    client.configure(cmds)
    return jsonify({"ok": True, "message": f"Route {original} updated"})


@app.delete("/api/routes")
def delete_route():
    network = (request.get_json(force=True).get("network") or "").strip()
    if not valid_cidr(network):
        return err("Invalid route network", 400)
    client.configure([f"delete protocols static route {network}"])
    return jsonify({"ok": True, "message": f"Route {network} deleted"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5050"))
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
