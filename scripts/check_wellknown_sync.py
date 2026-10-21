#!/usr/bin/env python3
"""Verify the well-known endpoint tree mirrors the canonical skill tree.

The well-known endpoint (.well-known/skills/) serves a copy of the skill
tree because raw.githubusercontent does not follow symlinks. This script
fails if the two drift, so a stale endpoint copy never silently ships.

Exit 0 = in sync, 1 = drift (lists the diffs).
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CANON = os.path.join(REPO, "skills", "post-call-hubspot")
SERVED = os.path.join(REPO, ".well-known", "skills", "post-call-hubspot")

def tree(root):
    out = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root)
            with open(full, "rb") as f:
                out[rel] = f.read()
    return out

def main():
    for label, path in (("canonical", CANON), ("served", SERVED)):
        if not os.path.isdir(path):
            print(f"FAIL: {label} tree missing: {path}")
            return 1

    canon, served = tree(CANON), tree(SERVED)
    diffs = []
    for rel in sorted(set(canon) | set(served)):
        if rel not in canon:
            diffs.append(f" extra in served tree: {rel}")
        elif rel not in served:
            diffs.append(f" missing from served: {rel}")
        elif canon[rel] != served[rel]:
            diffs.append(f" content differs: {rel}")

    if diffs:
        print(f"DRIFT between {CANON} and {SERVED}:")
        print("\n".join(diffs))
        return 1
    print(f"OK: {len(canon)} files in sync")
    return 0

if __name__ == "__main__":
    sys.exit(main())
