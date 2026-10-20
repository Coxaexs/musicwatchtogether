import os
import stat
import tempfile
import unittest

from storage import save_json, share_with_group


class ShareWithGroupTests(unittest.TestCase):
    def test_owner_only_files_become_group_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            old = os.path.join(directory, "old.json")
            with open(old, "w") as handle:
                handle.write("{}")
            os.chmod(old, 0o600)
            new = os.path.join(directory, "new.json")
            save_json(new, {"track": "x"})

            self.assertEqual(share_with_group(directory), 1)
            self.assertEqual(stat.S_IMODE(os.stat(old).st_mode), 0o660)
            self.assertEqual(stat.S_IMODE(os.stat(new).st_mode), 0o660)
            self.assertEqual(share_with_group(directory), 0)

    def test_missing_directory_is_fine(self):
        self.assertEqual(share_with_group("/nonexistent/lyrics"), 0)


if __name__ == "__main__":
    unittest.main()
