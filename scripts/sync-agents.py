#!/usr/bin/env python3
"""Project-level script to sync .claude focused skills to a framework-agnostic .agents directory."""

import filecmp
import shutil
import sys
from pathlib import Path


def main():
    root_dir = Path(__file__).resolve().parent.parent
    src_dir = root_dir / ".claude" / "skills"
    dest_dir = root_dir / ".agents" / "skills"

    if not src_dir.exists():
        print(f"Error: Source directory {src_dir} does not exist.", file=sys.stderr)
        sys.exit(1)

    print(f"Syncing from {src_dir.relative_to(root_dir)} to {dest_dir.relative_to(root_dir)}...")

    # Create destination directory if it doesn't exist
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Track files to copy and clean up deleted files in destination
    src_files = {}

    for src_path in src_dir.rglob("*"):
        if src_path.is_file():
            rel_path = src_path.relative_to(src_dir)
            src_files[rel_path] = src_path

    # Delete files in target that are no longer in source
    for dest_path in dest_dir.rglob("*"):
        if dest_path.is_file():
            rel_path = dest_path.relative_to(dest_dir)
            if rel_path not in src_files:
                print(f"Removing obsolete target file: {rel_path}")
                dest_path.unlink()

    # Clean up empty directories in target
    for dest_path in sorted(dest_dir.rglob("*"), reverse=True):
        if dest_path.is_dir() and not any(dest_path.iterdir()):
            dest_path.rmdir()

    # Copy files
    copied_count = 0
    for rel_path, src_path in src_files.items():
        target_path = dest_dir / rel_path
        target_path.parent.mkdir(parents=True, exist_ok=True)

        # Only copy if modified or does not exist
        if not target_path.exists() or not filecmp.cmp(src_path, target_path, shallow=False):
            shutil.copy2(src_path, target_path)
            print(f"  Synced: {rel_path}")
            copied_count += 1

    print(f"Sync complete! {copied_count} file(s) updated.")


if __name__ == "__main__":
    main()
