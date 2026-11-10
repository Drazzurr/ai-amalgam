"""The Terminal tab (phone / Linux): a real shell inside AI Amalgam's Linux system, shown in the app.

One shell session at a time. Its output is kept in a buffer (the last 512 KB), so the app can
reconnect and see what happened. The app asks for new output with a long poll (waits up to
20 seconds for something to arrive), which costs nothing while the shell is quiet.
"""

import base64
import fcntl
import os
import pty
import signal
import struct
import termios
import threading
import time

KEEP = 512 * 1024


def _hang_up_session(sid):
    """Like closing a terminal window: send a hangup to every job the shell started.
    Background jobs get their own process groups, so signalling only the shell's group
    would leave them running (and holding the terminal open)."""
    groups = {sid}
    try:
        for name in os.listdir("/proc"):
            if name.isdigit():
                try:
                    with open(f"/proc/{name}/stat") as f:
                        fields = f.read().rsplit(")", 1)[1].split()
                    if int(fields[3]) == sid:      # field 6 (session) after pid and comm
                        groups.add(int(fields[2]))   # field 5 (process group)
                except (OSError, ValueError, IndexError):
                    pass
    except OSError:
        pass
    for g in groups:
        for sig in (signal.SIGHUP, signal.SIGCONT):
            try:
                os.killpg(g, sig)
            except OSError:
                pass


class Terminal:
    def __init__(self, home=None):
        self.home = home or os.path.expanduser("~")
        self._lock = threading.Condition()
        self.pid = None
        self.fd = None
        self.buf = bytearray()
        self.start_offset = 0         # absolute position of buf[0]
        self.alive = False
        self.started_at = 0

    # ----- life cycle -----

    def start(self, cols=80, rows=24):
        with self._lock:
            if self.alive:
                self._resize(cols, rows)
                return
            env = dict(os.environ, TERM="xterm-256color", COLORTERM="truecolor", LANG="C.UTF-8",
                       PATH=os.path.expanduser("~/.local/bin") + ":" + os.path.expanduser("~/.npm-global/bin")
                       + ":" + os.environ.get("PATH", "/usr/bin:/bin"),
                       AMALGAM_TERMINAL_TAB="1")
            env.pop("COLUMNS", None)
            env.pop("LINES", None)
            pid, fd = pty.fork()
            if pid == 0:                                   # the shell
                # The app is started with "nohup ... &" (and Python ignores SIGPIPE), so these
                # arrive ignored; without a reset Ctrl+C would do nothing in the Terminal tab.
                for sig in (signal.SIGINT, signal.SIGQUIT, signal.SIGHUP, signal.SIGPIPE,
                            signal.SIGTERM, signal.SIGTSTP, signal.SIGTTIN, signal.SIGTTOU):
                    try:
                        signal.signal(sig, signal.SIG_DFL)
                    except (OSError, ValueError):
                        pass
                try:
                    os.chdir(self.home)
                except OSError:
                    pass
                os.execvpe("bash", ["bash", "-l"], env)
            self.pid, self.fd, self.alive = pid, fd, True
            self.started_at = time.time()
            self._resize(cols, rows)
            self._append(b"\r\n\x1b[90m[AI Amalgam terminal started]\x1b[0m\r\n")
            reader = threading.Thread(target=self._reader, args=(fd, pid), daemon=True)
            reader.start()
            threading.Thread(target=self._waiter, args=(pid, reader), daemon=True).start()

    def restart(self, cols=80, rows=24):
        self.stop()
        self.start(cols, rows)

    def stop(self):
        with self._lock:
            pid = self.pid
            self.alive = False
            self.pid = self.fd = None
        if pid:
            _hang_up_session(pid)
        # The handle is closed by its own reader once the shell's side shuts. Closing it
        # here would let the next shell reuse the same number under the old reader.

    def _reader(self, fd, pid):
        while True:
            try:
                data = os.read(fd, 65536)
            except OSError:
                data = b""
            if not data:
                break
            with self._lock:
                if self.pid == pid:          # late output from a shell that was replaced is dropped
                    self._append(data)
        try:
            os.close(fd)
        except OSError:
            pass

    def _waiter(self, pid, reader):
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
        reader.join(1.5)                     # let its last words reach the screen
        with self._lock:
            if self.pid == pid:
                self.alive = False
                self.pid = self.fd = None
                self._append(b"\r\n\x1b[90m[shell closed - press Restart to open a new one]\x1b[0m\r\n")

    def _append(self, data):
        """(lock held) add output and wake anyone waiting for it"""
        self.buf += data
        if len(self.buf) > KEEP:
            cut = len(self.buf) - KEEP
            del self.buf[:cut]
            self.start_offset += cut
        self._lock.notify_all()

    # ----- input / output -----

    def write(self, text):
        with self._lock:
            fd = self.fd
        if fd is None:
            return False
        data = text.encode("utf-8", "surrogatepass")
        while data:
            try:
                n = os.write(fd, data)
            except OSError:
                return False
            data = data[n:]
        return True

    def _resize(self, cols, rows):
        if self.fd is None:
            return
        try:
            cols = max(20, min(500, int(cols)))
            rows = max(5, min(300, int(rows)))
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        except (OSError, ValueError, TypeError):
            pass

    def resize(self, cols, rows):
        with self._lock:
            self._resize(cols, rows)

    def read(self, offset, wait=20.0):
        """Output from absolute position `offset`. Waits up to `wait` seconds if there's nothing new."""
        deadline = time.time() + wait
        with self._lock:
            while True:
                end = self.start_offset + len(self.buf)
                if offset < end or not self.alive and offset >= end and wait <= 0:
                    break
                left = deadline - time.time()
                if left <= 0:
                    break
                self._lock.wait(left)
            end = self.start_offset + len(self.buf)
            if offset < self.start_offset:          # fell behind the buffer (or first load): send all we have
                offset = self.start_offset
            chunk = bytes(self.buf[offset - self.start_offset:])
            return {"data": base64.b64encode(chunk).decode("ascii"), "next": end, "alive": self.alive}
