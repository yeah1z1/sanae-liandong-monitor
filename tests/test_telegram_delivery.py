import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
import urllib.error
from unittest.mock import patch
import uuid

from shop_monitor import Monitor, send_telegram


class NoAI:
    def __init__(self, config, cache):
        pass

    def classify(self, items):
        return {}


class TelegramDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.json"
        self.config = {
            "shops": [], "poll_seconds": 20, "max_backoff_seconds": 600,
            "pending_ttl_seconds": 120, "onebot_priority_group_id": 17001,
            "onebot_all_group_id": 17002, "onebot_private_user_id": "17003",
            "onebot_url": "http://127.0.0.1:3002", "telegram_chat_id": "test-private-chat",
        }
        self.qq_calls = []
        self.all_qq_calls = []
        self.private_qq_calls = []
        self.telegram_calls = []
        self.monitor = self.make_monitor(self.config, self.path)

    def make_monitor(self, config, path):
        monitor = Monitor(config, path, sender=self.send_qq, all_group_sender=self.send_all_qq,
                          telegram_sender=self.send_telegram, fetcher=lambda shop, cfg: [],
                          classifier_type=NoAI)
        monitor.private_sender = self.send_private_qq
        return monitor

    def send_qq(self, config, text, *, mentions=()):
        self.qq_calls.append((text, tuple(mentions)))
        return 101 + len(self.qq_calls)

    def send_all_qq(self, config, text):
        self.all_qq_calls.append(text)
        return 201 + len(self.all_qq_calls)

    def send_private_qq(self, config, text):
        self.private_qq_calls.append(text)
        return 251 + len(self.private_qq_calls)

    def send_telegram(self, config, text):
        self.telegram_calls.append((config.get("telegram_chat_id"), text))
        return 301 + len(self.telegram_calls)

    @staticmethod
    def pending(alert_type="low_price_new", message_id="low-message"):
        return {
            "id": message_id, "text": "Low-price first listing", "created": time.time(),
            "attempts": 0, "alert_type": alert_type, "mentions": [],
            "group_sent": False, "all_group_sent": False, "private_sent": False,
            "telegram_receipts": {}, "events": [], "refs": {},
        }

    def test_low_price_alert_uses_only_telegram_even_with_qq_destinations_enabled(self):
        self.monitor.state["pending"] = [self.pending()]
        self.monitor.deliver(time.time())
        self.assertEqual(len(self.telegram_calls), 1)
        self.assertEqual(self.qq_calls, [])
        self.assertEqual(self.all_qq_calls, [])
        self.assertEqual(self.private_qq_calls, [])
        self.assertEqual(self.monitor.state["pending"], [])
        deliveries = self.monitor.state["deliveries"]
        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0]["id"], "low-message")
        self.assertEqual(deliveries[0]["recipient"], "telegram:test-private-chat")
        self.assertEqual(deliveries[0]["message_id"], 302)

    def test_missing_chat_id_skips_telegram_and_never_falls_back_to_qq(self):
        config = dict(self.config, telegram_chat_id="")
        monitor = self.make_monitor(config, self.path)
        monitor.state["pending"] = [self.pending()]
        monitor.deliver(time.time())
        self.assertEqual(self.telegram_calls, [])
        self.assertEqual(self.qq_calls, [])
        self.assertEqual(self.all_qq_calls, [])
        self.assertEqual(self.private_qq_calls, [])
        self.assertEqual(monitor.state["pending"], [])
        self.assertEqual(monitor.state["deliveries"], [])

    def test_other_alert_types_keep_existing_qq_routes_and_skip_telegram(self):
        entry = self.pending(alert_type="normal", message_id="normal-message")
        entry.update(group_sent=False, all_group_sent=False, private_sent=False)
        self.monitor.state["pending"] = [entry]
        self.monitor.deliver(time.time())
        self.assertEqual(self.telegram_calls, [])
        self.assertEqual(len(self.qq_calls), 1)
        self.assertEqual(len(self.all_qq_calls), 1)
        self.assertEqual(len(self.private_qq_calls), 1)
        self.assertEqual(self.monitor.state["pending"], [])
        recipients = {delivery["recipient"] for delivery in self.monitor.state["deliveries"]}
        self.assertEqual(recipients, {"group", "17002", "17003"})

    def test_failed_telegram_send_retries_then_records_only_real_receipt(self):
        entry = self.pending()
        self.monitor.state["pending"] = [entry]

        def fail(config, text):
            raise RuntimeError("sensitive synthetic text must not persist")

        self.monitor.telegram_sender = fail
        self.monitor.deliver(time.time())
        self.assertEqual(len(self.monitor.state["pending"]), 1)
        self.assertEqual(entry["attempts"], 1)
        self.assertEqual(entry["delivery_errors"], {"telegram:test-private-chat": "RuntimeError"})
        state_text = self.path.read_text(encoding="utf-8")
        self.assertNotIn("sensitive synthetic text", state_text)

        self.monitor.telegram_sender = self.send_telegram
        self.monitor.deliver(time.time())
        self.assertEqual(len(self.telegram_calls), 1)
        self.assertEqual(len(self.monitor.state["deliveries"]), 1)
        delivery = self.monitor.state["deliveries"][0]
        self.assertEqual(delivery["recipient"], "telegram:test-private-chat")
        self.assertEqual(delivery["message_id"], 302)
        self.assertEqual(self.monitor.state["pending"], [])
        self.assertEqual(self.qq_calls, [])

    def test_saved_recipient_receipt_prevents_resend_after_restart(self):
        self.monitor.state["pending"] = [self.pending()]
        original_save = self.monitor.save
        save_count = 0

        def crash_before_pending_clear_is_saved():
            nonlocal save_count
            save_count += 1
            if save_count == 2:
                raise RuntimeError("simulated process stop")
            original_save()

        self.monitor.save = crash_before_pending_clear_is_saved
        with self.assertRaisesRegex(RuntimeError, "simulated process stop"):
            self.monitor.deliver(time.time())
        self.assertEqual(len(self.telegram_calls), 1)

        restarted = self.make_monitor(self.config, self.path)
        restarted.deliver(time.time())
        self.assertEqual(len(self.telegram_calls), 1)
        self.assertEqual(restarted.state["pending"], [])
        self.assertEqual(len(restarted.state["deliveries"]), 1)
        self.assertEqual(restarted.state["deliveries"][0]["message_id"], 302)

    def test_send_telegram_posts_plain_text_over_fixed_https_api_and_caps_length(self):
        token = "unit-test-only-" + uuid.uuid4().hex
        class Opener:
            def open(inner, request, timeout):
                inner.url = request.full_url
                inner.method = request.get_method()
                inner.timeout = timeout
                inner.body = json.loads(request.data.decode("utf-8"))
                inner.content_type = request.get_header("Content-type")
                return io.BytesIO(b'{"ok":true,"result":{"message_id":987}}')

        opener = Opener()
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": token}):
            receipt = send_telegram(self.config, "x" * 5000, opener)
        self.assertEqual(receipt, 987)
        self.assertEqual(opener.method, "POST")
        self.assertEqual(opener.timeout, 10)
        self.assertTrue(opener.url.startswith("https://api.telegram.org/bot" + token + "/sendMessage"))
        self.assertEqual(opener.content_type, "application/json; charset=utf-8")
        self.assertEqual(opener.body["chat_id"], "test-private-chat")
        self.assertEqual(len(opener.body["text"]), 4096)
        self.assertTrue(opener.body["text"].endswith("…"))
        self.assertNotIn("parse_mode", opener.body)

    def test_http_and_bot_api_errors_are_sanitized(self):
        token = "unit-test-only-" + uuid.uuid4().hex
        class HttpFailure:
            def open(inner, request, timeout):
                raise urllib.error.HTTPError(request.full_url, 502, "failed", {}, io.BytesIO(b"bad"))

        class BotFailure:
            def open(inner, request, timeout):
                return io.BytesIO(b'{"ok":false,"description":"failed","error_code":400}')

        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": token}):
            with self.assertRaises(RuntimeError) as http_error:
                send_telegram(self.config, "safe text", HttpFailure())
            with self.assertRaises(RuntimeError) as bot_error:
                send_telegram(self.config, "safe text", BotFailure())
        for error in (http_error.exception, bot_error.exception):
            self.assertNotIn(token, str(error))
            self.assertNotIn("https://api.telegram.org", str(error))
        self.assertEqual(str(http_error.exception), "telegram_http_error")
        self.assertEqual(str(bot_error.exception), "telegram_business_failure")

    def test_missing_token_environment_variable_fails_without_request(self):
        class NeverOpen:
            def open(self, request, timeout):
                self.fail("must not perform a request")

        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": ""}):
            with self.assertRaisesRegex(RuntimeError, "telegram_bot_token_missing"):
                send_telegram(self.config, "safe text", NeverOpen())


if __name__ == "__main__":
    unittest.main()
