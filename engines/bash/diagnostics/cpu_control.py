#!/usr/bin/env python3
"""Calibrate root-versus-child CPU accounting; not a performance workload."""

import json
import os
import resource
import sys
import time


def burn(system=False):
    start = time.process_time()
    while time.process_time() - start < 0.15:
        if system:
            os.stat("/dev/null")
        else:
            sum(range(50))


if sys.argv[1] == "child":
    child = os.fork()
    if child == 0:
        burn()
        os._exit(0)
    os.waitpid(child, 0)
else:
    burn(sys.argv[1] == "system")
self_usage = resource.getrusage(resource.RUSAGE_SELF)
children = resource.getrusage(resource.RUSAGE_CHILDREN)
print(json.dumps({"self_user": self_usage.ru_utime, "self_system": self_usage.ru_stime,
                  "children_user": children.ru_utime, "children_system": children.ru_stime}))
