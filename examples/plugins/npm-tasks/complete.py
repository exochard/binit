"""Complete npm run task names with Binit's isolated-provider protocol."""
import json
from pathlib import Path
import sys


def main():
    payload = sys.stdin.buffer.read(65537)
    if len(payload) > 65536 or not payload.endswith(b"\0"):
        return
    fields = payload[:-1].split(b"\0")
    if len(fields) < 8 or fields[0] != b"binit-completion-v1":
        return
    try:
        current = int(fields[3])
        count = int(fields[7])
    except ValueError:
        return
    words = fields[8:]
    if count != len(words) or current != 2 or len(words) < 3 or words[1] != b"run":
        return
    try:
        with Path("package.json").open("rb") as source:
            data = source.read(1048577)
        if len(data) > 1048576:
            return
        scripts = json.loads(data).get("scripts", {})
    except (OSError, ValueError, AttributeError):
        return
    if not isinstance(scripts, dict):
        return
    total = 0
    for name in sorted(scripts):
        encoded = name.encode("utf-8")
        if b"\0" in encoded or len(encoded) > 4096 or not encoded.startswith(words[current]):
            continue
        total += len(encoded) + 1
        if total > 1048576:
            return
        sys.stdout.buffer.write(encoded + b"\0")


if __name__ == "__main__":
    main()
