import json
import urllib.request

cfg = json.load(urllib.request.urlopen("http://127.0.0.1:7860/config", timeout=10))
by_id = {c["id"]: c for c in cfg["components"]}

def type_of(nid):
    c = by_id.get(nid)
    return c.get("type") if c else "?"

def prop(nid, key, default=None):
    c = by_id.get(nid)
    return c.get("props", {}).get(key, default) if c else default

def label_of(nid):
    lab = prop(nid, "label")
    if lab:
        return str(lab)[:40]
    v = prop(nid, "value")
    if isinstance(v, str) and v:
        return v[:40]
    return ""

lines = []
def dump(node, depth=0):
    nid = node.get("id")
    t = type_of(nid)
    vis = prop(nid, "visible")
    cls = prop(nid, "elem_classes") or ""
    lab = label_of(nid)
    lines.append("  " * depth + f"[{nid}] {t} vis={vis} cls={cls!r} lab={lab!r}")
    for ch in node.get("children", []):
        dump(ch, depth + 1)

root = cfg["layout"]
main_col = None
for ch in root["children"]:
    if ch.get("id") == 2:
        for sub in ch.get("children", []):
            if sub.get("id") != 3:
                main_col = sub
dump(main_col)
text = "\n".join(lines)
# print from the batch page onward: find line containing 台词文件
start = text.find("台词文件")
if start < 0:
    start = text.find("batch")
# print the batch page column subtree: find the vis=False column right after synth
import re

# find all top-level (depth-2, i.e. "  [id] column") page columns with their line numbers
segments = []
for m in re.finditer(r"^  \[\d+\] column vis=(True|False)", text, re.M):
    segments.append((m.start(), m.group(0)))
print("page columns found:", len(segments))
for i, (pos, head) in enumerate(segments):
    end = segments[i + 1][0] if i + 1 < len(segments) else len(text)
    seg = text[pos:end]
    print("----", head.strip())
    print(seg[:800])
