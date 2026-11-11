"""Phone only: make sure the Terminal app notices AI Amalgam's port.

The Terminal forwards a port to Android when it sees a program *start* listening on it
(its watcher, tcpstates, reports LISTEN/CLOSE events as they happen). When Linux starts
AI Amalgam at boot, AI Amalgam can start listening before that watcher is running, and
then the port is never forwarded: the app waits forever. So: whenever a new watcher shows
up after we started listening, listen again once it has had time to get going.
"""
import os
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


class PortAnnouncer:
    def __init__(self, relisten, delays=(10, 30), every=2.0):
        self.relisten = relisten
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

    def _step(self):
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
                "uptime": round(_uptime(), 1)}
