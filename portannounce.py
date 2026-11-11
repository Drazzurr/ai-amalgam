"""Phone only: make sure the Terminal app notices AI Amalgam's port.

The Terminal forwards a port to Android when it sees a program *start* listening on it
(its watcher, tcpstates, reports LISTEN/CLOSE events as they happen). When Linux starts
AI Amalgam at boot, AI Amalgam can start listening before that watcher is running, and
then the port is never forwarded: the app waits forever. So: whenever a new watcher shows
up after we started listening, listen again once it has had time to get going.
"""
import base64
import os
import socket
import threading
import time

MARKERS = (b"tcpstates", b"forwarder_guest_launcher")


def _uptime():
    try:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return time.monotonic()


def watchers():
    """{pid: start time in seconds since boot} of the Terminal's port watchers."""
    found = {}
    try:
        tick = os.sysconf("SC_CLK_TCK")
        names = os.listdir("/proc")
    except (OSError, ValueError):
        return found
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/cmdline", "rb") as f:
                cmd = f.read()
            if not any(m in cmd for m in MARKERS):
                continue
            with open(f"/proc/{name}/stat") as f:
                fields = f.read().rsplit(")", 1)[1].split()
            found[int(name)] = int(fields[19]) / tick      # field 22: start time
        except (OSError, ValueError, IndexError):
            pass
    return found


def reachable_from_android(gateway, proxy, port=8765, timeout=6):
    """Test the path the phone app uses, from inside Linux: ask the app's connection helper
    (on Android, at the gateway) to connect to 127.0.0.1:<port> on Android, which only works
    when the Terminal forwards our port. Returns True/False, or None when it can't tell
    (no helper running, unknown password)."""
    if not gateway or not proxy or not proxy.get("password"):
        return None
    auth = base64.b64encode(f"{proxy.get('user', 'amalgam')}:{proxy['password']}".encode()).decode()
    target = f"127.0.0.1:{port}"
    try:
        s = socket.create_connection((gateway, int(proxy["port"])), timeout=timeout)
    except (OSError, ValueError):
        return None
    try:
        s.settimeout(timeout)
        s.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\nProxy-Authorization: Basic {auth}\r\n\r\n".encode())
        head = b""
        while b"\r\n\r\n" not in head and len(head) < 4096:
            chunk = s.recv(1024)
            if not chunk:
                break
            head += chunk
        first = head.split(b"\r\n", 1)[0]
        if b" 407" in first or not first.startswith(b"HTTP/"):
            return None
        if b" 200" not in first:
            return False                      # Android couldn't connect: the port isn't forwarded
        s.sendall(b"GET /manifest.webmanifest HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
        body = head.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in head else b""
        while len(body) < 65536:
            try:
                chunk = s.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            body += chunk
            if b"AI Amalgam" in body:
                return True
        return b"AI Amalgam" in body
    except OSError:
        return False
    finally:
        s.close()


class PortAnnouncer:
    def __init__(self, relisten, delays=(10, 30), every=2.0, check=None):
        self.relisten = relisten
        self.check = check            # () -> True/False/None: can Android reach us right now?
        self.next_check = time.monotonic() + 20
        self.check_result = None
        self.check_at = None
        self.fixes = 0
        self.started_mono = time.monotonic()
        self.delays = delays          # seconds after a watcher starts (it compiles its probe first)
        self.every = every
        self.listening_since = _uptime()
        self.first_listen = self.listening_since   # seconds after Linux booted that AI Amalgam was ready
        self.last_announce = None
        self.seen = set()
        self.due = []                 # uptimes at which to listen again
        self.count = 0

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def note_listening(self):
        self.listening_since = _uptime()

    def _loop(self):
        while True:
            try:
                self._step()
            except Exception as e:
                print("Port announcer:", e)
            time.sleep(self.every)

    def _check_forwarding(self):
        """Every so often, test the path the phone app uses; if Android can't reach us even though
        we're running, the Terminal lost our port: listen again so it picks it up."""
        if not self.check or time.monotonic() < self.next_check:
            return
        result = self.check()
        self.check_result, self.check_at = result, time.time()
        young = time.monotonic() - self.started_mono < 300
        if result is False:
            self.fixes += 1
            print("The phone app can't reach AI Amalgam: listening again so the Terminal forwards it")
            self.relisten()
            self.note_listening()
            self.next_check = time.monotonic() + 6
        else:
            self.next_check = time.monotonic() + (15 if young else 60)

    def _step(self):
        self._check_forwarding()
        for pid, started in watchers().items():
            if pid in self.seen:
                continue
            self.seen.add(pid)
            if started > self.listening_since - max(self.delays):
                # this watcher may have missed our port: tell it again once it's ready
                self.due += [started + d for d in self.delays]
        now = _uptime()
        if any(t <= now for t in self.due):
            self.due = [t for t in self.due if t > now]
            self.count += 1
            print("Listening again so the Terminal forwards the port")
            self.relisten()
            self.note_listening()
            if self.last_announce is None:
                self.last_announce = self.listening_since      # the first re-announce

    def timing(self):
        """How this start went, in seconds after Linux booted."""
        return {"ready": round(self.first_listen, 1),
                "announced": round(self.last_announce, 1) if self.last_announce else None,
                "uptime": round(_uptime(), 1),
                "android_ok": self.check_result, "fixes": self.fixes}
