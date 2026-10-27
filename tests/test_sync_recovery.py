"""Offline check that panel installs stay pinned to the selected Git tag."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sync_recovery import sync


class SyncRecovery(unittest.TestCase):
    def test_installs_tagged_version_and_keeps_it_pinned(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "repo"
            repo.mkdir()

            def git(*args):
                subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)

            git("init", "-q")
            src = repo / "fathom_recovery.py"
            src.write_text('__version__ = "1.2.0"\n', encoding="utf-8")
            git("add", ".")
            git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "first")
            git("tag", "v1.2.0")

            dest = Path(temp) / "panel" / "scripts"
            result = sync(str(repo), "v1.2.0", str(dest))
            self.assertTrue(result["ok"])
            self.assertEqual(result["version"], "1.2.0")

            src.write_text('__version__ = "1.3.0"\n', encoding="utf-8")
            git("add", ".")
            git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "second")
            result = sync(str(repo), "v1.2.0", str(dest))
            self.assertEqual(result["version"], "1.2.0")
            self.assertEqual((dest / "fathom_recovery.py").read_text(encoding="utf-8"),
                             '__version__ = "1.2.0"\n')

    def test_rejects_credentials_in_url(self):
        with self.assertRaisesRegex(ValueError, "credentials"):
            sync("https://name:secret@example.invalid/org/repo.git", "v1.2.0", "scripts")


if __name__ == "__main__":
    unittest.main()
