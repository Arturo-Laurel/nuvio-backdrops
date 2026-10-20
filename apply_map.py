#!/usr/bin/env python3
import json
import sys
from pathlib import Path

collections_path = Path(sys.argv[1])
map_path = Path(sys.argv[2])

data = json.loads(collections_path.read_text(encoding="utf-8-sig"))
mapping = json.loads(map_path.read_text(encoding="utf-8"))


def iter_folders(node, parent_key=None):
    if isinstance(node, dict):
        title = node.get("title")
        if isinstance(title, str) and title.strip():
            if "heroBackdropUrl" in node or "heroBackdropMode" in node or parent_key == "folders":
                yield node
        for k, v in node.items():
            yield from iter_folders(v, k)
    elif isinstance(node, list):
        for v in node:
            yield from iter_folders(v, parent_key)


changed = 0
for folder in iter_folders(data):
    entry = mapping.get(folder["title"].strip())
    if not entry:
        continue
    if folder.get("heroBackdropUrl") != entry["heroBackdropUrl"]:
        folder["heroBackdropUrl"] = entry["heroBackdropUrl"]
        changed += 1
    folder["heroBackdropMode"] = "static"

collections_path.write_text(
    json.dumps(data, indent=2, ensure_ascii=False) + "\n",
    encoding="utf-8",
)
print(f"updated {changed} folder(s) in {collections_path}")