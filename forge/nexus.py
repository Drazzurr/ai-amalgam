"""Talk to Nexus Mods with the user's own personal API key (Nexus allows that for personal use).

Downloads through the API need a Premium account. The key is kept on this PC only (never synced) and is
sent only to api.nexusmods.com. Nothing else about the account is stored: just the name and Premium status
that the key check returns, so the window can show who is connected.
"""

import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

API = os.environ.get("AMALGAM_NEXUS_API", "https://api.nexusmods.com")   # tests point this at a stand-in
GAME_DOMAINS = {"Skyrim Special Edition": "skyrimspecialedition", "Skyrim": "skyrim", "Skyrim VR": "skyrimspecialedition",
                "Baldur's Gate 3": "baldursgate3", "Cyberpunk 2077": "cyberpunk2077", "The Witcher 3": "witcher3",
                "Mass Effect Legendary Edition": "masseffectlegendaryedition"}
FILE_CATEGORIES = {1: "MAIN", 2: "UPDATE", 3: "OPTIONAL", 4: "OLD_VERSION", 5: "MISCELLANEOUS", 6: "DELETED", 7: "ARCHIVED"}


class NexusError(Exception):
    def __init__(self, message, status=0):
        super().__init__(message)
        self.status = status


def mod_url(domain, mod_id):
    return f"https://www.nexusmods.com/{domain}/mods/{mod_id}"


class Nexus:
    def __init__(self, key, app_version="1.0", timeout=30):
        self.key = (key or "").strip()
        self.version = app_version
        self.timeout = timeout
        self.limits = {}          # hourly/daily requests left, from the last reply

    # ----- plumbing -----

    def _headers(self):
        return {"apikey": self.key, "Application-Name": "AI Amalgam", "Application-Version": self.version,
                "User-Agent": f"AI-Amalgam/{self.version}", "Accept": "application/json"}

    def _call(self, path, body=None):
        if not self.key:
            raise NexusError("No Nexus API key yet. Add yours in the Mod Forge (Nexus account).", 401)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(API + path, data=data, headers=dict(self._headers(), **(
            {"Content-Type": "application/json"} if data else {})))
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                self._note_limits(r.headers)
                return json.loads(r.read().decode("utf-8", "replace") or "null")
        except urllib.error.HTTPError as e:
            self._note_limits(e.headers or {})
            try:
                msg = json.loads(e.read().decode("utf-8", "replace")).get("message", "")
            except Exception:
                msg = ""
            if e.code == 401:
                msg = "Nexus didn't accept the API key. Copy it again from your Nexus account (Site preferences → API keys)."
            elif e.code == 403 and "premium" in msg.lower():
                msg = "Nexus says direct downloads need Premium on this account."
            elif e.code == 429:
                msg = "Nexus's request limit is used up for now (it resets each hour). Try again a bit later."
            elif e.code == 404:
                msg = msg or "Nexus couldn't find that mod or file (it may be hidden or deleted)."
            raise NexusError(msg or f"Nexus answered with error {e.code}.", e.code) from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise NexusError(f"Couldn't reach Nexus Mods: {getattr(e, 'reason', e)}") from None

    def _note_limits(self, headers):
        for k in ("x-rl-hourly-remaining", "x-rl-daily-remaining"):
            v = headers.get(k) if hasattr(headers, "get") else None
            if v is not None:
                try:
                    self.limits[k.split("-")[2]] = int(v)
                except ValueError:
                    pass

    def graphql(self, query, variables=None):
        r = self._call("/v2/graphql", {"query": query, "variables": variables or {}})
        if not isinstance(r, dict):
            raise NexusError("Nexus search sent back something unexpected.")
        if r.get("errors") and not r.get("data"):
            raise NexusError("Nexus search: " + "; ".join(str(e.get("message", e)) for e in r["errors"][:2]))
        return r.get("data") or {}

    # ----- account -----

    def validate(self):
        """{name, premium}: only what the window shows. The rest of Nexus's answer is dropped."""
        r = self._call("/v1/users/validate.json") or {}
        return {"name": str(r.get("name") or ""), "premium": bool(r.get("is_premium"))}

    # ----- mods -----

    def search(self, domain, words, count=10, adult=False):
        """Mods whose names match, best-endorsed first."""
        words = " ".join(str(words).split())[:120]
        flt = {"gameDomainName": [{"value": domain, "op": "EQUALS"}], "nameStemmed": [{"value": words, "op": "MATCHES"}]}
        if not adult:
            flt["adultContent"] = [{"value": False, "op": "EQUALS"}]
        q = """query Search($filter: ModsFilter, $sort: [ModsSort!], $count: Int) {
  mods(filter: $filter, sort: $sort, count: $count) { totalCount nodes {
    modId name summary version author endorsements downloads updatedAt adultContent } } }"""
        try:
            data = self.graphql(q, {"filter": flt, "sort": [{"endorsements": {"direction": "DESC"}}], "count": count})
        except NexusError:
            flt.pop("adultContent", None)       # older schema: filter by hand below
            data = self.graphql(q, {"filter": flt, "sort": [{"endorsements": {"direction": "DESC"}}], "count": count})
        nodes = ((data.get("mods") or {}).get("nodes")) or []
        out = []
        for n in nodes:
            if n.get("adultContent") and not adult:
                continue
            out.append({"mod_id": int(n.get("modId") or 0), "name": n.get("name") or "", "summary": (n.get("summary") or "")[:220],
                        "version": n.get("version") or "", "author": n.get("author") or "",
                        "endorsements": n.get("endorsements") or 0, "downloads": n.get("downloads") or 0,
                        "updated": str(n.get("updatedAt") or "")[:10]})
        return out

    def mod(self, domain, mod_id):
        r = self._call(f"/v1/games/{domain}/mods/{int(mod_id)}.json") or {}
        return {"mod_id": int(mod_id), "name": r.get("name") or f"Mod {mod_id}", "summary": r.get("summary") or "",
                "description": r.get("description") or "", "version": r.get("version") or "",
                "author": r.get("author") or r.get("uploaded_by") or "", "available": r.get("available", True),
                "status": r.get("status") or "", "adult": bool(r.get("contains_adult_content")),
                "endorsements": r.get("endorsement_count") or 0, "downloads": r.get("mod_downloads") or 0,
                "updated": time.strftime("%Y-%m-%d", time.gmtime(r["updated_timestamp"])) if r.get("updated_timestamp") else ""}

    def requirements(self, domain, mod_id):
        """Mods the page lists as requirements (best effort: Nexus's newer API; [] if it can't say)."""
        q = """query Req($ids: [CompositeDomainWithIdInput!]!) { legacyModsByDomain(ids: $ids) { nodes {
  modRequirements { nexusRequirements { nodes { modId modName notes externalRequirement url } } } } } }"""
        try:
            data = self.graphql(q, {"ids": [{"gameDomain": domain, "modId": int(mod_id)}]})
            nodes = ((data.get("legacyModsByDomain") or {}).get("nodes")) or []
            reqs = (((nodes[0].get("modRequirements") or {}).get("nexusRequirements") or {}).get("nodes")) if nodes else []
        except (NexusError, AttributeError, IndexError, TypeError):
            return []
        out = []
        for r in reqs or []:
            out.append({"mod_id": int(r.get("modId") or 0), "name": r.get("modName") or "", "notes": (r.get("notes") or "")[:160],
                        "external": bool(r.get("externalRequirement")), "url": r.get("url") or ""})
        return out

    def files(self, domain, mod_id):
        r = self._call(f"/v1/games/{domain}/mods/{int(mod_id)}/files.json") or {}
        out = []
        for f in r.get("files") or []:
            cat = f.get("category_name") or FILE_CATEGORIES.get(f.get("category_id"), "")
            out.append({"file_id": int(f.get("file_id") or (f.get("id") or [0])[0] or 0), "name": f.get("name") or "",
                        "version": f.get("version") or f.get("mod_version") or "", "category": str(cat).upper(),
                        "primary": bool(f.get("is_primary")), "size": int(f.get("size_in_bytes") or (f.get("size_kb") or 0) * 1024),
                        "file_name": f.get("file_name") or "", "uploaded": int(f.get("uploaded_timestamp") or 0),
                        "description": (f.get("description") or "")[:200]})
        return out

    @staticmethod
    def pick_main(files):
        """The file most people mean by 'download this mod': the primary main file, else the newest main file."""
        live = [f for f in files if f["category"] not in ("OLD_VERSION", "DELETED", "ARCHIVED")]
        main = [f for f in live if f["category"] == "MAIN"] or live
        if not main:
            return None
        prim = [f for f in main if f["primary"]]
        return (prim or sorted(main, key=lambda f: f["uploaded"]))[-1]

    def download_link(self, domain, mod_id, file_id):
        r = self._call(f"/v1/games/{domain}/mods/{int(mod_id)}/files/{int(file_id)}/download_link.json")
        links = [x.get("URI") or x.get("uri") for x in (r or []) if isinstance(x, dict)]
        links = [l for l in links if l]
        if not links:
            raise NexusError("Nexus didn't give a download link for that file.")
        return links[0]

    def download(self, domain, mod_id, file_id, dest_dir, file_name="", size=0, progress=None, stop=None):
        """Download one file into dest_dir (kept, so a rebuild doesn't download again). Returns the path."""
        os.makedirs(dest_dir, exist_ok=True)
        clean = lambda n: "".join(c for c in urllib.parse.unquote(n) if c not in '<>:"/\\|?*').strip()
        if file_name:      # already downloaded for an earlier build: no need to ask Nexus again
            cached = os.path.join(dest_dir, clean(file_name))
            if clean(file_name) and os.path.isfile(cached) and (not size or os.path.getsize(cached) == size):
                return cached
        url = self.download_link(domain, mod_id, file_id)
        name = clean(file_name or os.path.basename(urllib.parse.urlparse(url).path)) or f"{mod_id}-{file_id}"
        final = os.path.join(dest_dir, name)
        if os.path.isfile(final) and (not size or os.path.getsize(final) == size):
            return final
        part = final + ".part"
        req = urllib.request.Request(url, headers={"User-Agent": f"AI-Amalgam/{self.version}"})
        got, ok = 0, False
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r, open(part, "wb") as out:
                total = int(r.headers.get("Content-Length") or size or 0)
                while True:
                    if stop and stop():
                        raise NexusError("Stopped.")
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    out.write(chunk)
                    got += len(chunk)
                    if progress:
                        progress(got, total)
            ok = True
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise NexusError(f"The download of {name} failed: {getattr(e, 'reason', e)}") from None
        finally:
            if not ok:
                try:
                    os.remove(part)
                except OSError:
                    pass
        if size and got != size:
            os.remove(part)
            raise NexusError(f"{name} came down incomplete ({got} of {size} bytes). Try the build again.")
        os.replace(part, final)
        return final


def md5_file(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
