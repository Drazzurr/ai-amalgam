"""Export a chat, or a project's decisions and chats, as Markdown."""

import json
import re
import time


def _when(t):
    return time.strftime("%b %d, %Y %I:%M %p", time.localtime(t)).replace(" 0", " ")


def _fence(text, cap=4000):
    text = text if len(text) <= cap else text[:cap // 2] + "\n[...]\n" + text[-cap // 2:]
    ticks = "````" if "```" in text else "```"
    return f"{ticks}\n{text}\n{ticks}"


def _quote(text):
    return "\n".join("> " + line if line.strip() else ">" for line in (text or "(nothing)").splitlines())


def message_md(m, shortcut_names=()):
    sp, kind, text = m["speaker"], m["kind"], m["text"]
    head = f"**{sp}** · {_when(m['created'])}"
    if kind == "system":
        return "> _" + text.replace("\n", " ") + "_"
    if kind == "question":
        lines = text.splitlines()
        q = "\n".join(l for l in lines if not l.startswith("CHOICE:"))
        choices = [l[7:].strip() for l in lines if l.startswith("CHOICE:")]
        return f"**{sp} asked you** · {_when(m['created'])}\n\n{q}" + ("\n\n" + "\n".join(f"- {c}" for c in choices) if choices else "")
    if kind == "command":
        return f"**{sp} asked to run** · {_when(m['created'])}\n\n" + _fence(text)
    if kind == "output":
        return _fence(text)
    if kind == "image":
        path = text.strip().replace("\\", "/")
        return f"{head}\n\n![Image by {sp}](<{path}>)"
    if kind == "edits":
        try:
            d = json.loads(text)
        except ValueError:
            return f"{head}\n\n{text}"
        parts = [f"**{sp} proposed document edits** · {_when(m['created'])}"]
        for e in d.get("edits", []):
            parts.append(f"**Edit {e.get('n')}**" + (f" — {e['where']}" if e.get("where") else ""))
            if e.get("find"):
                parts.append("Before:\n\n" + _quote(e["find"]))
            parts.append("After:\n\n" + _quote(e.get("replace") or "(removed)"))
        return "\n\n".join(parts)
    return f"{head}\n\n{text}"


def chat_md(store, chat_id, level=1):
    title = store.chat_title(chat_id) or "Chat"
    msgs = store.messages(chat_id)
    pid = store.chat_project(chat_id)
    project = store.project(pid) if pid else None
    h = "#" * level
    lines = [f"{h} {title}", ""]
    meta = []
    if project:
        meta.append(f"Project: {project['name']}")
    if msgs:
        meta.append(f"{_when(msgs[0]['created'])} – {_when(msgs[-1]['created'])}")
    meta.append(f"{sum(1 for m in msgs if m['kind'] != 'system')} messages")
    lines += ["_" + " · ".join(meta) + "_", ""]
    for m in msgs:
        lines += [message_md(m), ""]
    return title, "\n".join(lines).rstrip() + "\n"


def project_md(store, project_id, full=False):
    p = store.project(project_id)
    if not p:
        return None, None
    chats = [c for c in store.chats() if c["project_id"] == project_id]
    lines = [f"# {p['name']}", "", f"_Exported from AI Amalgam on {_when(time.time())}_", ""]
    if p.get("doc_link"):
        lines += [f"**Project document:** {p['doc_link']}", ""]
    if (p.get("instructions") or "").strip():
        lines += ["## Instructions", "", p["instructions"].strip(), ""]
    roles = {k: v for k, v in (p.get("roles") or {}).items() if str(v).strip()}
    if roles:
        lines += ["## Roles", ""] + [f"- **{'GPT' if k == 'gpt' else k.title()}:** {v}" for k, v in roles.items()] + [""]
    lines += ["## Decisions and facts (project memory)", ""]
    mem = [l.strip() for l in (p.get("memory") or "").splitlines() if l.strip()]
    lines += ([l if l.startswith("-") else f"- {l}" for l in mem] or ["_Nothing saved yet._"]) + [""]
    lines += ["## Chats", ""]
    if chats:
        lines += ["| Chat | Last active | Messages |", "|---|---|---|"]
        for c in chats:
            n = len([m for m in store.messages(c["id"]) if m["kind"] != "system"])
            lines.append(f"| {re.sub(r'[|]', '/', c['title'])} | {_when(c['updated'])} | {n} |")
    else:
        lines.append("_No chats yet._")
    lines.append("")
    if full:
        for c in chats:
            lines += ["---", "", chat_md(store, c["id"], level=2)[1], ""]
    return p["name"], "\n".join(lines).rstrip() + "\n"


def safe_filename(name):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " ", name or "export").strip().rstrip(". ")[:60] or "export"
