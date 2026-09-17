"""Startup-time ceiling. redo runs in the shell hot path; keep it cheap."""

import os
import statistics
import subprocess
import sys
import tempfile
import time
import unittest

CEILING_MS = 150   # loose: CI boxes are slow. Laptop target is <50 ms.


class Bench(unittest.TestCase):
    def test_cold_start_under_ceiling(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, REDO_HOME=tmp, NO_COLOR="1")
            cmd = [sys.executable, "-m", "redo_cli"]
            subprocess.run(cmd, env=env, capture_output=True)   # warm the fs cache
            samples = []
            for _ in range(7):
                t = time.perf_counter()
                subprocess.run(cmd, env=env, capture_output=True, check=True)
                samples.append((time.perf_counter() - t) * 1000)
        median = statistics.median(samples)
        print("\nredo cold start: median %.0f ms (min %.0f)" % (median, min(samples)))
        self.assertLess(median, CEILING_MS)


if __name__ == "__main__":
    unittest.main()
