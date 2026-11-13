"""Every picture in a project (or in chats outside projects), in one place: images the models made,
images you attached, pictures the models showed in their messages, and textures pulled out of
game files. Each comes with the chat it's from and what was asked for it."""

import hashlib
import os
import re

IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")
MD_IMG_RE = re.compile(r"!\[([^\]]*)\]\(\s*<?([^)>]+?)>?\s*\)")
MAX_ITEMS = 600


def _abs(workspace, path):
    path = (path or "").strip().strip('"')
    if path.lower().startswith("file:///"):
        path = path[8:] if re.match(r"file:///[A-Za-z]:", path) else path[7:]
    return os.path.realpath(path if os.path.isabs(path) else os.path.join(workspace, path))


def collect(store, workspace, scope="all", allowed=None):
    """scope: a project id, "none" (chats outside projects) or "all". `allowed(path)` says whether the
    window may show a file (the app's own folders, game folders, the code folder)."""
    chats = store.chats()
    if scope == "none":
        chats = [c for c in chats if not c["project_id"]]
    elif scope != "all":
        chats = [c for c in chats if c["project_id"] == scope]
    items, seen = [], set()

    def add(path, chat, kind, by, caption, prompt, created):
        p = _abs(workspace, path)
        if p in seen or not p.lower().endswith(IMAGE_EXT) or not os.path.isfile(p):
            return
        if allowed and not allowed(p):
            return
        seen.add(p)
        items.append({"path": p, "chat_id": chat["id"], "chat_title": chat["title"], "kind": kind, "by": by,
                      "caption": (caption or "")[:300], "prompt": (prompt or "")[:600], "created": created,
                      "name": os.path.basename(p)})

    for chat in chats:
        last_user = ""
        for m in store.messages(chat["id"]):
            if m["kind"] == "user":
                last_user = re.sub(r"\n+📎 .*", "", m["text"]).strip()
            elif m["kind"] == "image":
                add(m["text"], chat, "generated", m["speaker"], "", last_user, m["created"])
            elif m["kind"] == "model":
                for cap, path in MD_IMG_RE.findall(m["text"]):
                    add(path, chat, "shown", m["speaker"], cap, last_user, m["created"])
        for a in store.attachments(chat["id"]):
            add(a["path"], chat, "attached", "You", a["name"], "", _mtime(_abs(workspace, a["path"])))
        ext_dir = os.path.join(workspace, "extracted", f"chat-{chat['id']}")
        for dirpath, _dirs, files in os.walk(ext_dir):
            for f in files:
                full = os.path.join(dirpath, f)
                add(full, chat, "extracted", "Reader", os.path.relpath(full, ext_dir), "", _mtime(full))
        if len(items) >= MAX_ITEMS:
            break
    items.sort(key=lambda x: x["created"] or 0, reverse=True)
    return items[:MAX_ITEMS]


def _mtime(p):
    try:
        return os.path.getmtime(p)
    except OSError:
        return 0


def thumbnail(path, cache_dir, size=360):
    """A small JPEG of the picture (made once, kept in cache_dir), or None without Pillow."""
    try:
        from PIL import Image
    except ImportError:
        return None
    st = os.stat(path)
    key = hashlib.sha1(f"{path}|{st.st_mtime_ns}|{st.st_size}|{size}".encode()).hexdigest()[:20]
    out = os.path.join(cache_dir, key + ".jpg")
    if os.path.isfile(out):
        return out
    os.makedirs(cache_dir, exist_ok=True)
    try:
        with Image.open(path) as im:
            im.thumbnail((size, size))
            if im.mode not in ("RGB", "L"):
                bg = Image.new("RGB", im.size, (36, 38, 42))
                im = im.convert("RGBA")
                bg.paste(im, mask=im.split()[-1])
                im = bg
            im.save(out, "JPEG", quality=82)
    except Exception as e:
        print("Thumbnail failed for", path, ":", e)
        return None
    return out
