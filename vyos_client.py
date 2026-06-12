"""SSH client wrapper + output parsers for VyOS routers (netmiko based)."""

import json
import re
import threading

from netmiko import ConnectHandler


class VyosError(Exception):
    pass


class NotConnectedError(VyosError):
    pass


# Words that indicate a `set`/`delete` line was rejected before commit.
_SET_ERROR_RE = re.compile(
    r"(Set failed|Delete failed|is not valid|Invalid command|Configuration path .+ is not valid)",
    re.IGNORECASE,
)


class VyosClient:
    def __init__(self):
        self._conn = None
        self._params = None
        self._lock = threading.RLock()
        self._netem_cache = None

    # ------------------------------------------------------------- connection

    def connect(self, host, username, password, port=22):
        with self._lock:
            self.disconnect()
            params = dict(
                device_type="vyos",
                host=host,
                username=username,
                password=password,
                port=port,
                conn_timeout=10,
                banner_timeout=20,
                auth_timeout=20,
            )
            self._conn = ConnectHandler(**params)
            self._params = params

    def disconnect(self):
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.disconnect()
                except Exception:
                    pass
            self._conn = None

    def is_connected(self):
        with self._lock:
            if self._conn is None:
                return False
            try:
                return self._conn.is_alive()
            except Exception:
                return False

    @property
    def host(self):
        return self._params["host"] if self._params else None

    def _reconnect(self):
        if not self._params:
            raise NotConnectedError("Not connected to a router")
        try:
            if self._conn is not None:
                self._conn.disconnect()
        except Exception:
            pass
        self._conn = ConnectHandler(**self._params)

    def _ensure(self):
        if self._conn is None:
            if self._params:
                self._reconnect()
            else:
                raise NotConnectedError("Not connected to a router")
        return self._conn

    # ------------------------------------------------------------- execution

    def run(self, cmd, read_timeout=30):
        """Run an operational-mode (or shell) command and return its output."""
        with self._lock:
            conn = self._ensure()
            try:
                return conn.send_command(cmd, read_timeout=read_timeout)
            except Exception:
                # one transparent reconnect+retry for dropped sessions
                self._reconnect()
                return self._conn.send_command(cmd, read_timeout=read_timeout)

    # VyOS prints "[edit]" with every config-mode prompt; expecting it is far
    # faster than netmiko's generic config helpers (~3 s vs ~16 s per commit).
    _EDIT = r"\[edit\]"

    def configure(self, cmds, save=True, read_timeout=90):
        """Enter config mode, apply commands, commit and save."""
        with self._lock:
            conn = self._ensure()
            output = ""
            try:
                conn.send_command("configure", expect_string=self._EDIT, read_timeout=30)
                for cmd in cmds:
                    out = conn.send_command(cmd, expect_string=self._EDIT, read_timeout=30)
                    output += out
                    m = _SET_ERROR_RE.search(out)
                    if m:
                        raise VyosError(f"Command rejected: {m.group(0)} ({cmd})")
                out = conn.send_command(
                    "commit", expect_string=self._EDIT, read_timeout=read_timeout
                )
                output += out
                if re.search(r"(commit failed|failed to commit|commit aborted)", out, re.I):
                    raise VyosError(f"Commit failed: {out.strip()[:500]}")
            except VyosError:
                self._abort_config(conn)
                raise
            except Exception as exc:
                self._abort_config(conn)
                raise VyosError(f"Configuration failed: {exc}") from exc
            try:
                if save:
                    output += conn.send_command(
                        "save", expect_string=self._EDIT, read_timeout=30
                    )
                conn.send_command("exit", expect_string=r"\$", read_timeout=15)
            except Exception:
                pass
            return output

    @staticmethod
    def _abort_config(conn):
        try:
            conn.send_command_timing("exit discard")
        except Exception:
            pass

    # --------------------------------------------------------------- queries

    def _run_json(self, cmd):
        out = self.run(cmd).strip()
        start = out.find("[")
        if start < 0:
            start = out.find("{")
        if start < 0:
            raise VyosError(f"No JSON in output of '{cmd}': {out[:200]}")
        return json.loads(out[start:])

    def get_interfaces(self):
        """Ethernet interfaces with MAC, addresses and current netem policy."""
        links = self._run_json("ip -j addr show")
        netem = self.get_netem_config()
        interfaces = []
        for link in links:
            name = link.get("ifname", "")
            if not re.match(r"^eth\d+$", name):
                continue
            addrs = [
                f"{a['local']}/{a['prefixlen']}"
                for a in link.get("addr_info", [])
                if a.get("family") == "inet"
            ]
            interfaces.append(
                {
                    "name": name,
                    "mac": link.get("address", ""),
                    "addresses": addrs,
                    "oper_state": link.get("operstate", "UNKNOWN"),
                    "netem": netem.get(name, {}),
                }
            )
        interfaces.sort(key=lambda i: i["name"])
        return interfaces

    def get_netem_config(self):
        """Map interface name -> netem parameter dict from the running config."""
        out = self.run("show configuration commands | match qos")
        policies = {}
        if_policy = {}
        for line in out.splitlines():
            line = line.strip()
            m = re.match(
                r"set qos policy network-emulator '?(\S+?)'? (\S+) '?([^']*)'?$", line
            )
            if m:
                policies.setdefault(m.group(1), {})[m.group(2)] = m.group(3)
                continue
            m = re.match(r"set qos interface '?(\S+?)'? egress '?([^']*)'?$", line)
            if m:
                if_policy[m.group(1)] = m.group(2)
        result = {}
        for ifname, pol in if_policy.items():
            if pol in policies:
                result[ifname] = dict(policies[pol], _policy=pol)
        self._netem_cache = result
        return result

    def get_bound_policy(self, ifname):
        """Policy name bound to an interface, from cache when available."""
        cache = self._netem_cache
        if cache is None:
            cache = self.get_netem_config()
        return cache.get(ifname, {}).get("_policy", "")

    def get_counters(self):
        """Interface counters honoring VyOS 'clear counters' offsets."""
        out = self.run("show interfaces counters")
        rows = self._parse_counters_table(out)
        if rows:
            return rows
        return self._counters_from_ip()

    @staticmethod
    def _parse_counters_table(out):
        lines = [l for l in out.splitlines() if l.strip()]
        header_idx = next(
            (i for i, l in enumerate(lines) if "Interface" in l and "Packets" in l),
            None,
        )
        if header_idx is None:
            return []
        headers = re.split(r"\s{2,}", lines[header_idx].strip())
        keys = [h.strip().lower().replace(" ", "_") for h in headers]
        rows = []
        for line in lines[header_idx + 1:]:
            if set(line.strip()) <= set("- "):
                continue
            parts = line.split()
            if len(parts) != len(keys):
                continue
            row = dict(zip(keys, parts))
            name = row.pop("interface", None)
            if not name or not re.match(r"^eth\d+$", name):
                continue
            norm = {"interface": name}
            for k, v in row.items():
                # normalize e.g. rx_dropped -> rx_drops
                k = k.replace("dropped", "drops")
                try:
                    norm[k] = int(v)
                except ValueError:
                    norm[k] = v
            rows.append(norm)
        rows.sort(key=lambda r: r["interface"])
        return rows

    def _counters_from_ip(self):
        links = self._run_json("ip -j -s link show")
        rows = []
        for link in links:
            name = link.get("ifname", "")
            if not re.match(r"^eth\d+$", name):
                continue
            rx = link.get("stats64", {}).get("rx", {})
            tx = link.get("stats64", {}).get("tx", {})
            rows.append(
                {
                    "interface": name,
                    "rx_packets": rx.get("packets", 0),
                    "rx_bytes": rx.get("bytes", 0),
                    "rx_drops": rx.get("dropped", 0),
                    "rx_errors": rx.get("errors", 0),
                    "tx_packets": tx.get("packets", 0),
                    "tx_bytes": tx.get("bytes", 0),
                    "tx_drops": tx.get("dropped", 0),
                    "tx_errors": tx.get("errors", 0),
                }
            )
        rows.sort(key=lambda r: r["interface"])
        return rows

    def clear_counters(self, ifname):
        out = self.run(f"clear interfaces counters ethernet {ifname}")
        if re.search(r"(Invalid command|is not valid|Incomplete command)", out):
            out = self.run(f"clear interfaces ethernet {ifname} counters")
        return out

    def get_static_routes(self):
        """Static routes from the running configuration, grouped by network."""
        out = self.run('show configuration commands | match "protocols static route"')
        routes = {}
        for line in out.splitlines():
            m = re.match(
                r"set protocols static route '?(\S+?)'?(?:\s+(next-hop|interface)\s+'?([^'\s]+)'?)?(?:\s.*)?$",
                line.strip(),
            )
            if not m:
                continue
            net = m.group(1)
            entry = routes.setdefault(net, {"network": net, "next_hops": [], "interfaces": []})
            if m.group(2) == "next-hop" and m.group(3) not in entry["next_hops"]:
                entry["next_hops"].append(m.group(3))
            elif m.group(2) == "interface" and m.group(3) not in entry["interfaces"]:
                entry["interfaces"].append(m.group(3))
        self._resolve_route_interfaces(routes)
        return sorted(routes.values(), key=lambda r: r["network"])

    def _resolve_route_interfaces(self, routes):
        """Fill in the egress interface of each route from the kernel table."""
        if not routes:
            return
        try:
            kernel = self._run_json("ip -j -4 route show")
        except (VyosError, ValueError):
            return
        kmap = {}
        for entry in kernel:
            dst = entry.get("dst", "")
            if dst == "default":
                dst = "0.0.0.0/0"
            elif "/" not in dst:
                dst += "/32"
            devs = kmap.setdefault(dst, [])
            for dev in [entry.get("dev")] + [
                nh.get("dev") for nh in entry.get("nexthops", [])
            ]:
                if dev and dev not in devs:
                    devs.append(dev)
        for route in routes.values():
            for dev in kmap.get(route["network"], []):
                if dev not in route["interfaces"]:
                    route["interfaces"].append(dev)

    def get_arp_table(self):
        neighbors = self._run_json("ip -j -4 neigh show")
        rows = []
        for n in neighbors:
            rows.append(
                {
                    "ip": n.get("dst", ""),
                    "mac": n.get("lladdr", ""),
                    "interface": n.get("dev", ""),
                    "state": ",".join(n.get("state", [])),
                }
            )
        rows.sort(key=lambda r: (r["interface"], r["ip"]))
        return rows
