#!/usr/bin/env python3
"""
claude_code_healthcheck.py

Quick diagnostic for Claude Code's local footprint on macOS:
  1. Disk usage of its scratchpad (/private/tmp/claude-<uid>) and
     home config dir (~/.claude)
  2. Any lingering dev-server / browser-automation processes it may
     have spawned (next dev, playwright, chromium, node) that are
     still running

Usage:
    python3 claude_code_healthcheck.py
    python3 claude_code_healthcheck.py --kill   # also offer to kill orphans
"""

import argparse
import os
import subprocess
import sys

WATCH_PROCESS_PATTERNS = [
    "next dev",
    "next-server",
    "playwright",
    "chromium",
    "chrome-mac",
]


def run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=False).stdout.strip()
    except FileNotFoundError:
        return ""


def get_dir_size(path: str) -> str:
    if not os.path.isdir(path):
        return "not found"
    out = run(["du", "-sh", path])
    return out.split("\t")[0] if out else "unknown"


def check_disk_usage():
    uid = os.getuid()
    scratchpad = f"/private/tmp/claude-{uid}"
    home_config = os.path.expanduser("~/.claude")

    print("=== Disk usage ===")
    print(f"{scratchpad:45s} {get_dir_size(scratchpad)}")
    print(f"{home_config:45s} {get_dir_size(home_config)}")
    print()


def check_orphan_processes():
    print("=== Potentially orphaned processes ===")
    ps_output = run(["ps", "aux"])
    if not ps_output:
        print("Could not read process list.")
        return []

    matches = []
    for line in ps_output.splitlines()[1:]:
        lower = line.lower()
        if any(p in lower for p in WATCH_PROCESS_PATTERNS) and "grep" not in lower:
            matches.append(line)

    if not matches:
        print("None found. Looks clean.")
    else:
        for line in matches:
            parts = line.split(None, 10)
            pid = parts[1] if len(parts) > 1 else "?"
            cmd = parts[10] if len(parts) > 10 else line
            print(f"PID {pid:<8} {cmd[:100]}")
    print()
    return matches


def offer_kill(matches):
    if not matches:
        return
    answer = input("Kill these processes? [y/N]: ").strip().lower()
    if answer != "y":
        print("Skipped.")
        return
    for line in matches:
        parts = line.split(None, 10)
        pid = parts[1] if len(parts) > 1 else None
        if pid:
            subprocess.run(["kill", pid], check=False)
            print(f"Killed PID {pid}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--kill", action="store_true", help="Prompt to kill orphaned processes found")
    args = parser.parse_args()

    if sys.platform != "darwin":
        print("Warning: this script assumes macOS paths (/private/tmp/claude-<uid>).")

    check_disk_usage()
    matches = check_orphan_processes()

    if args.kill:
        offer_kill(matches)


if __name__ == "__main__":
    main()
