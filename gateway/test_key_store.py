import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import key_store


class ScopedKeyTests(unittest.TestCase):
    def test_distinct_saves_select_distinct_keys_and_reuse_copies_source(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(key_store, 'CONFIG', directory):
            first = {'packId': 'pack', 'playthroughId': 'first'}
            second = {'packId': 'pack', 'playthroughId': 'second'}
            third = {'packId': 'pack', 'playthroughId': 'third'}
            a = b'a' * 32
            b = b'b' * 32
            key_store.write_key(first, base64.urlsafe_b64encode(a).decode())
            key_store.write_key(second, base64.urlsafe_b64encode(b).decode())
            self.assertEqual(key_store.read_key(first), a)
            self.assertEqual(key_store.read_key(second), b)
            self.assertEqual(key_store.read_key(third, first), a)
            self.assertEqual(Path(key_store.key_path(third)).read_text().strip(),
                             Path(key_store.key_path(first)).read_text().strip())


if __name__ == '__main__':
    unittest.main()
