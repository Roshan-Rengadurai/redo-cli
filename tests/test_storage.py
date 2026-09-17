import os
import tempfile
import unittest

from redo_cli.models import FailureEvent
from redo_cli.storage import Storage, default_db_path


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["REDO_HOME"] = self.tmp.name
        self.store = Storage(default_db_path())

    def tearDown(self):
        self.store.close()
        os.environ.pop("REDO_HOME", None)
        self.tmp.cleanup()

    def _ev(self, command, ts):
        return FailureEvent(id=None, command=command, exit_code=1,
                            stderr="boom", cwd="/tmp", timestamp=ts, shell="zsh")

    def test_record_and_latest(self):
        self.store.record(self._ev("first", 100.0))
        self.store.record(self._ev("second", 200.0))
        latest = self.store.latest()
        self.assertIsNotNone(latest)
        self.assertEqual(latest.command, "second")
        self.assertEqual(latest.stderr, "boom")

    def test_recent_order_and_limit(self):
        for i in range(5):
            self.store.record(self._ev("cmd%d" % i, 100.0 + i))
        recent = self.store.recent(3)
        self.assertEqual([e.command for e in recent], ["cmd4", "cmd3", "cmd2"])

    def test_empty(self):
        self.assertIsNone(self.store.latest())
        self.assertEqual(self.store.recent(), [])

    def test_default_path_honours_env(self):
        self.assertTrue(str(default_db_path()).startswith(self.tmp.name))


if __name__ == "__main__":
    unittest.main()
