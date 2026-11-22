from pathlib import Path
import tempfile
import unittest

from shop_monitor import Monitor


class NoAI:
    def __init__(self, config, cache):
        pass

    def classify(self, items):
        return {item["id"]: True for item in items}


def product(item_id="sample-item", **changes):
    item = {"id": item_id, "shop_id": "sample-shop", "shop_name": "sample shop",
            "title": "team5x 3h速刷 listing", "description": "", "price": "19.99",
            "stock": 3, "url": "https://example.test/item/" + item_id,
            "active": True, "target": True}
    item.update(changes)
    return item


class NewListingRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.json"
        self.catalog = []
        self.messages = []
        self.telegram_messages = []
        self.config = {"shops": [{"id": "sample-shop"}], "poll_seconds": 20,
                       "max_backoff_seconds": 600, "pending_ttl_seconds": 120,
                       "onebot_priority_group_id": 7, "onebot_priority_mentions": ["7001"],
                       "onebot_url": "http://127.0.0.1:3002",
                       "telegram_chat_id": "test-private-chat"}
        self.monitor = Monitor(self.config, self.path, sender=self.send, telegram_sender=self.send_telegram,
                               fetcher=lambda shop, config: [dict(item) for item in self.catalog],
                               classifier_type=NoAI)

    def send(self, config, message, *, mentions=()):
        self.messages.append((message, tuple(mentions)))
        return len(self.messages)

    def send_telegram(self, config, message):
        self.telegram_messages.append(message)
        return len(self.telegram_messages)

    def poll(self):
        for health in self.monitor.state["shop_health"].values():
            health["next_attempt"] = 0
        return self.monitor.poll()

    def test_initial_catalog_is_baseline_and_existing_price_drop_is_silent(self):
        self.catalog = [product(price="19.99")]
        self.poll()
        self.assertEqual(len(self.messages), 1)  # unchanged legacy generic baseline event
        self.assertNotIn("低价新上架", self.messages[0][0])
        self.poll()
        self.assertEqual(len(self.messages), 1)

        self.catalog = [product(price="24.00")]
        self.poll()
        self.catalog = [product(price="19.99")]
        self.poll()
        self.assertEqual(len(self.messages), 1)  # price-only change is not a new listing

    def test_new_listing_fires_once_and_actual_relisting_fires_once_again(self):
        self.poll()  # successful empty baseline
        self.catalog = [product()]
        self.poll()
        self.assertEqual(self.messages, [])
        self.assertEqual(len(self.telegram_messages), 1)
        first_text = self.telegram_messages[0]
        self.assertIn("低价新上架", first_text)
        self.assertIn("严格低于¥20", first_text)

        self.poll()
        self.assertEqual(len(self.telegram_messages), 1)
        self.catalog = []
        self.poll()  # confirmed absence marks the item inactive
        self.catalog = [product()]
        self.poll()  # existing event model reports a relisting
        self.assertEqual(len(self.telegram_messages), 2)
        self.assertIn("低价新上架", self.telegram_messages[1])
        self.assertEqual(self.messages, [])
        self.poll()
        self.assertEqual(len(self.telegram_messages), 2)

    def test_failed_low_alert_refresh_keeps_its_type_and_latest_facts(self):
        self.poll()
        self.catalog = [product()]

        def fail(config, message, *, mentions=()):
            raise RuntimeError("offline test delivery failure")

        self.monitor.telegram_sender = fail
        self.poll()
        self.assertEqual(len(self.monitor.state["pending"]), 1)
        self.assertEqual(self.monitor.state["pending"][0]["alert_type"], "low_price_new")
        self.catalog = [product(price="19.50")]
        self.monitor.telegram_sender = self.send_telegram
        self.poll()
        self.assertEqual(len(self.telegram_messages), 1)
        self.assertEqual(self.messages, [])
        text = self.telegram_messages[0]
        self.assertIn("低价新上架", text)
        self.assertIn("新上架·延迟送达", text)
        self.assertIn("¥19.5", text)
        self.assertEqual(self.monitor.state["pending"], [])

    def test_custom_threshold_is_used(self):
        self.config["new_listing_price_threshold_cny"] = 15
        self.monitor = Monitor(self.config, self.path, sender=self.send, telegram_sender=self.send_telegram,
                               fetcher=lambda shop, config: [dict(item) for item in self.catalog],
                               classifier_type=NoAI)
        self.poll()
        self.catalog = [product(price="14.99")]
        self.poll()
        self.assertEqual(len(self.telegram_messages), 1)
        self.assertEqual(self.messages, [])
        self.assertIn("严格低于¥15", self.telegram_messages[0])
        self.assertNotIn("2FA", self.telegram_messages[0])

    def test_legacy_below_forty_twofa_alert_keeps_precedence_without_duplicate(self):
        self.poll()
        self.catalog = [product(title="team5x 3h速刷 2FA", price="19.99")]
        self.poll()
        self.assertEqual(len(self.messages), 1)
        text, mentions = self.messages[0]
        self.assertIn("低价2FA速刷提醒", text)
        self.assertNotIn("低价新上架", text)
        self.assertTrue(mentions)

    def test_first_scan_of_a_new_shop_is_its_baseline(self):
        self.poll()  # empty baseline for the shop
        second_shop = {"id": "new-sample-shop"}
        self.config["shops"].append(second_shop)
        self.monitor.fetcher = lambda shop, config: [dict(item) for item in self.catalog
                                                     if item["shop_id"] == shop["id"]]
        self.catalog = [product("new-sample-item", shop_id="new-sample-shop", price="19.99")]
        self.poll()
        self.assertEqual(len(self.messages), 1)
        self.assertNotIn("低价新上架", self.messages[0][0])
        self.poll()
        self.assertEqual(len(self.messages), 1)


if __name__ == "__main__":
    unittest.main()
