"""Phone only: run AI Amalgam as a system service in the Terminal's Linux system.

With the service, Linux starts AI Amalgam the moment it boots (and again if it ever
crashes), so the phone app never depends on a Terminal window running ~/.bashrc.
Installed by the app itself; needs the Terminal's password-free sudo.
"""
import os
import pwd
import shutil
import subprocess
import sys

NAME = "ai-amalgam"
UNIT_PATH = f"/etc/systemd/system/{NAME}.service"


def under_service():
    """True when systemd started this copy (it restarts us, so updates just exit)."""
    return os.environ.get("AMALGAM_SERVICE") == "1"


def _systemd():
    return sys.platform.startswith("linux") and os.path.isdir("/run/systemd/system") and shutil.which("systemctl")


def _sudo(*cmd, timeout=20):
    try:
        r = subprocess.run(["sudo", "-n", *cmd], capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
        return r.returncode == 0, (r.stderr or r.stdout).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)


def unit_text(app_dir, python=None):
    user = pwd.getpwuid(os.getuid())
    home = user.pw_dir
    path = ":".join([f"{home}/.local/bin", f"{home}/.npm-global/bin", "/usr/local/sbin", "/usr/local/bin",
                     "/usr/sbin", "/usr/bin", "/sbin", "/bin"])
    return f"""[Unit]
Description=AI Amalgam
After=network.target
StartLimitIntervalSec=120
StartLimitBurst=20

[Service]
User={user.pw_name}
WorkingDirectory={app_dir}
Environment=HOME={home}
Environment=PATH={path}
Environment=LANG=C.UTF-8
Environment=PYTHONUNBUFFERED=1
Environment=DISABLE_AUTOUPDATER=1
Environment=AMALGAM_SERVICE=1
ExecStart={python or sys.executable} {app_dir}/app.py --phone
Restart=always
RestartSec=2
TimeoutStopSec=60

[Install]
WantedBy=multi-user.target
"""


def installed():
    return os.path.isfile(UNIT_PATH)


def ensure(app_dir):
    """Install or refresh the service and turn it on for every boot. Returns (ok, message)."""
    if not _systemd():
        return False, "no systemd here"
    want = unit_text(app_dir)
    try:
        have = open(UNIT_PATH, encoding="utf-8").read()
    except OSError:
        have = ""
    if have != want:
        try:
            r = subprocess.run(["sudo", "-n", "tee", UNIT_PATH], input=want, capture_output=True, text=True,
                               timeout=20)
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, f"couldn't write the service: {e}"
        if r.returncode != 0:
            return False, f"couldn't write the service: {r.stderr.strip()}"
        ok, msg = _sudo("systemctl", "daemon-reload")
        if not ok:
            return False, f"daemon-reload failed: {msg}"
    ok, msg = _sudo("systemctl", "is-enabled", "--quiet", NAME)
    if not ok:
        ok, msg = _sudo("systemctl", "enable", NAME)
        if not ok:
            return False, f"couldn't turn the service on: {msg}"
    return True, "installed" if have != want else "ok"


def hand_over(app_dir, delay=4):
    """This copy wasn't started by systemd: after it closes, let the service take over.
    If the service doesn't come up, start AI Amalgam the old way so it's never left off."""
    import shlex
    fallback = (f"cd {shlex.quote(app_dir)} && AMALGAM_NO_HANDOVER=1 setsid nohup "
                f"{shlex.quote(sys.executable)} app.py --phone >/dev/null 2>&1 </dev/null &")
    script = (f"sleep {delay}; sudo -n systemctl reset-failed {NAME}; sudo -n systemctl start {NAME}; "
              f"sleep 10; sudo -n systemctl is-active --quiet {NAME} || {{ {fallback} }}")
    subprocess.Popen(["sh", "-c", script],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, close_fds=True)


def stop_for_good():
    """'Close AI Amalgam' on the phone: stop the service (otherwise it would come straight back)."""
    subprocess.Popen(["sudo", "-n", "systemctl", "stop", NAME], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
