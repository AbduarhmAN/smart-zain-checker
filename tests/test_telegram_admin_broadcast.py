"""Tests for Telegram Admin Group Broadcast & Management.
Verifies that completion messages and result files are dispatched to all admin groups & targets.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from telegram_bot import config
from telegram_bot.notifier import TelegramNotifier
from telegram_bot.bot import TelegramBotRunner


class TestAdminGroupConfig:
    def test_settings_parses_admin_groups_and_targets(self, tmp_path, monkeypatch):
        cfg_file = tmp_path / ".zain-telegram-local.json"
        cfg_file.write_text(
            json.dumps({
                "bot_token": "test:token",
                "chat_id": 1001,
                "authorized_id": 1001,
                "authorized_ids": [1001, 2002],
                "admin_groups": [-100999999]
            }),
            encoding="utf-8"
        )
        monkeypatch.setattr(config, "LOCAL_SETTINGS", cfg_file)

        settings = config.settings()
        assert settings["chat_id"] == 1001
        assert 1001 in settings["authorized_ids"]
        assert 2002 in settings["authorized_ids"]
        assert -100999999 in settings["admin_groups"]
        assert settings["admin_targets"] == [1001, 2002, -100999999]

    def test_add_and_remove_admin_group(self, tmp_path, monkeypatch):
        cfg_file = tmp_path / ".zain-telegram-local.json"
        cfg_file.write_text(
            json.dumps({"bot_token": "test", "chat_id": 1001, "authorized_ids": [1001]}),
            encoding="utf-8"
        )
        monkeypatch.setattr(config, "LOCAL_SETTINGS", cfg_file)

        assert config.add_admin_group(-100888888) is True
        s1 = config.settings()
        assert -100888888 in s1["admin_groups"]
        assert -100888888 in s1["admin_targets"]

        assert config.remove_admin_group(-100888888) is True
        s2 = config.settings()
        assert -100888888 not in s2["admin_groups"]
        assert -100888888 not in s2["admin_targets"]


class TestTelegramNotifierBroadcast:
    @pytest.fixture
    def mock_settings(self, monkeypatch):
        def _get():
            return {
                "bot_token": "fake_token",
                "chat_id": 1001,
                "authorized_id": 1001,
                "authorized_ids": [1001, 2002],
                "admin_groups": [-100999999],
                "admin_targets": [1001, 2002, -100999999],
            }
        monkeypatch.setattr("telegram_bot.notifier.settings", _get)

    def test_session_started_broadcasts_to_all_admin_targets(self, mock_settings):
        notifier = TelegramNotifier(bot_token="fake_token", chat_id=1001)
        sent_messages = []
        notifier.send_message = MagicMock(side_effect=lambda text, **kwargs: sent_messages.append((text, kwargs.get("chat_id"))))

        payload = {
            "workbook": "customers.xlsx",
            "total_records": 500,
            "telegram_chat_id": 5555,  # customer
        }
        notifier._on_session_started("session_started", payload)

        targets_notified = [cid for _, cid in sent_messages]
        # Admin targets: 1001, 2002, -100999999
        assert 1001 in targets_notified
        assert 2002 in targets_notified
        assert -100999999 in targets_notified
        # Customer owner 5555 should also receive start notice
        assert 5555 in targets_notified

    def test_session_completed_round_pending_broadcasts_message_and_result_file(self, tmp_path, mock_settings):
        notifier = TelegramNotifier(bot_token="fake_token", chat_id=1001)
        sent_messages = []
        sent_documents = []
        notifier.send_message = MagicMock(side_effect=lambda text, **kwargs: sent_messages.append((text, kwargs.get("chat_id"))))
        notifier.send_excel_document = MagicMock(side_effect=lambda fpath, **kwargs: sent_documents.append((fpath, kwargs.get("chat_id"), kwargs.get("caption"))))

        res_file = tmp_path / "result_part1.xlsx"
        res_file.write_bytes(b"dummy_content")

        payload = {
            "workbook": "part1.xlsx",
            "total": 100,
            "matches": 90,
            "mismatches": 5,
            "needs_review": 3,
            "not_found": 2,
            "errors": 0,
            "result_file": str(res_file),
            "round_pending": 1,
            "telegram_chat_id": 5555,
        }
        notifier._on_session_completed("session_completed", payload)

        msg_targets = [cid for _, cid in sent_messages]
        doc_targets = [cid for _, cid, _ in sent_documents]

        # Both the completion message AND the result file must reach all admin targets
        for expected in [1001, 2002, -100999999]:
            assert expected in msg_targets, f"Admin target {expected} missing completion message"
            assert expected in doc_targets, f"Admin target {expected} missing result file document"

        # Customer 5555 also receives their file
        assert 5555 in msg_targets
        assert 5555 in doc_targets

    def test_session_completed_final_broadcasts_message_and_all_result_files(self, tmp_path, mock_settings):
        notifier = TelegramNotifier(bot_token="fake_token", chat_id=1001)
        sent_messages = []
        sent_documents = []
        notifier.send_message = MagicMock(side_effect=lambda text, **kwargs: sent_messages.append((text, kwargs.get("chat_id"))))
        notifier.send_excel_document = MagicMock(side_effect=lambda fpath, **kwargs: sent_documents.append((fpath, kwargs.get("chat_id"), kwargs.get("caption"))))

        f1 = tmp_path / "res1.xlsx"
        f1.write_bytes(b"excel1")
        f2 = tmp_path / "res2.xlsx"
        f2.write_bytes(b"excel2")

        payload = {
            "workbook": "batch.xlsx",
            "total": 200,
            "matches": 190,
            "mismatches": 10,
            "needs_review": 0,
            "not_found": 0,
            "errors": 0,
            "result_file": str(f2),
            "round_pending": 0,
            "round_result_files": [str(f1), str(f2)],
        }
        notifier._on_session_completed("session_completed", payload)

        msg_targets = [cid for _, cid in sent_messages]
        doc_deliveries = [(fpath.name, cid) for fpath, cid, _ in sent_documents]

        for expected in [1001, 2002, -100999999]:
            assert expected in msg_targets, f"Admin target {expected} missing final summary message"
            assert ("res1.xlsx", expected) in doc_deliveries, f"Admin target {expected} missing res1.xlsx"
            assert ("res2.xlsx", expected) in doc_deliveries, f"Admin target {expected} missing res2.xlsx"


class TestBotGroupCommands:
    @pytest.fixture
    def bot(self, tmp_path):
        runner = TelegramBotRunner(
            orchestrator=MagicMock(),
            queue_service=MagicMock(),
            project_root=tmp_path,
            bot_token="mock_token",
            authorized_id=1001,
            authorized_ids=[1001, 2002],
        )
        runner._reply = MagicMock()
        return runner

    def test_group_id_command_replies_to_anyone(self, bot):
        update = {
            "message": {
                "from": {"id": 7777, "is_bot": False},
                "chat": {"id": -1001234567, "type": "supergroup"},
                "text": "/id",
            }
        }
        bot._handle_update(update)
        bot._reply.assert_called_once()
        assert "-1001234567" in bot._reply.call_args[0][1]

    def test_group_set_admin_group_rejected_for_non_admin(self, bot):
        update = {
            "message": {
                "from": {"id": 8888, "is_bot": False},  # not in authorized_ids
                "chat": {"id": -1001234567, "type": "supergroup"},
                "text": "/set_admin_group",
            }
        }
        bot._handle_update(update)
        bot._reply.assert_not_called()

    def test_group_set_admin_group_accepted_for_admin(self, bot, monkeypatch):
        added = []
        monkeypatch.setattr("telegram_bot.config.add_admin_group", lambda gid: added.append(gid) or True)

        update = {
            "message": {
                "from": {"id": 1001, "is_bot": False},  # admin
                "chat": {"id": -1001234567, "type": "supergroup"},
                "text": "/set_admin_group",
            }
        }
        bot._handle_update(update)
        assert -1001234567 in added
        bot._reply.assert_called_once()
        assert "تم ربط هذه المجموعة" in bot._reply.call_args[0][1]
