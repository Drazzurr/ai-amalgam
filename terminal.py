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
                try:
                    os.chdir(self.home)
                except OSError:
                    pass
                os.execvpe("bash", ["bash", "-l"], env)
            self.pid, self.fd, self.alive = pid, fd, True
            self.started_at = time.time()
            self._resize(cols, rows)
            self._append(b"\r\n\x1b[90m[AI Amalgam terminal started]\x1b[0m\r\n")
            threading.Thread(target=self._reader, args=(fd, pid), daemon=True).start()

    def restart(self, cols=80, rows=24):
        self.stop()
        self.start(cols, rows)

    def stop(self):
        with self._lock:
            pid, fd = self.pid, self.fd
            self.alive = False
            self.pid = self.fd = None
        if pid:
            try:
                os.killpg(os.getpgid(pid), signal.SIGHUP)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    def _reader(self, fd, pid):
        while True:
            try:
                data = os.read(fd, 65536)
            except OSError:
                data = b""
            if not data:
                break
            with self._lock:
                self._append(data)
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
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
