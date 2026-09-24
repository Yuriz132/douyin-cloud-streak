import unittest
import tempfile
import shutil
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from core import ledger
from core import automation


class TestLedgerAndFiber(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        self.orig_account_dir = ledger.account_dir
        # Redirect account_dir to temporary test directory
        ledger.account_dir = lambda aid=None: self.test_dir

    def tearDown(self):
        ledger.account_dir = self.orig_account_dir
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_default_entry_schema(self):
        entry = ledger._default_entry("测试好友")
        self.assertIn("user_id", entry)
        self.assertIn("conversation_id", entry)
        self.assertIn("sec_uid", entry)
        self.assertIn("short_id", entry)
        self.assertEqual(entry["user_id"], "")
        self.assertEqual(entry["conversation_id"], "")
        self.assertEqual(entry["sec_uid"], "")
        self.assertEqual(entry["short_id"], "")

    def test_upsert_and_rename_by_id(self):
        entries = []
        # First insertion
        e1 = ledger._upsert(entries, {
            "display_name": "蛇宝",
            "user_id": "1008611",
            "conversation_id": "0:111:222",
            "streak_days": 10,
            "has_conversation": True,
        })
        e1["selected"] = True
        e1["last_sent_at"] = "2026-09-24T18:00:00"

        # Subsequent upsert with same user_id but new display_name (simulate contact rename)
        e2 = ledger._upsert(entries, {
            "display_name": "新蛇宝备注",
            "user_id": "1008611",
            "streak_days": 11,
            "has_conversation": True,
        })
        # Should NOT duplicate, should update display_name and preserve selected / last_sent_at
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["display_name"], "新蛇宝备注")
        self.assertTrue(entries[0]["selected"])
        self.assertEqual(entries[0]["last_sent_at"], "2026-09-24T18:00:00")
        self.assertEqual(entries[0]["streak_days"], 11)

    def test_update_contact_name_by_id_self_healing(self):
        # Save an initial ledger
        initial_entries = [
            {
                **ledger._default_entry("蛇宝"),
                "user_id": "1008611",
                "conversation_id": "0:111:222",
                "selected": True,
            }
        ]
        ledger._save(initial_entries)

        # Trigger self healing
        success = ledger.update_contact_name_by_id(
            old_name="蛇宝",
            new_name="新备注小蛇",
            target_ids={"user_id": "1008611", "conversation_id": "0:111:222"},
        )
        self.assertTrue(success)

        # Verify persisted state
        loaded = ledger.load_ledger()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["display_name"], "新备注小蛇")
        self.assertTrue(loaded[0]["selected"])

    def test_locate_contact_priority(self):
        mock_page = MagicMock()
        mock_page.evaluate.return_value = {"moved": True}
        # Mock 1: First priority succeeds -> fiber should NOT be called
        with patch.object(automation, "_find_contact") as mock_find, \
             patch.object(automation, "_verify_in_conversation", return_value=True) as mock_verify, \
             patch.object(automation, "_locate_by_fiber_id") as mock_fiber, \
             patch.object(automation, "_list_title_matches", return_value=1), \
             patch.object(automation, "_quick_logged_out", return_value=False), \
             patch("time.sleep", return_value=None):
            
            mock_target = MagicMock()
            mock_target.count.return_value = 1
            mock_find.return_value = mock_target

            entry = {"display_name": "好友A", "user_id": "999"}
            ok, why = automation._locate_contact(mock_page, "好友A", entry=entry)
            self.assertTrue(ok, f"Failed with: {why}")
            mock_fiber.assert_not_called()

        # Mock 2: Name match fails, search fails, fiber ID succeeds
        with patch.object(automation, "_find_contact") as mock_find, \
             patch.object(automation, "_search_and_open", return_value=False), \
             patch.object(automation, "_locate_by_fiber_id", return_value=True) as mock_fiber, \
             patch.object(automation, "_list_title_matches", return_value=0), \
             patch.object(automation, "_quick_logged_out", return_value=False), \
             patch("time.sleep", return_value=None):
            
            mock_target = MagicMock()
            mock_target.count.return_value = 0
            mock_find.return_value = mock_target

            entry = {"display_name": "好友A", "user_id": "999"}
            ok, why = automation._locate_contact(mock_page, "好友A", entry=entry)
            self.assertTrue(ok, f"Failed with: {why}")
            mock_fiber.assert_called_once()


if __name__ == "__main__":
    unittest.main()
