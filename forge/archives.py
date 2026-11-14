"""Open mod archives: .zip with Python itself, .7z with py7zr (or 7-Zip), .rar with 7-Zip.

Only reads and extracts into Amalgam's own working folders; never runs anything from inside an archive.
"""

import os
import shutil
import subprocess
import zipfile

NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def seven_zip():
    """7-Zip's command-line program, if installed (it handles every archive type)."""
    for p in (shutil.which("7z"), shutil.which("7za"),
              *(os.path.join(os.environ.get(v) or "", "7-Zip", "7z.exe") for v in ("ProgramFiles", "ProgramFiles(x86)"))):
        if p and os.path.isfile(p):
            return p
    return None


def kind(path):
    with open(path, "rb") as f:
        head = f.read(8)
    if head.startswith(b"PK"):
        return "zip"
    if head.startswith(b"7z\xbc\xaf\x27\x1c"):
        return "7z"
    if head.startswith(b"Rar!"):
        return "rar"
    return os.path.splitext(path)[1].lower().lstrip(".") or "unknown"


class ArchiveError(Exception):
    pass


def _safe(rel):
    rel = rel.replace("\\", "/").lstrip("/")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts) or (parts and ":" in parts[0]):
        return None
    return "/".join(parts)


def names(path):
    """Every file inside (forward slashes), without extracting."""
    k = kind(path)
    if k == "zip":
        with zipfile.ZipFile(path) as z:
            return [n.replace("\\", "/") for n in z.namelist() if not n.endswith("/")]
    if k == "7z":
        try:
            import py7zr
            with py7zr.SevenZipFile(path) as z:
                return [i.filename.replace("\\", "/") for i in z.list() if not i.is_directory]
        except ImportError:
            pass
    exe = seven_zip()
    if not exe:
        raise ArchiveError(f"{os.path.basename(path)} is a .{k} archive. Install 7-Zip (free, 7-zip.org) so Amalgam can open it"
                           + (", or press Install helpers in Apps → Game files." if k == "7z" else "."))
    r = subprocess.run([exe, "l", "-slt", "-ba", path], capture_output=True, creationflags=NO_WINDOW)
    out, cur = [], {}
    for line in r.stdout.decode("utf-8", "replace").splitlines() + [""]:
        if not line.strip():
            if cur.get("Path") and cur.get("Folder", "-") != "+" and "D" not in cur.get("Attributes", ""):
                out.append(cur["Path"].replace("\\", "/"))
            cur = {}
        elif " = " in line:
            k2, v = line.split(" = ", 1)
            cur[k2.strip()] = v
    return out


def read(path, member, limit=None):
    """Bytes of one file inside (the first `limit` bytes if given)."""
    k = kind(path)
    if k == "zip":
        with zipfile.ZipFile(path) as z:
            real = next((n for n in z.namelist() if n.replace("\\", "/").lower() == member.lower()), member)
            with z.open(real) as f:
                return f.read(limit) if limit else f.read()
    tmp = os.path.join(os.path.dirname(path), ".amalgam-peek")
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        extract(path, tmp, only=[member])
        found = os.path.join(tmp, *member.split("/"))
        if not os.path.isfile(found):
            low = member.lower()
            for dp, _d, fs in os.walk(tmp):
                for f in fs:
                    if os.path.relpath(os.path.join(dp, f), tmp).replace("\\", "/").lower() == low:
                        found = os.path.join(dp, f)
        with open(found, "rb") as f:
            return f.read(limit) if limit else f.read()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def extract(path, dest, only=None):
    """Extract everything (or just `only`) into dest. Paths that would escape dest are skipped."""
    os.makedirs(dest, exist_ok=True)
    want = {o.lower() for o in only} if only else None
    k = kind(path)
    if k == "zip":
        with zipfile.ZipFile(path) as z:
            for info in z.infolist():
                rel = _safe(info.filename)
                if not rel or info.is_dir() or (want is not None and rel.lower() not in want):
                    continue
                out = os.path.join(dest, *rel.split("/"))
                os.makedirs(os.path.dirname(out), exist_ok=True)
                with z.open(info) as src, open(out, "wb") as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)
        return
    if k == "7z":
        try:
            import py7zr
            with py7zr.SevenZipFile(path) as z:
                targets = None
                if want is not None:
                    targets = [n for n in z.getnames() if n.replace("\\", "/").lower() in want]
                z.extract(dest, targets=targets) if targets is not None else z.extractall(dest)
            _check_inside(dest)
            return
        except ImportError:
            pass
    exe = seven_zip()
    if not exe:
        raise ArchiveError(f"{os.path.basename(path)} is a .{k} archive. Install 7-Zip (free, 7-zip.org) so Amalgam can open it.")
    args = [exe, "x", "-y", f"-o{dest}", path] + (list(only) if only else [])
    r = subprocess.run(args, capture_output=True, creationflags=NO_WINDOW)
    if r.returncode not in (0, 1):
        raise ArchiveError(f"7-Zip couldn't extract {os.path.basename(path)}: {r.stderr.decode('utf-8', 'replace')[-200:]}")
    _check_inside(dest)


def _check_inside(dest):
    """After an outside extractor ran: remove anything that ended up as a link pointing elsewhere."""
    real = os.path.realpath(dest)
    for dp, ds, fs in os.walk(dest):
        for n in ds + fs:
            p = os.path.join(dp, n)
            if os.path.islink(p) and not os.path.realpath(p).startswith(real):
                os.remove(p)
