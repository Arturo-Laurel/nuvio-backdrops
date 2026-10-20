#!/usr/bin/env python3
"""
Reads Nuvio collections.json files, for each folder:
  1. Fetches the folder's actual catalog contents via MDBList API
  2. Pulls one TMDB backdrop per title (up to 6 titles)
  3. Writes an animated WebP carousel
Falls back to TMDB search when a folder has no MDBList sources.

Never rewrites your input JSON. Apply the emitted map locally with
apply_map.py so the real file (with addon tokens) never leaves your machine.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
from io import BytesIO
from pathlib import Path

import requests
from PIL import Image, ImageOps

TMDB_API = "https://api.themoviedb.org/3"
TMDB_IMG = "https://image.tmdb.org/t/p/w1280"
MDBLIST_API = "https://api.mdblist.com"

SIZE = (1280, 720)
FRAME_MS = 5000
MAX_FRAMES = 6
QUALITY = 78
METHOD = 4
FOLDER_KEYS = {"folders"}

SESSION = requests.Session()


def log(msg: str) -> None:
    print(msg, flush=True)


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text or "folder"


def tmdb_get(api_key: str, path: str, **params):
    params["api_key"] = api_key
    url = TMDB_API + path
    last = None
    for attempt in range(6):
        r = SESSION.get(url, params=params, timeout=30)
        last = r
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", "2")) + attempt)
            continue
        if r.status_code >= 500:
            time.sleep(2 ** attempt)
            continue
        r.raise_for_status()
        return r.json()
    if last is not None:
        last.raise_for_status()
    raise RuntimeError("TMDB request failed")


def search_results(api_key: str, query: str, kind: str):
    data = tmdb_get(api_key, f"/search/{kind}", query=query, include_adult="false")
    return data.get("results") or []


def backdrop_paths_for(api_key: str, kind: str, tmdb_id: int):
    if kind == "collection":
        data = tmdb_get(api_key, f"/collection/{tmdb_id}/images")
    else:
        data = tmdb_get(api_key, f"/{kind}/{tmdb_id}/images", include_image_language="en,null")
    return [b["file_path"] for b in (data.get("backdrops") or []) if b.get("file_path")]


def find_backdrops(api_key: str, query: str, preferred=None, want=MAX_FRAMES):
    """Search-based fallback: backdrops from ONE title."""
    kinds = [preferred] if preferred in ("collection", "movie", "tv") else ["collection", "movie", "tv"]
    for kind in kinds:
        try:
            results = search_results(api_key, query, kind)
        except requests.RequestException as exc:
            log(f"    ! TMDB search {kind} failed: {exc}")
            continue
        if not results:
            continue
        first = results[0]
        paths = []
        try:
            paths = backdrop_paths_for(api_key, kind, first["id"])
        except requests.RequestException as exc:
            log(f"    ! TMDB images {kind}/{first['id']} failed: {exc}")
        for res in results:
            bp = res.get("backdrop_path")
            if bp and bp not in paths:
                paths.append(bp)
            if len(paths) >= want:
                break
        seen, unique = set(), []
        for p in paths:
            if p not in seen:
                seen.add(p)
                unique.append(p)
        if unique:
            return unique[:want], f"search:{kind}:{first['id']}"
    return [], None


def find_person_backdrops(api_key: str, query: str, want: int):
    """TMDB person search -> combined credits -> one backdrop per top title."""
    if want <= 0:
        return [], None
    try:
        data = tmdb_get(api_key, "/search/person", query=query, include_adult="false")
    except requests.RequestException:
        return [], None
    results = data.get("results") or []
    if not results:
        return [], None
    person = results[0]
    person_id = person["id"]

    try:
        credits = tmdb_get(api_key, f"/person/{person_id}/combined_credits")
    except requests.RequestException:
        return [], None

    cast = credits.get("cast") or []
    cast.sort(key=lambda x: x.get("popularity", 0), reverse=True)

    backdrops = []
    seen_ids = set()
    for c in cast:
        if len(backdrops) >= want:
            break
        cid = c.get("id")
        if not cid or cid in seen_ids:
            continue
        seen_ids.add(cid)
        kind = "tv" if c.get("media_type") == "tv" else "movie"
        try:
            paths = backdrop_paths_for(api_key, kind, cid)
        except requests.RequestException:
            continue
        if paths:
            backdrops.append(paths[0])

    if backdrops:
        return backdrops, f"person:{person_id}"
    return [], None


def extract_mdblist_ids(folder):
    """Return [(list_id, media_type), ...] from a folder's sources."""
    out = []
    for src in (folder.get("sources") or []) + (folder.get("nativeSources") or []):
        cid = (src.get("catalogId") or "").strip()
        m = re.match(r"^mdblist\.(\d+)$", cid)
        if m:
            out.append((int(m.group(1)), src.get("type", "movie")))
    # de-dup while preserving order
    seen, uniq = set(), []
    for pair in out:
        if pair not in seen:
            seen.add(pair)
            uniq.append(pair)
    return uniq


def fetch_mdblist_items(mdblist_key: str, list_id: int, limit: int = 30):
    url = f"{MDBLIST_API}/lists/{list_id}/items"
    try:
        r = SESSION.get(url, params={"apikey": mdblist_key, "limit": limit}, timeout=30)
        if r.status_code == 401:
            log(f"    ! MDBList {list_id} 401 Unauthorized (bad key?)")
            return []
        if r.status_code == 404:
            log(f"    ! MDBList {list_id} 404 (list missing or private)")
            return []
        r.raise_for_status()
        return r.json().get("items") or []
    except requests.RequestException as exc:
        log(f"    ! MDBList {list_id} failed: {exc}")
        return []


def backdrops_from_contents(api_key: str, mdblist_key: str, folder: dict, want=MAX_FRAMES):
    """Carousel from actual folder contents: one backdrop per title, up to `want` titles."""
    if not mdblist_key:
        return [], None
    mdblists = extract_mdblist_ids(folder)
    if not mdblists:
        return [], None

    backdrops = []
    seen_titles = set()
    used_lists = []

    for list_id, _ in mdblists:
        if len(backdrops) >= want:
            break
        items = fetch_mdblist_items(mdblist_key, list_id, limit=want * 4)
        if not items:
            continue
        used_lists.append(list_id)

        for item in items:
            if len(backdrops) >= want:
                break
            movie = item.get("movie") or item.get("show")
            if not movie:
                continue
            title = (movie.get("title") or movie.get("name") or "").strip()
            if not title or title in seen_titles:
                continue
            seen_titles.add(title)

            tmdb_id = (movie.get("ids") or {}).get("tmdb")
            if not tmdb_id:
                continue
            kind = "tv" if item.get("show") else "movie"
            try:
                paths = backdrop_paths_for(api_key, kind, int(tmdb_id))
            except requests.RequestException:
                continue
            if paths:
                backdrops.append(paths[0])

    if backdrops:
        return backdrops, f"contents:{','.join(str(i) for i in used_lists)}"
    return [], None


def find_backdrops_for_folder(api_key: str, mdblist_key: str, folder: dict, query: str, preferred):
    # 1. MDBList contents (carousel from actual folder)
    paths, source = backdrops_from_contents(api_key, mdblist_key, folder)

    # 2. Top off via person lookup (handles actor folders + short lists)
    if len(paths) < MAX_FRAMES:
        extra, extra_src = find_person_backdrops(api_key, query, MAX_FRAMES - len(paths))
        if extra:
            paths = paths + extra
            source = (source + "+" if source else "") + (extra_src or "")

    if len(paths) >= MAX_FRAMES:
        return paths[:MAX_FRAMES], source
    if paths:
        return paths, source

    # 3. Explicit tmdb.collection.NNNN
    for src in (folder.get("sources") or []) + (folder.get("nativeSources") or []):
        cid = (src.get("catalogId") or "").strip()
        m = re.match(r"^tmdb\.collection\.(\d+)$", cid)
        if m:
            coll_id = int(m.group(1))
            try:
                coll_paths = backdrop_paths_for(api_key, "collection", coll_id)
                if coll_paths:
                    return coll_paths[:MAX_FRAMES], f"collection:{coll_id}"
            except requests.RequestException as exc:
                log(f"    ! tmdb collection {coll_id} failed: {exc}")
            break

    # 4. Search fallback
    return find_backdrops(api_key, query, preferred)

def download_frame(path: str) -> Image.Image:
    r = SESSION.get(TMDB_IMG + path, timeout=60)
    r.raise_for_status()
    im = Image.open(BytesIO(r.content)).convert("RGB")
    return ImageOps.fit(im, SIZE, method=Image.Resampling.LANCZOS)


def write_webp(paths, out_path: Path) -> bool:
    frames = []
    for p in paths[:MAX_FRAMES]:
        try:
            frames.append(download_frame(p))
        except Exception as exc:
            log(f"    ! frame failed {p}: {exc}")
    if not frames:
        return False
    while len(frames) < 2:
        frames.append(frames[0].copy())
    out_path.parent.mkdir(parents=True, exist_ok=True)
    frames[0].save(
        out_path, "WEBP", save_all=True, append_images=frames[1:],
        duration=FRAME_MS, loop=0, quality=QUALITY, method=METHOD,
    )
    return True


def iter_folders(node, parent_key=None):
    if isinstance(node, dict):
        title = node.get("title")
        if isinstance(title, str) and title.strip():
            has_hero = "heroBackdropUrl" in node or "heroBackdropMode" in node
            if has_hero or parent_key in FOLDER_KEYS:
                yield node
        for k, v in node.items():
            yield from iter_folders(v, k)
    elif isinstance(node, list):
        for v in node:
            yield from iter_folders(v, parent_key)


def process_file(path: Path, args, api_key: str, mdblist_key: str, base_url: str, manifest: dict, prefix: bool) -> int:
    raw = path.read_text(encoding="utf-8-sig")
    data = json.loads(raw)
    mapping = {}
    folders = list(iter_folders(data))
    log(f"  {path}: {len(folders)} folder candidate(s)")

    for idx, folder in enumerate(folders):
        title = folder["title"].strip()
        if idx % 10 == 0:
            log(f"  [{idx+1}/{len(folders)}]")
        spec = args.overrides.get(title, {})
        query = (spec.get("query") or title).strip()
        preferred = spec.get("type")
        # Slug is now based on the FOLDER, not the query — each folder gets its own carousel
        slug = slugify(title)
        if prefix:
            slug = f"{path.stem}-{slug}"

        out_file = args.output_dir / f"{slug}.webp"
        log(f"  - {title!r}")

        paths, source = find_backdrops_for_folder(api_key, mdblist_key, folder, query, preferred)
        if not paths:
            log("    ! no backdrops found; leaving folder unchanged")
            continue

        digest = hashlib.sha256(
            ("|".join(paths) + f"|{SIZE}|{FRAME_MS}|{QUALITY}").encode("utf-8")
        ).hexdigest()
        entry = manifest["items"].get(slug, {})

        if entry.get("hash") == digest and out_file.exists() and not args.force:
            log(f"    = cached {out_file.name}")
        elif args.dry_run:
            log(f"    ~ dry-run would write {out_file.name} ({len(paths)} frames)")
        else:
            if not write_webp(paths, out_file):
                log("    ! failed to write WebP")
                continue
            manifest["items"][slug] = {
                "hash": digest, "file": out_file.name, "source": source, "paths": paths,
            }
            log(f"    + wrote {out_file.name} ({out_file.stat().st_size // 1024} KB, {len(paths)} frames)")

        mapping[title] = {
            "heroBackdropUrl": f"{base_url.rstrip('/')}/{out_file.name}",
            "heroBackdropMode": "static",
        }

    map_path = args.output_dir / "backdrop_map.json"
    existing = {}
    if map_path.exists():
        try:
            existing = json.loads(map_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}
    existing.update(mapping)
    if not args.dry_run:
        map_path.write_text(json.dumps(existing, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        log(f"  * wrote {map_path} ({len(existing)} total entries)")

    return len(mapping)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+", type=Path)
    ap.add_argument("--output-dir", type=Path, default=Path("backdrops"))
    ap.add_argument("--base-url", default=os.environ.get("RAW_BASE_URL", ""))
    ap.add_argument("--overrides", type=Path, default=Path("backdrop_queries.json"))
    ap.add_argument("--manifest", type=Path, default=Path("backdrops/manifest.json"))
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--profile-prefix", action="store_true")
    args = ap.parse_args()

    api_key = os.environ.get("TMDB_API_KEY")
    if not api_key or api_key.lower().startswith("paste"):
        sys.exit("TMDB_API_KEY is not set (or is still the placeholder)")
    mdblist_key = os.environ.get("MDBLIST_API_KEY", "").strip()
    if not mdblist_key:
        log("WARNING: MDBLIST_API_KEY not set — falling back to single-title search for every folder")

    base_url = args.base_url.strip()
    if not base_url:
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        branch = os.environ.get("GITHUB_REF_NAME", "main")
        if not repo:
            sys.exit("Set RAW_BASE_URL or GITHUB_REPOSITORY")
        base_url = f"https://raw.githubusercontent.com/{repo}/{branch}/backdrops"

    overrides = {}
    if args.overrides.exists():
        overrides = json.loads(args.overrides.read_text(encoding="utf-8-sig"))
    args.overrides = overrides

    args.output_dir.mkdir(parents=True, exist_ok=True)
    manifest = {"items": {}}
    if args.manifest.exists():
        try:
            manifest = json.loads(args.manifest.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError:
            pass
    manifest.setdefault("items", {})

    prefix = args.profile_prefix or len(args.files) > 1
    total = 0
    for f in args.files:
        total += process_file(f, args, api_key, mdblist_key, base_url, manifest, prefix)

    if not args.dry_run:
        args.manifest.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    log(f"Done. per-file entries={total}, base_url={base_url}")


if __name__ == "__main__":
    main()