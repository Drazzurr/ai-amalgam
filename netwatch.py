"""Phone only: keep the Linux system's internet working when the phone switches between Wi-Fi and
mobile data.

Every 30 seconds (and whenever something fails to reach the internet) it checks whether GitHub,
Anthropic and OpenAI can be reached. If not, it tries to repair the connection by itself, in
gentle steps: renew the network address, restart the network services, flush name lookups,
and as a last resort add public name servers.

On mobile data, Android doesn't give the Linux system internet at all. Then Amalgam uses the
AI Amalgam Android app's built-in proxy (the app runs on the phone, which does have internet):
all of Amalgam's traffic (Claude, Codex, git, updates) goes through it until the Linux system's
own connection works again. After a repair it re-checks, tells sync to run,
and re-checks for updates. Only if nothing works does the app show a note asking you to restart
the Terminal app.

The repairs use `sudo -n` (never asks for a password; the Pixel Terminal's user doesn't need one).
"""

import base64
import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.request

HOSTS = ("github.com", "api.anthropic.com", "chatgpt.com")
CHECK_EVERY = 30          # seconds, while things are fine
RETRY_EVERY = 10          # seconds, while broken
FALLBACK_DNS = ("1.1.1.1", "8.8.8.8")


def _reach(host, port=443, timeout=4):
    """True if a name lookup and a connection both work."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return "dns"
    for family, kind, proto, _name, addr in infos[:4]:
        s = socket.socket(family, kind, proto)
        s.settimeout(timeout)
        try:
            s.connect(addr)
            return "ok"
        except OSError:
            continue
        finally:
            s.close()
    return "route"


def _ip_reach(ip="1.1.1.1", port=443, timeout=4):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((ip, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _sudo(*cmd, timeout=25):
    if not shutil.which("sudo") or not shutil.which(cmd[0]):
        return False
    try:
        r = subprocess.run(["sudo", "-n", *cmd], capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
        return r.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


PROXY_VARS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")


def gateway():
    """The phone's side of the Linux system's network link (where the Android app listens)."""
    try:
        with open("/proc/net/route", encoding="ascii") as f:
            for line in f.readlines()[1:]:
                parts = line.split()
                if len(parts) > 2 and parts[1] == "00000000" and parts[2] != "00000000":
                    raw = bytes.fromhex(parts[2])[::-1]
                    return socket.inet_ntoa(raw)
    except (OSError, ValueError):
        pass
    return None


def _proxy_works(host, port, user, password, target="github.com:443", timeout=8):
    """Ask the app's proxy to open a connection to GitHub; True if it says yes."""
    auth = base64.b64encode(f"{user}:{password}".encode()).decode()
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\nProxy-Authorization: Basic {auth}\r\n\r\n".encode())
            s.settimeout(timeout)
            first = s.recv(64).decode("latin-1", "replace")
            return first.startswith("HTTP/1.1 200") or first.startswith("HTTP/1.0 200")
    except OSError:
        return False


def _set_proxy_env(url):
    for k in PROXY_VARS:
        if url:
            os.environ[k] = url
        else:
            os.environ.pop(k, None)
    if url:
        os.environ["NO_PROXY"] = os.environ["no_proxy"] = "localhost,127.0.0.1,::1"
    # Python keeps the proxy settings it read first; make it read them again
    urllib.request.install_opener(urllib.request.build_opener())


class NetWatch:
    def __init__(self, on_restored=None, log=print, proxy=None):
        self.on_restored = on_restored or (lambda: None)
        self.log = log
        self.proxy = proxy          # {"port", "user", "password"} from the Android app
        self.via_proxy = False
        self.state = {"ok": True, "checked": 0, "problem": "", "fixing": False, "fixed_at": 0, "fixes": 0,
                      "gave_up": False, "via": None, "app": bool(proxy)}
        self._wake = threading.Event()
        self._last_fix = 0

    def view(self):
        return dict(self.state)

    def set_proxy(self, info):
        """The Android app told us how to reach its proxy."""
        self.proxy = info
        self.state["app"] = bool(info)
        self.poke()

    def _try_proxy(self):
        if not self.proxy:
            return False
        gw = gateway()
        if not gw:
            return False
        p = self.proxy
        if not _proxy_works(gw, int(p["port"]), p["user"], p["password"]):
            return False
        url = f"http://{p['user']}:{p['password']}@{gw}:{int(p['port'])}"
        if os.environ.get("HTTPS_PROXY") != url:
            _set_proxy_env(url)
            self.log(f"Network: no internet of our own; using the AI Amalgam app's connection ({gw}).")
        self.via_proxy = True
        return True

    def _drop_proxy(self):
        if self.via_proxy:
            _set_proxy_env(None)
            self.via_proxy = False
            self.log("Network: own internet is back; no longer going through the app.")

    def poke(self):
        """Something just failed to reach the internet: check now."""
        self._wake.set()

    def check(self):
        results = {h: _reach(h) for h in HOSTS}
        return any(v == "ok" for v in results.values()), results

    def loop(self, quit_event):
        while not quit_event.is_set():
            ok, results = self.check()           # always the Linux system's own connection
            self.state["checked"] = time.time()
            if ok:
                was_proxy = self.via_proxy
                self._drop_proxy()
                if not self.state["ok"] or was_proxy:
                    if not was_proxy:
                        self.log("Network: back by itself.")
                    self._restored()
                self.state.update(ok=True, problem="", gave_up=False, via=None)
            elif self._try_proxy():
                if not self.state["ok"] or self.state["via"] != "app":
                    self._restored()
                self.state.update(ok=True, problem="", gave_up=False, via="app")
            else:
                self._drop_proxy()
                self.state.update(ok=False, via=None)
                self._repair(results)
            self._wake.wait(CHECK_EVERY if self.state["ok"] else RETRY_EVERY)
            self._wake.clear()

    def _restored(self):
        self.state.update(ok=True, problem="", fixed_at=time.time(), gave_up=False)
        self.state["via"] = "app" if self.via_proxy else None
        try:
            self.on_restored()
        except Exception as e:
            self.log(f"Network: refresh after reconnect failed: {e}")

    def _repair(self, results):
        if time.time() - self._last_fix < 45:      # give the last repair time to settle
            return
        self._last_fix = time.time()
        dns_only = all(v == "dns" for v in results.values()) and _ip_reach()
        self.state.update(fixing=True, problem="dns" if dns_only else "offline")
        self.log(f"Network: can't reach the internet ({results}); trying to repair.")
        steps = []
        if not dns_only:
            steps += [
                ("renew address", lambda: _sudo("networkctl", "renew", self._iface() or "eth0")
                 or _sudo("dhclient", "-1", "-v")),
                ("restart network", lambda: _sudo("systemctl", "restart", "systemd-networkd")
                 or _sudo("systemctl", "restart", "networking")),
            ]
        steps += [
            ("flush name lookups", lambda: _sudo("resolvectl", "flush-caches")
             or _sudo("systemctl", "restart", "systemd-resolved")),
            ("add public name servers", self._fallback_dns),
        ]
        try:
            for name, step in steps:
                if not step():
                    continue
                time.sleep(3)
                ok, _ = self.check()
                if ok:
                    self.state["fixes"] += 1
                    self.log(f"Network: repaired ({name}).")
                    self._restored()
                    return
            self.state["gave_up"] = True
            self.log("Network: still down after repairs. Probably the phone itself is offline, "
                     "or the Terminal needs a restart.")
        finally:
            self.state["fixing"] = False

    @staticmethod
    def _iface():
        try:
            for name in os.listdir("/sys/class/net"):
                if name != "lo" and not name.startswith(("docker", "veth", "br-")):
                    return name
        except OSError:
            pass
        return None

    @staticmethod
    def _fallback_dns():
        """Only when name lookups fail but the internet works: add public servers after the existing ones."""
        try:
            with open("/etc/resolv.conf", encoding="utf-8") as f:
                cur = f.read()
        except OSError:
            cur = ""
        if all(ip in cur for ip in FALLBACK_DNS) or os.path.islink("/etc/resolv.conf"):
            return False
        add = "".join(f"nameserver {ip}\n" for ip in FALLBACK_DNS if ip not in cur)
        try:
            r = subprocess.run(["sudo", "-n", "tee", "-a", "/etc/resolv.conf"], input=add.encode(),
                               capture_output=True, timeout=10)
            return r.returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            return False
