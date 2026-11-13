"""Proposed document edits, shown as a before/after diff you approve one by one.

The models write each proposed change as a block:

    EDIT 1
    DOC: <link, id or title>
    WHERE: <where in the document>
    FIND:
    <the exact current text>
    REPLACE WITH:
    <the new text>
    END EDIT

The app takes these blocks out of the message, works out a word-by-word diff, checks the FIND text
against the saved copy of the document (when there is one), and shows them as cards.
"""

import difflib
import json
import re

START_RE = re.compile(r"^\W*EDIT\s*#?\s*(\d+)\W*$")
END_RE = re.compile(r"^\W*END\s+EDIT\W*$")
FIELD_RE = re.compile(r"^\s*(DOC|WHERE|FIND|REPLACE WITH)\s*:\s?(.*)$")
DOC_ID_RE = re.compile(r"(?:/d/|^)([A-Za-z0-9_-]{25,})")
MAX_EDITS = 30

FORMAT_HELP = """Write each proposed change as its own block, exactly like this (the app turns these into a before/after view with an approve button for each, so don't repeat the text elsewhere):
EDIT 1
DOC: <document link or title>
WHERE: <where in the document, e.g. "Chapter 2, second paragraph">
FIND:
<the exact current text, copied from the document, enough to be unique>
REPLACE WITH:
<the new text>
END EDIT
Number them 1, 2, 3... For new text with nothing to replace, leave FIND empty and say in WHERE exactly where it goes. To delete text, leave REPLACE WITH empty."""


def parse(text):
    """Take EDIT blocks out of a message. Returns (the rest of the message, [edits])."""
    keep, edits, cur, field, after_block = [], [], None, None, False
    for line in text.splitlines():
        if cur is None:
            m = START_RE.match(line)
            if m and len(edits) < MAX_EDITS:
                cur, field = {"n": int(m.group(1)), "doc": "", "where": "", "find": [], "replace": []}, None
                while keep and not keep[-1].strip():
                    keep.pop()
                if keep and keep[-1].strip().startswith("```"):    # the block was wrapped in a code fence
                    keep.pop()
                continue
            if after_block and line.strip().startswith("```"):     # ...and this closes it
                after_block = False
                continue
            if line.strip():
                after_block = False
            keep.append(line)
            continue
        if END_RE.match(line):
            edits.append(cur)
            cur, field, after_block = None, None, True
            continue
        f = FIELD_RE.match(line)
        if f:
            name, rest = f.group(1), f.group(2)
            if name in ("DOC", "WHERE"):
                cur[name.lower()] = rest.strip()
                field = None
            else:
                field = "find" if name == "FIND" else "replace"
                if rest.strip():
                    cur[field].append(rest)
            continue
        if field in ("find", "replace"):
            cur[field].append(line)
    if cur is not None:          # an unfinished block: put it back as ordinary text
        keep.append(f"EDIT {cur['n']} (unfinished)")
    for e in edits:
        e["find"] = "\n".join(e["find"]).strip("\n")
        e["replace"] = "\n".join(e["replace"]).strip("\n")
    return re.sub(r"\n{3,}", "\n\n", "\n".join(keep)).strip(), edits


def word_diff(old, new):
    """[[op, text], ...] with op = eq | del | ins, word by word."""
    tok = lambda s: re.findall(r"\s+|\w+|[^\w\s]", s)
    a, b = tok(old), tok(new)
    ops = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            ops.append(["eq", "".join(a[i1:i2])])
        else:
            if i2 > i1:
                ops.append(["del", "".join(a[i1:i2])])
            if j2 > j1:
                ops.append(["ins", "".join(b[j1:j2])])
    return ops


def _norm(s):
    return re.sub(r"\s+", " ", s or "").strip()


def enrich(edits, default_doc_link, doc_text_for):
    """Add the diff, the document id, and whether FIND is in the saved copy (True/False, or None
    when there's no copy to check). doc_text_for(doc_id) returns the saved text or None."""
    out = []
    for e in edits:
        doc_ref = e["doc"] or default_doc_link or ""
        m = DOC_ID_RE.search(doc_ref) or DOC_ID_RE.search(default_doc_link or "")
        doc_id = m.group(1) if m else ""
        found = None
        if e["find"] and doc_id:
            copy = doc_text_for(doc_id)
            if copy is not None:
                found = _norm(e["find"]) in _norm(copy)
        out.append(dict(e, doc_id=doc_id, found=found, diff=word_diff(e["find"], e["replace"])))
    return out


def to_message(by, edits):
    return json.dumps({"by": by, "edits": edits}, ensure_ascii=False)


def for_transcript(text):
    """How the models see an edits card (exact text kept, so the approved ones can be applied as written)."""
    try:
        d = json.loads(text)
    except ValueError:
        return text
    parts = []
    for e in d.get("edits", []):
        note = " (the app could NOT find this FIND text in the saved copy of the document)" if e.get("found") is False else ""
        parts.append(f"EDIT {e['n']}{note}\nDOC: {e.get('doc', '')}\nWHERE: {e.get('where', '')}\nFIND:\n{e.get('find', '')}\n"
                     f"REPLACE WITH:\n{e.get('replace', '')}\nEND EDIT")
    return "\n\n".join(parts)
