"""Memory compaction changes summaries while preserving event linkage."""

import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


class MaintenanceTests(unittest.TestCase):
    def test_compaction_archives_original_and_keeps_event_fields(self):
        with patch.dict(sys.modules, {"fcntl": SimpleNamespace(
            LOCK_EX=1, LOCK_NB=2, LOCK_UN=3, flock=lambda *_: None)}):
            maintenance = importlib.import_module("agent_maintenance")
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            gateway = base / "gateway"
            gateway.mkdir()
            memory = base / "content" / "pack" / "play" / "memory"
            memory.mkdir(parents=True)
            original = {"pack:play": {"surfaces": {"messages.dm.wan-zhou": "晚舟去了码头，晚舟去了码头。"},
                                      "ledger": [{"eventId": "event-1", "step": 1,
                                                  "batchIds": ["batch-1"], "summary": "晚舟去了码头，晚舟去了码头。"}]}}
            (gateway / "muse-memory.json").write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
            proposal = memory / "compaction-proposal.json"
            proposal.write_text(json.dumps({"scope": "pack:play", "ledger": [
                {"eventId": "event-1", "step": 1, "summary": "晚舟去了码头。"}],
                "surfaces": {"messages.dm.wan-zhou": "晚舟去了码头。"}}, ensure_ascii=False), encoding="utf-8")
            with patch.object(maintenance, "GATEWAY", gateway), patch.object(
                maintenance, "CONTENT", base / "content"):
                changed = maintenance.apply_compaction({"packId": "pack", "playthroughId": "play"}, proposal)
            updated = json.loads((gateway / "muse-memory.json").read_text(encoding="utf-8"))["pack:play"]["ledger"][0]
            self.assertEqual(changed, 1)
            self.assertEqual((updated["eventId"], updated["step"], updated["batchIds"]),
                             ("event-1", 1, ["batch-1"]))
            self.assertEqual(updated["summary"], "晚舟去了码头。")
            self.assertEqual(len(list((gateway / "memory-archive").glob("before-compaction-*.json"))), 1)


if __name__ == "__main__":
    unittest.main()
