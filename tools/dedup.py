#!/usr/bin/env python3
"""Find files with identical content in a directory and remove the extra copies.

By default nothing is changed: the duplicates are only listed. Add --delete to
remove them, or --trash DIR to move them into DIR instead.

Two files are duplicates when their data is byte-for-byte the same (names and
dates do not matter). Candidates are narrowed by size, then by a hash of their
first 64 KB, and confirmed by a SHA-256 of the whole content, so large files
are only read in full when they may really be equal.

In each group of identical files one is kept: the shortest name (the original
rather than "x (1).jpg" or "copie de x.jpg"), then the oldest, then the first
alphabetically. Symbolic links are ignored, and hard links to the same file are
not counted as copies. Empty files are skipped unless --include-empty.

--remove-from DIR (repeatable; relative to the current folder or to the scanned
directory) limits the removal to copies inside DIR: a group's kept file is
taken outside it when there is one, and files outside it are never removed.
If all copies of a group are inside DIR, one of them is kept. Groups with no
copy inside DIR are left alone and not listed.

Examples:
    tools/dedup.py data/photos -r                 # list only
    tools/dedup.py data/photos -r --delete        # remove the copies
    tools/dedup.py data/gps --trash /tmp/doublons # move them aside
    tools/dedup.py data/photos -r --remove-from data/photos/import   # only copies there
"""

import argparse
import hashlib
import os
import shutil
import sys
from collections import defaultdict

HEAD = 64 * 1024  # bytes hashed in the quick first pass


def files_in(root, recursive):
    """(path, stat) of the regular files in root; no symlinks, no hidden folders."""
    if recursive:
        for folder, subdirs, names in os.walk(root):
            subdirs[:] = sorted(d for d in subdirs if not d.startswith("."))
            for n in sorted(names):
                yield from _regular(os.path.join(folder, n))
    else:
        for n in sorted(os.listdir(root)):
            yield from _regular(os.path.join(root, n))


def _regular(path):
    try:
        st = os.lstat(path)
    except OSError:
        return
    if os.path.isfile(path) and not os.path.islink(path):
        yield path, st


def digest(path, limit=None):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        if limit:
            h.update(f.read(limit))
        else:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    return h.hexdigest()


def refine(groups, key):
    """Split each group of paths by key(path); keep only sub-groups of 2 or more."""
    out = []
    for paths in groups:
        sub = defaultdict(list)
        for p in paths:
            try:
                sub[key(p)].append(p)
            except OSError as e:
                print(f"cannot read {p}: {e}", file=sys.stderr)
        out.extend(g for g in sub.values() if len(g) > 1)
    return out


def find_duplicates(root, recursive, include_empty):
    """Groups of paths whose content is identical."""
    by_size, seen = defaultdict(list), set()
    for path, st in files_in(root, recursive):
        if (st.st_dev, st.st_ino) in seen:
            continue  # another name (hard link) of a file already listed
        seen.add((st.st_dev, st.st_ino))
        if st.st_size or include_empty:
            by_size[st.st_size].append(path)
    groups = refine([g for g in by_size.values() if len(g) > 1], lambda p: digest(p, HEAD))
    # Up to HEAD bytes the first pass already read everything; beyond, confirm on the whole file.
    small = [g for g in groups if os.path.getsize(g[0]) <= HEAD]
    large = refine([g for g in groups if os.path.getsize(g[0]) > HEAD], digest)
    return small + large


def keeper_order(path):
    """Sort key: the first file of a group is the one kept."""
    return len(os.path.basename(path)), os.path.getmtime(path), path


def inside(path, folders):
    """Whether path is in one of folders (real absolute paths)."""
    real = os.path.realpath(path)
    return any(real == f or real.startswith(f + os.sep) for f in folders)


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog="\n".join(__doc__.splitlines()[2:]))
    p.add_argument("directory")
    p.add_argument("-r", "--recursive", action="store_true", help="include subfolders")
    action = p.add_mutually_exclusive_group()
    action.add_argument("--delete", action="store_true", help="remove the extra copies")
    action.add_argument("--trash", metavar="DIR", help="move the extra copies into DIR")
    p.add_argument("--include-empty", action="store_true",
                   help="also treat empty files as copies of each other")
    p.add_argument("--remove-from", metavar="DIR", action="append", default=[],
                   help="only remove copies inside DIR (repeatable)")
    args = p.parse_args()
    if not os.path.isdir(args.directory):
        sys.exit(f"not a directory: {args.directory}")
    removable = []
    for d in args.remove_from:
        if not os.path.isdir(d) and os.path.isdir(os.path.join(args.directory, d)):
            d = os.path.join(args.directory, d)
        if not os.path.isdir(d):
            sys.exit(f"not a directory: {d}")
        removable.append(os.path.realpath(d))
    if args.trash:
        os.makedirs(args.trash, exist_ok=True)

    def order(path):  # with --remove-from, keep a file outside those folders first
        return (bool(removable) and inside(path, removable),) + keeper_order(path)

    groups = []
    for g in find_duplicates(args.directory, args.recursive, args.include_empty):
        keep, *rest = sorted(g, key=order)
        extra = [q for q in rest if not removable or inside(q, removable)]
        if extra:
            groups.append(([keep] + [q for q in rest if q not in extra], extra))
    groups.sort(key=lambda g: g[0][0])
    copies = saved = 0
    for kept, extra in groups:
        for path in kept:
            print(f"keep    {path}")
        for path in extra:
            size = os.path.getsize(path)
            if args.delete:
                os.remove(path)
                verb = "deleted"
            elif args.trash:
                rel = os.path.relpath(path, args.directory)
                dest = os.path.join(args.trash, rel)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.move(path, dest)
                verb = "moved  "
            else:
                verb = "copy   "
            print(f"{verb} {path}")
            copies += 1
            saved += size
    done = "removed" if args.delete else "moved" if args.trash else "found (nothing changed, use --delete)"
    print(f"\n{copies} duplicate(s) in {len(groups)} group(s), {human(saved)} {done}")


if __name__ == "__main__":
    main()
