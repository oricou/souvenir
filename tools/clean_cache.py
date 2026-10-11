#!/usr/bin/env python3
"""Remove from cache/ the previews and face images of photos that are gone.

    tools/clean_cache.py            # (make clean-cache)
    tools/clean_cache.py --dry-run  # only show what would be removed

A photo deleted from the site goes to data/trash/: its preview (cache/thumbs/)
and the small images of its faces (cache/face_crops/) stay, so that putting it
back is instant. This removes them once the photo is neither in data/photos/
nor in data/trash/. Unfinished files (*.tmp) are removed too.

The other caches clean themselves: cache/photos.json at once, cache/faces.*
and cache/embeddings.* at the next tools/find_faces.py / tools/embed_photos.py.
Everything removed here is remade when needed.
"""

import argparse
import hashlib
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import photos  # noqa: E402

THUMBS = os.path.join(ROOT, "cache", "thumbs")
CROPS = os.path.join(ROOT, "cache", "face_crops")
FACES = os.path.join(ROOT, "cache", "faces.json")
NUMBERED = re.compile(r"^(.*) \(\d+\)(\.[^.]+)$")  # "photo (2).jpg" in the trash = "photo.jpg"


def photo_names(folder):
    """Paths (relative, with "/") of the photos and videos under folder."""
    names = set()
    for root, subdirs, files in os.walk(folder):
        subdirs[:] = [d for d in subdirs if not d.startswith(".")]
        for f in files:
            if os.path.splitext(f)[1].lower() in photos.IMAGE_EXT | photos.VIDEO_EXT:
                name = os.path.relpath(os.path.join(root, f), folder).replace(os.sep, "/")
                names.add(name)
                m = NUMBERED.match(name)
                if m:
                    names.add(m.group(1) + m.group(2))
    return names


def files_under(folder):
    for root, _, files in os.walk(folder):
        for f in files:
            yield os.path.join(root, f)


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog="\n".join(__doc__.splitlines()[2:]))
    p.add_argument("--data", default=os.path.join(ROOT, "data"))
    p.add_argument("--dry-run", action="store_true", help="only show what would be removed")
    args = p.parse_args()

    kept = photo_names(os.path.join(args.data, "photos")) | photo_names(os.path.join(args.data, "trash"))
    print(f"{len(kept)} photos in data/photos and data/trash")

    # Face images are named sha1("photo#index#box") (people.People.crop): those of the
    # faces still listed in cache/faces.json for a kept photo stay.
    wanted = set()
    try:
        with open(FACES, encoding="utf-8") as f:
            meta = json.load(f)
        for photo, info in meta["photos"].items():
            if photo in kept:
                for i, face in enumerate(info["faces"]):
                    wanted.add(hashlib.sha1(f"{photo}#{i}#{face['box']}".encode()).hexdigest())
        faces_known = True
    except (OSError, ValueError, KeyError):
        faces_known = False  # no faces list: do not guess, keep every face image

    doomed = []
    for path in files_under(THUMBS):
        name = os.path.relpath(path, THUMBS).replace(os.sep, "/")
        if name.endswith(".tmp") or not (name.endswith(".jpg") and name[:-4] in kept):
            doomed.append(path)
    if faces_known:
        for path in files_under(CROPS):
            key = os.path.basename(path)
            if key.endswith(".tmp") or key[:-4] not in wanted:
                doomed.append(path)

    size = sum(os.path.getsize(f) for f in doomed)
    thumbs = sum(1 for f in doomed if f.startswith(THUMBS + os.sep))
    print(f"{'Would remove' if args.dry_run else 'Removing'} {thumbs} previews and "
          f"{len(doomed) - thumbs} face images: {size / 1e6:.1f} MB"
          + ("" if faces_known else " (no cache/faces.json: face images kept)"))
    if args.dry_run:
        for f in doomed[:20]:
            print("  ", os.path.relpath(f, ROOT))
        if len(doomed) > 20:
            print(f"   … and {len(doomed) - 20} more")
        return
    for f in doomed:
        os.remove(f)
    for folder in (THUMBS, CROPS):  # empty subfolders left behind
        for root, subdirs, files in os.walk(folder, topdown=False):
            if root != folder and not os.listdir(root):
                os.rmdir(root)


if __name__ == "__main__":
    main()
