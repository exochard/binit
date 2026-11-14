#!/usr/bin/env python3
"""Known delayed leaf provider for cancellation measurements, not workload gains."""
import json
import os
from pathlib import Path
import resource
import sys
import time

mode, destination = sys.argv[1:]
if mode == "isolated":
    fields = sys.stdin.buffer.read().split(b"\0")
    assert fields[0] == b"binit-completion-v1"
    assert fields[1] == b"probe_cmd p"
receipt = {"pid": os.getpid(), "parent": os.getppid(),
           "limits": {name: resource.getrlimit(value) for name, value in
                      (("address_space", resource.RLIMIT_AS), ("cpu", resource.RLIMIT_CPU),
                       ("descriptors", resource.RLIMIT_NOFILE))},
           "descriptors": len(os.listdir("/proc/self/fd")) - 1}
path = Path(destination)
temporary = path.with_suffix(".pending")
temporary.write_text(json.dumps(receipt))
temporary.replace(path)
time.sleep(.2)
os.write(1, b"p stale one\0p stale two\0")
