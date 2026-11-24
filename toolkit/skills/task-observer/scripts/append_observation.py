#!/usr/bin/env python3
"""Append one observation to a shared log under a fresh number, in one process.

Usage: python3 -I append_observation.py <log.md> <entry.md>

<entry.md> holds the whole entry, starting with the header line
"### Observation NNN: <title>" (NNN literally) and carrying "**Status:** OPEN".
Holding an exclusive lock on the log, this reads the live file, takes max+1,
appends the entry under that number at the end, then re-reads the file and
checks the number appears exactly once and the title landed. It prints the
number it used. Exit 1 when a post-write check fails (a writer that ignores
the lock collided: renumber this entry by hand), 2 on bad input.

One plain command, so it also runs where a command guard refuses
`$(( $(grep ...) + 1 ))` or several heredocs.
"""
import fcntl
import re
import sys
from pathlib import Path

HEADER = re.compile(r"^### Observation (\d+):", re.M)
PLACEHOLDER = re.compile(r"^### Observation NNN: (\S.*)$")


def main(argv):
    if len(argv) != 3:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    log, entry_path = Path(argv[1]), Path(argv[2])
    entry = entry_path.read_text(encoding="utf-8")
    first, _, rest = entry.partition("\n")
    m = PLACEHOLDER.match(first.rstrip())
    if not m:
        print('the entry must start with "### Observation NNN: <title>"', file=sys.stderr)
        return 2
    if "**Status:** OPEN" not in rest:
        print('the entry must carry "**Status:** OPEN"', file=sys.stderr)
        return 2
    title = m.group(1).strip()

    with open(log, "r+", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            text = fh.read()
            numbers = [int(n) for n in HEADER.findall(text)]
            number = (max(numbers) if numbers else 0) + 1
            block = f"### Observation {number}: {title}\n{rest}".rstrip("\n") + "\n"
            if not text or text.endswith("\n\n"):
                sep = ""
            elif text.endswith("\n"):
                sep = "\n"
            else:
                sep = "\n\n"
            fh.seek(0, 2)
            fh.write(sep + block)
            fh.flush()
            fh.seek(0)
            after = fh.read()
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)

    count = len(re.findall(rf"^### Observation {number}:", after, re.M))
    landed = f"### Observation {number}: {title}\n" in after
    if count != 1 or not landed:
        print(f"check failed: #{number} appears {count} time(s); title landed: {landed}", file=sys.stderr)
        return 1
    print(number)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
