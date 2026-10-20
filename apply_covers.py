#!/usr/bin/env python3
"""
Copy coverImageUrl (and optionally focusGifUrl) from a source layout JSON to
one or more target layout JSONs, matching folders by (parent_tab, title).
"""
import json
import sys
from pathlib import Path


def build_index(data):
    idx = {}
    for top in data:
        parent = top.get("title", "").strip()
        for folder in top.get("folders", []):
            title = (folder.get("title") or "").strip()
            if title:
                idx[(parent, title)] = folder
    return idx


def main():
    if len(sys.argv) < 3:
        sys.exit("usage: python apply_covers.py SOURCE.json TARGET.json [TARGET2.json ...]")

    source_path = Path(sys.argv[1])
    target_paths = [Path(p) for p in sys.argv[2:]]

    if not source_path.exists():
        sys.exit(f"source not found: {source_path}")

    source_data = json.loads(source_path.read_text(encoding="utf-8-sig"))
    source_index = build_index(source_data)

    for target_path in target_paths:
        if not target_path.exists():
            print(f"skip: {target_path} not found")
            continue

        target_data = json.loads(target_path.read_text(encoding="utf-8-sig"))
        target_index = build_index(target_data)

        changed = 0
        for key, src_folder in source_index.items():
            tgt_folder = target_index.get(key)
            if tgt_folder is None:
                continue
            for field in ("coverImageUrl", "focusGifUrl"):
                if field not in src_folder:
                    continue
                new_val = src_folder[field]
                if tgt_folder.get(field) != new_val:
                    tgt_folder[field] = new_val
                    changed += 1

        if changed:
            target_path.write_text(
                json.dumps(target_data, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            print(f"{target_path.name}: {changed} field(s) updated")
        else:
            print(f"{target_path.name}: nothing to change")

    print("done")


if __name__ == "__main__":
    main()