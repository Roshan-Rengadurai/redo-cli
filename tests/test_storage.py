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

    def test_spool_path_is_beside_db(self):
        from redo_cli.storage import spool_path
        self.assertEqual(spool_path(), default_db_path().parent / "spool")

    def test_parse_spool_records(self):
        from redo_cli.storage import parse_spool
        text = ("zsh\x1f127\x1f/tmp\x1f1700000000\x1fgti status\x1e"
                "bash\x1f1\x1f/home/x\x1f1700000001\x1fnpm run dev\x1e")
        events = parse_spool(text)
        self.assertEqual([e.command for e in events], ["gti status", "npm run dev"])
        self.assertEqual(events[0].exit_code, 127)
        self.assertEqual(events[0].cwd, "/tmp")
        self.assertEqual(events[0].timestamp, 1700000000.0)
        self.assertEqual(events[0].shell, "zsh")
        self.assertEqual(events[0].stderr, "")

    def test_parse_spool_keeps_multiline_commands(self):
        from redo_cli.storage import parse_spool
        text = "zsh\x1f1\x1f/tmp\x1f1\x1ffor f in *; do\n  cat $f\ndone\x1e"
        self.assertEqual(parse_spool(text)[0].command, "for f in *; do\n  cat $f\ndone")

    def test_parse_spool_drops_partial_and_malformed(self):
        from redo_cli.storage import parse_spool
        text = ("zsh\x1f1\x1f/tmp\x1f1\x1fgood\x1e"
                "zsh\x1fnotanint\x1f/tmp\x1f1\x1fbad\x1e"
                "zsh\x1f1\x1f/tmp\x1e"          # too few fields
                "zsh\x1f1\x1f/tmp\x1f1\x1fpartial-no-terminator")
        self.assertEqual([e.command for e in parse_spool(text)], ["good"])

    def test_ingest_spool_records_and_removes_file(self):
        from redo_cli.storage import spool_path
        p = spool_path()
        p.write_text("zsh\x1f127\x1f/tmp\x1f5\x1fgti status\x1e")
        n = self.store.ingest_spool(p)
        self.assertEqual(n, 1)
        self.assertFalse(p.exists())
        self.assertEqual(self.store.latest().command, "gti status")

    def test_ingest_spool_missing_file_is_noop(self):
        from redo_cli.storage import spool_path
        self.assertEqual(self.store.ingest_spool(spool_path()), 0)


if __name__ == "__main__":
    unittest.main()
