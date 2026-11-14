"""ComfyUI, run in the background for the Artisan: a small client for its local web API, the workflows it
runs (FLUX.1 dev for pictures, the official TRELLIS.2 template for 3D), and a converter that turns a saved
ComfyUI workflow (the format the ComfyUI window saves, subgraphs included) into the format its API runs.

Everything talks to 127.0.0.1 only. Nothing here sends anything anywhere else.
"""

import copy
import json
import mimetypes
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

FLUX_FILE = "flux1-dev-fp8.safetensors"
FLUX_URL = "https://huggingface.co/Comfy-Org/flux1-dev/resolve/main/flux1-dev-fp8.safetensors"
TRELLIS_TEMPLATE = "3d_pixal3d_trellis2_image_to_model"
WIDGET_TYPES = {"INT", "FLOAT", "STRING", "BOOLEAN", "COMBO"}
SKIP_NODES = {"Note", "MarkdownNote", "PrimitiveNode", "Reroute", "GetNode", "SetNode"}


class ComfyError(Exception):
    pass


class Comfy:
    def __init__(self, url, timeout=30):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.client_id = uuid.uuid4().hex
        self._info = None

    # ----- plumbing -----

    def _req(self, path, body=None, raw=False, timeout=None, method=None, headers=None):
        data = json.dumps(body).encode() if isinstance(body, (dict, list)) else body
        h = {"Content-Type": "application/json"} if isinstance(body, (dict, list)) else {}
        h.update(headers or {})
        req = urllib.request.Request(self.url + path, data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                out = r.read()
                return out if raw else json.loads(out.decode("utf-8", "replace") or "null")
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            try:
                info = json.loads(text)
            except ValueError:
                info = {"error": text[:300]}
            raise ComfyError(_explain(info, e.code)) from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ComfyError(f"ComfyUI isn't answering ({getattr(e, 'reason', e)}).") from None

    def alive(self):
        try:
            self._req("/system_stats", timeout=3)
            return True
        except ComfyError:
            return False

    def system(self):
        return self._req("/system_stats", timeout=5)

    def object_info(self, refresh=False):
        if self._info is None or refresh:
            self._info = self._req("/object_info", timeout=60)
        return self._info

    def choices(self, node, field):
        """The files ComfyUI offers for a loader's file box (e.g. checkpoints)."""
        spec = self.object_info().get(node, {}).get("input", {})
        for part in ("required", "optional"):
            if field in spec.get(part, {}):
                v = spec[part][field]
                if isinstance(v, list) and v and isinstance(v[0], list):
                    return v[0]
                if isinstance(v, list) and len(v) > 1 and isinstance(v[1], dict) and isinstance(v[1].get("options"), list):
                    return v[1]["options"]
        return []

    def upload_image(self, path):
        """Put an image in ComfyUI's input folder. Returns the name to use in a Load Image node."""
        boundary = uuid.uuid4().hex
        name = f"amalgam_{uuid.uuid4().hex[:8]}{os.path.splitext(path)[1].lower() or '.png'}"
        with open(path, "rb") as f:
            data = f.read()
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{name}\"\r\n"
                f"Content-Type: {ctype}\r\n\r\n").encode() + data + \
               f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"overwrite\"\r\n\r\ntrue\r\n--{boundary}--\r\n".encode()
        r = self._req("/upload/image", body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        return (r.get("subfolder") + "/" if r.get("subfolder") else "") + r.get("name", name)

    def run(self, prompt, timeout=1800, stop=None, progress=None):
        """Queue an API-format workflow and wait for it. Returns its outputs."""
        r = self._req("/prompt", {"prompt": prompt, "client_id": self.client_id})
        if r.get("node_errors"):
            raise ComfyError(_explain(r, 400))
        pid = r["prompt_id"]
        t0 = time.time()
        while time.time() - t0 < timeout:
            if stop and stop():
                try:
                    self._req("/interrupt", {})
                except ComfyError:
                    pass
                raise ComfyError("Stopped.")
            h = self._req(f"/history/{pid}", timeout=15) or {}
            item = h.get(pid)
            if item:
                st = item.get("status") or {}
                if st.get("status_str") == "error" or (st.get("completed") is False and st.get("messages") and
                                                       any(m[0] == "execution_error" for m in st["messages"])):
                    msg = next((m[1] for m in st.get("messages", []) if m[0] == "execution_error"), {})
                    raise ComfyError(f"ComfyUI stopped with an error in {msg.get('node_type', 'a step')}: "
                                     f"{str(msg.get('exception_message', '')).strip()[:300]}")
                if st.get("completed", True):
                    return item.get("outputs") or {}
            if progress:
                progress(time.time() - t0)
            time.sleep(1.0)
        raise ComfyError("ComfyUI took too long.")

    def fetch_outputs(self, outputs, dest_dir, prefix):
        """Copy every file a run produced into dest_dir. Returns their paths."""
        os.makedirs(dest_dir, exist_ok=True)
        got = []
        for node_out in outputs.values():
            for key, items in (node_out or {}).items():
                if not isinstance(items, list):
                    continue
                for it in items:
                    if not isinstance(it, dict) or not it.get("filename") or it.get("type") == "temp" and key == "images" and len(outputs) > 1:
                        continue
                    q = urllib.parse.urlencode({"filename": it["filename"], "subfolder": it.get("subfolder", ""),
                                                "type": it.get("type", "output")})
                    data = self._req(f"/view?{q}", raw=True, timeout=120)
                    ext = os.path.splitext(it["filename"])[1] or ".bin"
                    out = os.path.join(dest_dir, f"{prefix}-{len(got) + 1}{ext}")
                    with open(out, "wb") as f:
                        f.write(data)
                    got.append(out)
        return got

    def free(self):
        """Unload models so the graphics card is free for games."""
        try:
            self._req("/free", {"unload_models": True, "free_memory": True}, timeout=20)
        except ComfyError:
            pass

    def template(self, name):
        """A built-in workflow template, as the ComfyUI window saves it."""
        return self._req(f"/templates/{urllib.parse.quote(name)}.json", timeout=30)


def _explain(info, code):
    """Plain words for ComfyUI's error replies (which file or setting a step is missing)."""
    if not isinstance(info, dict):
        return f"ComfyUI answered with error {code}."
    errs = info.get("node_errors") or {}
    bits = []
    for _nid, ne in list(errs.items())[:4]:
        for e in (ne.get("errors") or [])[:2]:
            det = str(e.get("details") or e.get("message") or "")
            if "not in" in det and "safetensors" in det or "value_not_in_list" in str(e.get("type")):
                bits.append(f"{ne.get('class_type', 'a step')} can't find its model file ({det[:160]})")
            else:
                bits.append(f"{ne.get('class_type', 'a step')}: {det[:160] or e.get('message', '')}")
    if bits:
        return "ComfyUI can't run this yet: " + "; ".join(bits)
    e = info.get("error")
    if isinstance(e, dict):
        return f"ComfyUI: {e.get('message', '')} {e.get('details', '')}".strip()
    return f"ComfyUI: {e or 'error ' + str(code)}"


# ---------------- FLUX ----------------

def flux_prompt(ckpt, text, width=1024, height=1024, seed=None, steps=20, guidance=3.5, image_name=None, strength=0.6,
                prefix="amalgam"):
    """FLUX.1 dev from the all-in-one checkpoint: text to image, or image to image when image_name is given."""
    seed = random.randint(1, 2**48) if seed is None else int(seed)
    p = {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": text, "clip": ["1", 1]}},
        "3": {"class_type": "FluxGuidance", "inputs": {"conditioning": ["2", 0], "guidance": float(guidance)}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["1", 1]}},
        "5": {"class_type": "EmptySD3LatentImage", "inputs": {"width": int(width), "height": int(height), "batch_size": 1}},
        "6": {"class_type": "KSampler", "inputs": {"model": ["1", 0], "seed": seed, "steps": int(steps), "cfg": 1.0,
                                                   "sampler_name": "euler", "scheduler": "simple", "positive": ["3", 0],
                                                   "negative": ["4", 0], "latent_image": ["5", 0], "denoise": 1.0}},
        "7": {"class_type": "VAEDecode", "inputs": {"samples": ["6", 0], "vae": ["1", 2]}},
        "8": {"class_type": "SaveImage", "inputs": {"images": ["7", 0], "filename_prefix": prefix}},
    }
    if image_name:
        p["9"] = {"class_type": "LoadImage", "inputs": {"image": image_name}}
        p["10"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["9", 0], "vae": ["1", 2]}}
        p["6"]["inputs"]["latent_image"] = ["10", 0]
        p["6"]["inputs"]["denoise"] = max(0.05, min(1.0, float(strength)))
        del p["5"]
    return p, seed


# ---------------- saved workflow -> API format ----------------

def _widget_names(spec):
    """Names of an object's inputs that show as widgets, in widgets_values order, with extra slots
    (the 'control after generate' box, the upload button)."""
    names = []
    for part in ("required", "optional"):
        for name, v in (spec.get("input", {}).get(part) or {}).items():
            t = v[0] if isinstance(v, (list, tuple)) and v else v
            opts = v[1] if isinstance(v, (list, tuple)) and len(v) > 1 and isinstance(v[1], dict) else {}
            if isinstance(t, list) or t in WIDGET_TYPES or (isinstance(t, str) and t.startswith("COMFY_") and "COMBO" in t):
                if opts.get("forceInput"):
                    continue
                names.append(name)
                if opts.get("control_after_generate") or (name in ("seed", "noise_seed") and t == "INT"):
                    names.append(None)
                if opts.get("image_upload") or opts.get("upload"):
                    names.append(None)
    return names


def _flatten(wf):
    """Expand subgraphs into one flat list of nodes and links: (nodes by id, links by id)."""
    subs = {s["id"]: s for s in (wf.get("definitions") or {}).get("subgraphs") or []}
    nodes, links = {}, {}

    def norm_link(l):
        if isinstance(l, dict):
            return [l["id"], l["origin_id"], l["origin_slot"], l["target_id"], l["target_slot"], l.get("type")]
        return list(l)

    def add(graph_nodes, graph_links, prefix, depth=0):
        local_links = {}
        for l in graph_links:
            l = norm_link(l)
            local_links[l[0]] = l
        id_map = {}
        expanded = {}     # instance node id -> (sub definition, inner prefix)
        for n in graph_nodes:
            nid = f"{prefix}{n['id']}"
            id_map[n["id"]] = nid
            if n.get("type") in subs and depth < 6:
                expanded[n["id"]] = (subs[n["type"]], f"{nid}:")
            else:
                nn = copy.deepcopy(n)
                nn["id"] = nid
                nodes[nid] = nn
        # links of this level
        for lid, (i, o, os_, t, ts, typ) in local_links.items():
            links[f"{prefix}{lid}"] = [f"{prefix}{lid}", id_map.get(o, f"{prefix}{o}"), os_, id_map.get(t, f"{prefix}{t}"), ts, typ]
        for inst_id, (sg, inner) in expanded.items():
            add(sg.get("nodes") or [], sg.get("links") or [], inner, depth + 1)
        # outputs first (so a subgraph feeding another subgraph is already resolved), then inputs
        for inst_id, (sg, inner) in expanded.items():
            inst_nid = id_map[inst_id]
            for slot, sout in enumerate(sg.get("outputs") or []):
                origin = None
                for ilid in sout.get("linkIds") or []:
                    il = links.pop(f"{inner}{ilid}", None)
                    if il:
                        origin = (il[1], il[2])
                if origin:
                    for k, l in list(links.items()):
                        if l[1] == inst_nid and l[2] == slot:
                            links[k] = [k, origin[0], origin[1], l[3], l[4], l[5]]
        for inst_id, (sg, inner) in expanded.items():
            inst = next(n for n in graph_nodes if n["id"] == inst_id)
            for slot, sin in enumerate(sg.get("inputs") or []):
                inst_in = inst.get("inputs") or []
                outer = links.get(f"{prefix}{inst_in[slot]['link']}") if slot < len(inst_in) and \
                    inst_in[slot].get("link") is not None else None
                for ilid in sin.get("linkIds") or []:
                    key = f"{inner}{ilid}"
                    il = links.get(key)
                    if not il:
                        continue
                    if outer:
                        links[key] = [key, outer[1], outer[2], il[3], il[4], il[5]]
                    else:
                        links.pop(key, None)
                        # a promoted widget: the instance holds the value; give it to the inner widget
                        wv = inst.get("widgets_values")
                        if isinstance(wv, list) and slot < len(wv):
                            tgt = nodes.get(il[3])
                            if tgt is not None:
                                tgt.setdefault("_set", {})[il[4]] = wv[slot]
            in_node = (sg.get("inputNode") or {}).get("id", -10)
            out_node = (sg.get("outputNode") or {}).get("id", -20)
            dead = (f"{inner}{in_node}", f"{inner}{out_node}")
            for k, l in list(links.items()):
                if l[1] in dead or l[3] in dead:
                    links.pop(k, None)

    add(wf.get("nodes") or [], wf.get("links") or [], "")
    return nodes, links


def to_api(wf, object_info):
    """Turn a workflow saved by the ComfyUI window into the API format. Returns {id: {class_type, inputs}}."""
    nodes, links = _flatten(wf)

    def link_of(nid, lid):
        pre = nid[:nid.rfind(":") + 1] if ":" in nid else ""
        return links.get(f"{pre}{lid}")

    def source(nid, slot_in):
        """Follow a node input back through reroutes and bypassed nodes to its real origin."""
        n = nodes.get(nid)
        ins = (n.get("inputs") or []) if n else []
        if slot_in >= len(ins) or ins[slot_in].get("link") is None:
            return None
        l = link_of(nid, ins[slot_in]["link"])
        for _ in range(50):
            if not l:
                return None
            o = nodes.get(l[1])
            if o is None:
                return None
            if o.get("type") == "Reroute" or o.get("mode") == 4:
                oins = o.get("inputs") or []
                pick = next((x for x in oins if x.get("link") is not None and
                             (o.get("type") == "Reroute" or x.get("type") == l[5])), None)
                if pick is None:
                    return None
                l = link_of(o["id"], pick["link"])
                continue
            if o.get("type") == "PrimitiveNode":
                return ("value", (o.get("widgets_values") or [None])[0])
            return [o["id"], l[2]]
        return None

    api = {}
    for nid, n in nodes.items():
        t = n.get("type")
        if t in SKIP_NODES or n.get("mode") in (2, 4) or t not in object_info:
            continue
        spec = object_info[t]
        inputs = {}
        wv = n.get("widgets_values")
        if isinstance(wv, dict):
            for k, v in wv.items():
                if k in _all_inputs(spec):
                    inputs[k] = v
        elif isinstance(wv, list):
            for name, val in zip(_widget_names(spec), wv):
                if name:
                    inputs[name] = val
        for slot, inp in enumerate(n.get("inputs") or []):
            if inp.get("link") is None:
                continue
            src = source(nid, slot)
            name = inp.get("name") or (inp.get("widget") or {}).get("name")
            if src is None or not name:
                continue
            inputs[name] = src[1] if isinstance(src, tuple) else src
        for slot, val in (n.get("_set") or {}).items():
            ins = n.get("inputs") or []
            if slot < len(ins):
                inputs[ins[slot].get("name") or (ins[slot].get("widget") or {}).get("name")] = val
        api[nid] = {"class_type": t, "inputs": inputs, "_title": n.get("title", "")}
    return api


def _all_inputs(spec):
    out = set()
    for part in ("required", "optional"):
        out |= set((spec.get("input", {}).get(part) or {}).keys())
    return out


def ancestors(api, nid):
    seen, todo = set(), [nid]
    while todo:
        cur = todo.pop()
        if cur in seen or cur not in api:
            continue
        seen.add(cur)
        for v in api[cur]["inputs"].values():
            if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str):
                todo.append(v[0])
    return seen


def trellis_prompt(template, object_info, image_name, prefix="amalgam_3d"):
    """The TRELLIS.2 half of the official 3D template, with your picture. Returns the API workflow."""
    api = to_api(template, object_info)
    outs = [k for k, v in api.items() if object_info.get(v["class_type"], {}).get("output_node")]
    want = None
    for k in outs:
        anc = ancestors(api, k)
        kinds = {api[a]["class_type"] for a in anc}
        if any(c.startswith("Trellis2Conditioning") for c in kinds) and not any(c.startswith("Pixal3D") for c in kinds) \
                and "3d" in api[k]["class_type"].lower():
            want = k
            break
    if want is None:
        raise ComfyError("The 3D template has no TRELLIS.2 save step Amalgam recognises. Update ComfyUI, or export your "
                         "own TRELLIS.2 workflow (File → Export (API)) and pick it in Apps → Artisan.")
    keep = ancestors(api, want)
    # also keep a preview/save image fed by the same branch, if any (a quick look at the result)
    for k in outs:
        if k != want and "image" in api[k]["class_type"].lower() and ancestors(api, k) <= keep | {k}:
            keep |= {k}
    sub = {k: {"class_type": v["class_type"], "inputs": v["inputs"]} for k, v in api.items() if k in keep}
    loads = [k for k, v in sub.items() if v["class_type"] in ("LoadImage", "LoadImageMask", "LoadImageOutput")]
    if not loads:
        raise ComfyError("The 3D template has no Load Image step.")
    for k in loads:
        sub[k]["inputs"]["image"] = image_name
    for v in sub.values():
        if "filename_prefix" in v["inputs"]:
            v["inputs"]["filename_prefix"] = prefix
        for s in ("seed", "noise_seed"):
            if s in v["inputs"] and isinstance(v["inputs"][s], int):
                v["inputs"][s] = random.randint(1, 2**48)
    return sub


def api_with_image(api_wf, image_name, prefix="amalgam_3d"):
    """Your own exported API workflow: put the picture into its Load Image step."""
    sub = copy.deepcopy(api_wf)
    loads = [k for k, v in sub.items() if isinstance(v, dict) and v.get("class_type") in ("LoadImage", "LoadImageOutput")]
    if not loads:
        raise ComfyError("That workflow has no Load Image step to put the picture into.")
    for k in loads:
        sub[k]["inputs"]["image"] = image_name
    for v in sub.values():
        if isinstance(v, dict) and "filename_prefix" in v.get("inputs", {}):
            v["inputs"]["filename_prefix"] = prefix
    return sub


def template_models(template):
    """Model files a template asks for (name, url, folder), from its loaders' properties."""
    out = []

    def scan(nodes):
        for n in nodes or []:
            for m in ((n.get("properties") or {}).get("models") or []):
                if m.get("url") and m.get("name"):
                    out.append({"name": m["name"], "url": m["url"], "directory": m.get("directory") or ""})
    scan(template.get("nodes"))
    for sg in (template.get("definitions") or {}).get("subgraphs") or []:
        scan(sg.get("nodes"))
    seen, uniq = set(), []
    for m in out:
        if (m["directory"], m["name"]) not in seen:
            seen.add((m["directory"], m["name"]))
            uniq.append(m)
    return uniq
