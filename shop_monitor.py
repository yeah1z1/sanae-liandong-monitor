"""Generic read-only shop monitor: independent worker, public catalog input and explicit QQ target."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
import urllib.error
import urllib.request

from monitor_core import rule_classify, make_events, render_messages
from deepseek_classifier import Classifier
from shop_source import fetch_shop as fetch_catalog_shop
from mdkj_source import fetch_shop as fetch_mdkj_shop
from priority_alert import qualifies, render_priority
from new_listing_alert import (DEFAULT_THRESHOLD_CNY, parse_threshold, qualifies_new_listing,
                               render_new_listings, select_new_listings)
from service_status import StatusClient, recommendation_line
from notification_config import (configured_mentions, onebot_api_base,
                                 optional_target_id, validate_notification_config)

LOG = logging.getLogger("shop-monitor")
CST = timezone(timedelta(hours=8))
NOTIFICATION_POLICY = "listed-or-last-five-generic-v2"


def fetch_shop(shop, config, *, opener=None):
    if shop.get("kind") == "mdkj":
        return fetch_mdkj_shop(shop, config, opener=opener)
    return fetch_catalog_shop(shop, config, opener=opener)


def item_signature(item):
    return [item.get(k) for k in ("price", "stock", "active", "target", "title", "description")]


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def load_state(path):
    if not Path(path).exists():
        return {"version": 1, "items": {}, "initialized_shops": [], "shop_health": {},
                "pending": [], "classifier_cache": {}, "deliveries": []}
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if value.get("version") != 1 or not isinstance(value.get("items"), dict):
        raise ValueError("invalid_state")
    return value


def _send_group_onebot(config, group_id, text, opener=None, *, mentions=()):
    # Product text is always a text segment; only explicitly configured mentions
    # on the priority route can create OneBot mention segments.
    priority_group = optional_target_id(config, "onebot_priority_group_id")
    all_group = optional_target_id(config, "onebot_all_group_id")
    if group_id not in (priority_group, all_group) or group_id is None:
        raise ValueError("onebot_group_not_configured")
    mentions = tuple(str(value) for value in mentions)
    if mentions:
        if group_id != priority_group or mentions != configured_mentions(config):
            raise ValueError("unauthorized_onebot_mentions")
    endpoint = onebot_api_base(config)
    segments = []
    for qq in mentions:
        segments.extend([{"type": "at", "data": {"qq": qq}}, {"type": "text", "data": {"text": " "}}])
    segments.append({"type": "text", "data": {"text": ("\n" if mentions else "") + text}})
    payload = {"group_id": group_id, "message": segments, "auto_escape": True}
    request = urllib.request.Request(endpoint + "/send_group_msg",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
    opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=10) as response:
        result = json.load(response)
    if result.get("status") != "ok" or result.get("retcode") not in (0, "0"):
        raise RuntimeError("onebot_business_failure")
    message_id = (result.get("data") or {}).get("message_id")
    if message_id is None:
        raise RuntimeError("onebot_missing_receipt")
    return message_id


def send_onebot(config, text, opener=None, *, mentions=()):
    """Send priority and optional normal alerts to the configured group."""
    target = optional_target_id(config, "onebot_priority_group_id")
    if target is None:
        raise ValueError("onebot_priority_group_not_configured")
    return _send_group_onebot(config, target, text, opener, mentions=mentions)


def send_all_group_onebot(config, text, opener=None):
    """Send real-time listing/last-five alerts to an optional second group."""
    target = optional_target_id(config, "onebot_all_group_id")
    if target is None:
        raise ValueError("onebot_all_group_not_configured")
    return _send_group_onebot(config, target, text, opener)


def send_private_onebot(config, text, opener=None):
    """Send a plain-text copy to an explicitly configured OneBot user."""
    target = optional_target_id(config, "onebot_private_user_id")
    if target is None:
        raise ValueError("onebot_private_user_not_configured")
    endpoint = onebot_api_base(config)
    payload = {"user_id": target, "message": text}
    request = urllib.request.Request(endpoint + "/send_private_msg",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
    opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=10) as response:
        result = json.load(response)
    if result.get("status") != "ok" or result.get("retcode") not in (0, "0"):
        raise RuntimeError("onebot_private_business_failure")
    message_id = (result.get("data") or {}).get("message_id")
    if message_id is None:
        raise RuntimeError("onebot_private_missing_receipt")
    return message_id


TELEGRAM_TOKEN_ENV_DEFAULT = "TELEGRAM_BOT_TOKEN"
TELEGRAM_MAX_MESSAGE_CHARS = 4096


def telegram_token_env_name(config):
    if "telegram_bot_token" in config:
        raise ValueError("telegram_bot_token_must_be_environment_variable")
    name = config.get("telegram_bot_token_env", TELEGRAM_TOKEN_ENV_DEFAULT)
    if not isinstance(name, str) or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None:
        raise ValueError("invalid_telegram_bot_token_env")
    return name


def log_telegram_configuration(config):
    """Warn without ever exposing a Telegram token or request URL."""
    if not str(config.get("telegram_chat_id", "")).strip():
        LOG.warning("telegram_low_price_alerts_disabled reason=missing_chat_id; no_qq_fallback=true")
        return
    try:
        env_name = telegram_token_env_name(config)
    except ValueError as exc:
        LOG.warning("telegram_low_price_alerts_unavailable reason=%s", str(exc))
        return
    if not os.environ.get(env_name):
        LOG.warning("telegram_low_price_alerts_unavailable reason=missing_token_environment_variable name=%s",
                    env_name)


class _NoTelegramRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def _cap_telegram_text(text):
    text = str(text)
    if len(text) <= TELEGRAM_MAX_MESSAGE_CHARS:
        return text
    return text[:TELEGRAM_MAX_MESSAGE_CHARS - 1] + "…"


def send_telegram(config, text, opener=None):
    """Send a plain-text Telegram private-chat message and return its real ID."""
    chat_id = str(config.get("telegram_chat_id", "")).strip()
    if not chat_id:
        raise RuntimeError("telegram_chat_id_not_configured")
    env_name = telegram_token_env_name(config)
    token = os.environ.get(env_name)
    if not token:
        raise RuntimeError("telegram_bot_token_missing")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": _cap_telegram_text(text)}
    request = urllib.request.Request(url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
    opener = opener or urllib.request.build_opener(_NoTelegramRedirects())
    try:
        with opener.open(request, timeout=10) as response:
            body = response.read()
    except urllib.error.HTTPError:
        raise RuntimeError("telegram_http_error") from None
    except Exception:
        # Do not propagate urllib exceptions: their string can include the
        # bot-token-bearing URL. The caller records only the exception type.
        raise RuntimeError("telegram_transport_error") from None
    try:
        result = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("telegram_invalid_response") from None
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise RuntimeError("telegram_business_failure")
    response_data = result.get("result")
    if not isinstance(response_data, dict):
        raise RuntimeError("telegram_missing_receipt")
    message_id = response_data.get("message_id")
    if type(message_id) is not int or message_id <= 0:
        raise RuntimeError("telegram_missing_receipt")
    return message_id


class Monitor:
    def __init__(self, config, state_path, *, sender=send_onebot, fetcher=fetch_shop,
                 classifier_type=Classifier, dry_run=False, config_path=None,
                 all_group_sender=send_all_group_onebot, telegram_sender=send_telegram):
        self.config = config
        self.config = config
        validate_notification_config(config)
        telegram_token_env_name(config)  # validate env-only secret configuration before startup
        self.new_listing_price_threshold = parse_threshold(
            config.get("new_listing_price_threshold_cny", DEFAULT_THRESHOLD_CNY))
        self.state_path = Path(state_path)
        self.state = load_state(state_path)
        if self.state.get("notification_policy") != NOTIFICATION_POLICY:
            self.state["suppressed_legacy_pending"] = len(self.state["pending"])
            self.state["pending"] = []
            self.state.pop("priority_seen", None)
            self.state["notification_policy"] = NOTIFICATION_POLICY
        # Old queued messages are suppressed during policy migration; messages
        # created before a second group target was configured are never replayed.
        for entry in self.state["pending"]:
            if "all_group_sent" not in entry:
                entry["all_group_sent"] = True
                entry["all_group_migration_suppressed"] = True
        self.sender, self.fetcher = sender, fetcher
        self.all_group_sender = all_group_sender
        self.telegram_sender = telegram_sender
        self.private_sender = send_private_onebot
        self.dry_run = dry_run
        self.classifier = classifier_type(config, self.state["classifier_cache"])
        self.status_client = StatusClient(config)
        self.openai_status = self.state.get("service_status", {})
        self.analytics = None
        self.analytics_config_path = config_path
        self.analytics_retry_after = 0
        self.reconcile_shop_roster(time.time())

    def save(self):
        atomic_json(self.state_path, self.state)

    def configured_shop_ids(self):
        return {shop["id"] for shop in self.config["shops"]}

    def reconcile_shop_roster(self, now):
        configured = self.configured_shop_ids()
        previous = set(self.state.get("notification_shop_ids", self.state["initialized_shops"]))
        if "notification_shop_ids" not in self.state:
            previous.update(item["shop_id"] for item in self.state["items"].values())
        removed = previous - configured
        if removed:
            pending, self.state["pending"] = self.state["pending"], []
            fresh = self.fresh_items(now)
            # Rebuild even entries whose events belong to retained shops: their
            # already-rendered minimum-price header may mention a removed shop.
            # Keep per-recipient receipts and original expiry when splitting.
            for old in pending:
                old_alert_type = old.get("alert_type", "priority" if old.get("mentions") else "normal")
                events = []
                for event in old.get("events", []):
                    item = self.state["items"].get(event["item_id"])
                    if (item and item["shop_id"] in configured
                            and event["kind"] in ("new", "low_stock")
                            and (old_alert_type != "low_price_new" or event["kind"] == "new")):
                        events.append({"kind": event["kind"], "item": item, "old": event.get("old")})
                urgent = [event for event in events if qualifies(event["item"])]
                urgent_ids = {event["item"]["id"] for event in urgent}
                start = len(self.state["pending"])
                self.enqueue(urgent, fresh, now, priority=True)
                remaining = [event for event in events if event["item"]["id"] not in urgent_ids]
                if old_alert_type == "low_price_new":
                    self.enqueue(remaining, fresh, now, low_price_new=True)
                else:
                    self.enqueue(remaining, fresh, now)
                for entry in self.state["pending"][start:]:
                    for field in ("created", "group_sent", "all_group_sent", "private_sent", "attempts",
                                  "attempts_by_recipient", "delivery_errors", "telegram_receipts"):
                        if field in old and (field != "telegram_receipts" or entry.get("alert_type") == old_alert_type):
                            entry[field] = old[field]
                    # Let deliver's normal refresh validate a changed catalog.
                    entry["refs"] = {key: old.get("refs", {}).get(key, signature)
                                     for key, signature in entry["refs"].items()}
            LOG.info("removed_shops=%s pending_rebuilt=%d", sorted(removed), len(self.state["pending"]))
        self.state["notification_shop_ids"] = sorted(configured)

    def fresh_items(self, now):
        configured = self.configured_shop_ids()
        return {key: item for key, item in self.state["items"].items()
                if item["shop_id"] in configured
                and not self.state["shop_health"].get(item["shop_id"], {}).get("error")
                and now - item.get("observed_epoch", 0) <= max(60, self.config["poll_seconds"] * 3)}

    def enqueue(self, events, fresh, now, *, priority=False, low_price_new=False, recovery=False):
        if events:
            stamp = datetime.fromtimestamp(now, CST).strftime("%m-%d %H:%M:%S")
            if priority:
                alert_type = "priority"
                messages = render_priority(events, stamp)
            elif low_price_new:
                alert_type = "low_price_new"
                messages = render_new_listings(events, fresh, stamp, self.new_listing_price_threshold)
            else:
                alert_type = "normal"
                messages = render_messages(events, fresh, stamp)
            group_enabled = optional_target_id(self.config, "onebot_priority_group_id") is not None
            all_group_enabled = optional_target_id(self.config, "onebot_all_group_id") is not None
            private_enabled = optional_target_id(self.config, "onebot_private_user_id") is not None
            mentions = configured_mentions(self.config) if priority and group_enabled else ()
            for text in messages:
                if self.config.get("service_status", {}).get("enabled", False):
                    text = text.rstrip() + "\n" + recommendation_line(self.openai_status)
                if recovery:
                    text = text.replace("— 新上架", "— 新上架·延迟送达")
                self.state["pending"].append({"text": text, "created": now,
                    "id": hashlib.sha256((str(now) + text).encode()).hexdigest()[:20], "attempts": 0,
                    "alert_type": alert_type,
                    "mentions": list(mentions),
                    "group_sent": not group_enabled,
                    "all_group_sent": not all_group_enabled,
                    "private_sent": not private_enabled, "telegram_receipts": {},
                    "events": [{"kind": e["kind"], "item_id": e["item"]["id"], "old": e.get("old")} for e in events],
                    "refs": {e["item"]["id"]: item_signature(e["item"]) for e in events}})
            LOG.info("events=%d messages=%d priority=%s low_price_new=%s", len(events), len(messages),
                     priority, low_price_new)

    def queue_events(self, previous, now, *, initialized_shop_ids=None):
        fresh = self.fresh_items(now)
        configured = self.configured_shop_ids()
        eligible_shops = set(initialized_shop_ids if initialized_shop_ids is not None
                             else self.state["initialized_shops"])
        allowed = [event for event in make_events(previous, self.state["items"], eligible_shops)
                   if event["item"]["shop_id"] in configured]
        priority = [e for e in allowed if e["item"]["id"] in fresh and qualifies(e["item"])]
        priority_ids = {e["item"]["id"] for e in priority}
        low_price_new = [e for e in select_new_listings(allowed, eligible_shops,
                                                        self.new_listing_price_threshold)
                         if e["item"]["id"] in fresh and e["item"]["id"] not in priority_ids]
        low_price_ids = {e["item"]["id"] for e in low_price_new}
        events = [e for e in allowed if e["item"]["id"] not in priority_ids | low_price_ids]
        self.enqueue(priority, fresh, now, priority=True)
        self.enqueue(low_price_new, fresh, now, low_price_new=True)
        self.enqueue(events, fresh, now)
        self.state["pending"] = self.state["pending"][-100:]
        self.save()
        return len(events) + len(priority) + len(low_price_new)

    def deliver(self, now):
        pending = self.state["pending"]
        ttl = self.config.get("pending_ttl_seconds", 120)
        refresh = any(now - e["created"] > ttl or any(
            item_signature(self.state["items"].get(key, {})) != sig
            for key, sig in e.get("refs", {}).items()) for e in pending)
        if refresh:
            ids = {key for e in pending for key in e.get("refs", {})}
            fresh = self.fresh_items(now)
            # A failed catalog cannot safely refresh an old purchase alert.
            if any(key not in fresh for key in ids):
                return
            queued = {}
            for entry in pending:
                alert_type = entry.get("alert_type", "priority" if entry.get("mentions") else "normal")
                for event in entry.get("events", []):
                    queued[(event["item_id"], event["kind"], alert_type)] = event
            events, low_price_events = [], []
            for (key, kind, alert_type), event in queued.items():
                item = fresh.get(key, {})
                stock = item.get("stock")
                if not item.get("active") or not item.get("target") or stock == 0:
                    continue
                if kind == "low_stock" and not (type(stock) is int and 1 <= stock <= 5):
                    continue
                if kind not in ("new", "low_stock"):
                    continue
                refreshed = {"kind": kind, "item": item, "old": event.get("old")}
                if alert_type == "low_price_new":
                    if kind == "new" and (qualifies_new_listing(item, self.new_listing_price_threshold)
                                            or qualifies(item)):
                        low_price_events.append(refreshed)
                    continue
                events.append(refreshed)
            old_pending = pending
            self.state["pending"] = []
            priority_events = [e for e in events + low_price_events if qualifies(e["item"])]
            priority_ids = {e["item"]["id"] for e in priority_events}
            low_price_new = [e for e in low_price_events if e["item"]["id"] not in priority_ids]
            normal_events = [e for e in events if e["item"]["id"] not in priority_ids]
            self.enqueue(priority_events, fresh, now, priority=True, recovery=True)
            self.enqueue(low_price_new, fresh, now, low_price_new=True, recovery=True)
            self.enqueue(normal_events, fresh, now, recovery=True)
            for entry in self.state["pending"]:
                matches = [old for old in old_pending if set(entry.get("refs", {})) & set(old.get("refs", {}))]
                if matches:
                    entry["group_sent"] = all(old.get("group_sent", False) for old in matches)
                    entry["all_group_sent"] = all(old.get("all_group_sent", True) for old in matches)
                    entry["private_sent"] = all(old.get("private_sent", False) for old in matches)
                    if entry.get("alert_type") == "low_price_new":
                        receipts = {}
                        for old in matches:
                            if old.get("alert_type") == "low_price_new":
                                receipts.update(old.get("telegram_receipts", {}))
                        entry["telegram_receipts"] = receipts
            self.save()
        group_target = optional_target_id(self.config, "onebot_priority_group_id")
        all_group_target = optional_target_id(self.config, "onebot_all_group_id")
        private_target = optional_target_id(self.config, "onebot_private_user_id")
        group_enabled = group_target is not None
        private_enabled = private_target is not None
        all_group_enabled = all_group_target is not None
        group_priority_only = bool(self.config.get("onebot_priority_only", False))
        index = 0
        while index < len(self.state["pending"]):
            entry = self.state["pending"][index]
            if now - entry["created"] > self.config.get("pending_ttl_seconds", 120):
                LOG.warning("expired_notification id=%s", entry["id"])
                self.state["pending"].pop(index)
                self.save()
                continue
            if entry.get("alert_type") == "low_price_new":
                if self.dry_run:
                    print(entry["text"], flush=True)
                    self.state["pending"].pop(index)
                    self.save()
                    continue
                chat_id = str(self.config.get("telegram_chat_id", "")).strip()
                if not chat_id:
                    LOG.warning("telegram_notification_skipped id=%s reason=missing_chat_id no_qq_fallback=true",
                                entry.get("id", "unknown"))
                    self.state["pending"].pop(index)
                    self.save()
                    continue
                recipient = "telegram:" + chat_id
                receipt = (entry.get("telegram_receipts") or {}).get(recipient)
                if type(receipt) is not int or receipt <= 0:
                    receipt = next((delivery.get("message_id") for delivery in reversed(self.state["deliveries"])
                                    if delivery.get("id") == entry.get("id")
                                    and delivery.get("recipient") == recipient
                                    and type(delivery.get("message_id")) is int
                                    and delivery["message_id"] > 0), None)
                if type(receipt) is int and receipt > 0:
                    entry.setdefault("telegram_receipts", {})[recipient] = receipt
                    self.state["pending"].pop(index)
                    self.save()
                    continue
                try:
                    receipt = self.telegram_sender(self.config, entry["text"])
                    if type(receipt) is not int or receipt <= 0:
                        raise RuntimeError("telegram_invalid_receipt")
                except Exception as exc:
                    self._record_delivery_failure(entry, recipient, exc)
                    index += 1
                else:
                    self._record_delivery(entry, recipient, receipt, now, [])
                    self.state["pending"].pop(index)
                    self.save()
                continue
            if not group_enabled:
                entry["group_sent"] = True
            if not all_group_enabled:
                entry["all_group_sent"] = True
            if not private_enabled:
                entry["private_sent"] = True
            if self.dry_run:
                if (not entry.get("group_sent") or
                        (all_group_enabled and not entry.get("all_group_sent")) or
                        (private_enabled and not entry.get("private_sent"))):
                    print(entry["text"], flush=True)
                if not entry.get("group_sent"):
                    entry["group_sent"] = True
                    if group_priority_only and not entry.get("mentions"):
                        entry["group_suppressed"] = True
                if all_group_enabled:
                    entry["all_group_sent"] = True
                if private_enabled:
                    entry["private_sent"] = True
            else:
                if not entry.get("group_sent"):
                    if group_priority_only and not entry.get("mentions"):
                        entry["group_sent"] = True
                        entry["group_suppressed"] = True
                        LOG.info("group_suppressed id=%s reason=priority_only", entry["id"])
                        self.save()
                    else:
                        try:
                            if entry.get("mentions"):
                                receipt = self.sender(self.config, entry["text"],
                                                      mentions=tuple(entry["mentions"]))
                            else:
                                receipt = self.sender(self.config, entry["text"])
                        except Exception as exc:
                            self._record_delivery_failure(entry, "group", exc)
                        else:
                            entry["group_sent"] = True
                            self._record_delivery(entry, "group", receipt, now,
                                                  entry.get("mentions", []))
                if all_group_enabled and not entry.get("all_group_sent"):
                    try:
                        receipt = self.all_group_sender(self.config, entry["text"])
                    except Exception as exc:
                        self._record_delivery_failure(entry, str(all_group_target), exc)
                    else:
                        entry["all_group_sent"] = True
                        self._record_delivery(entry, str(all_group_target), receipt, now, [])
                if private_enabled and not entry.get("private_sent"):
                    try:
                        receipt = self.private_sender(self.config, entry["text"])
                    except Exception as exc:
                        self._record_delivery_failure(entry, str(private_target), exc)
                    else:
                        entry["private_sent"] = True
                        self._record_delivery(entry, str(private_target), receipt, now, [])

            complete = (entry.get("group_sent", False)
                        and entry.get("all_group_sent", not all_group_enabled)
                        and (entry.get("private_sent", False) or not private_enabled))
            if complete:
                self.state["pending"].pop(index)
                LOG.info("delivered id=%s group=%s all_group=%s private=%s", entry["id"],
                         entry.get("group_sent"), entry.get("all_group_sent", not all_group_enabled),
                         entry.get("private_sent", not private_enabled))
                self.save()
                continue
            index += 1
        if not any(entry.get("delivery_errors") for entry in self.state["pending"]):
            self.state.pop("delivery_error", None)
            self.save()

    def _record_delivery(self, entry, recipient, receipt, now, mentions):
        errors = entry.get("delivery_errors", {})
        errors.pop(recipient, None)
        if errors:
            entry["delivery_errors"] = errors
        else:
            entry.pop("delivery_errors", None)
        if recipient.startswith("telegram:"):
            entry.setdefault("telegram_receipts", {})[recipient] = receipt
        self.state["deliveries"].append({"id": entry["id"], "recipient": recipient,
                                          "message_id": receipt, "sent_at": now,
                                          "mentions": list(mentions)})
        self.state["deliveries"] = self.state["deliveries"][-400:]
        self.save()

    def _record_delivery_failure(self, entry, recipient, exc):
        error = type(exc).__name__
        entry["attempts"] = entry.get("attempts", 0) + 1
        attempts = entry.setdefault("attempts_by_recipient", {})
        attempts[recipient] = attempts.get(recipient, 0) + 1
        entry.setdefault("delivery_errors", {})[recipient] = error
        self.state["delivery_error"] = error
        LOG.warning("delivery_failed recipient=%s type=%s", recipient, error)
        self.save()

    def poll(self, *, use_ai=True):
        now = time.time()
        initialized_before_poll = set(self.state["initialized_shops"])
        previous = copy.deepcopy(self.state["items"])
        due = [shop for shop in self.config["shops"]
               if self.state["shop_health"].get(shop["id"], {}).get("next_attempt", 0) <= now]
        successful = []
        uncertain = []
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(self.fetcher, shop, self.config): shop for shop in due}
            for future in as_completed(futures):
                shop = futures[future]
                sid = shop["id"]
                health = self.state["shop_health"].setdefault(sid, {})
                try:
                    items = future.result()
                    if not isinstance(items, list):
                        raise ValueError("invalid_catalog")
                    incoming = {item["id"]: item for item in items}
                    if len(incoming) != len(items):
                        raise ValueError("duplicate_catalog_id")
                    staged = {}
                    shop_uncertain = []
                    for key, old in list(self.state["items"].items()):
                        if old["shop_id"] == sid and key not in incoming:
                            staged[key] = dict(old, active=False, observed_epoch=now)
                    for key, item in incoming.items():
                        if item["shop_id"] != sid or not isinstance(item["title"], str):
                            raise ValueError("invalid_product_identity")
                        item["observed_epoch"] = now
                        result = rule_classify(item["title"])
                        old = previous.get(key, {})
                        if result is None:
                            item["target"] = bool(old.get("target"))
                            item["classification_pending"] = bool(old.get("classification_pending")) or (
                                old.get("title") != item["title"] or old.get("description") != item.get("description"))
                            shop_uncertain.append(item)
                        else:
                            item["target"] = result
                            item["classification_pending"] = False
                        staged[key] = item
                    self.state["items"].update(staged)
                    uncertain.extend(shop_uncertain)
                    health.update({"last_success": now, "failures": 0, "error": None,
                                   "next_attempt": now + self.config["poll_seconds"], "count": len(items)})
                    successful.append(sid)
                except Exception as exc:
                    failures = health.get("failures", 0) + 1
                    wait = min(self.config["max_backoff_seconds"], self.config["poll_seconds"] * 2 ** min(failures, 6))
                    health.update({"error": type(exc).__name__, "failures": failures,
                                   "next_attempt": now + wait, "last_failure": now})
                    LOG.warning("shop_failed shop=%s type=%s retry=%ss", sid, type(exc).__name__, wait)
        self.state["last_poll"] = now
        self.refresh_service_status(now)
        count = self.queue_events(previous, now, initialized_shop_ids=initialized_before_poll)
        self.state["initialized_shops"] = sorted(set(self.state["initialized_shops"]) | set(successful))
        self.save()
        self.deliver(time.time())
        # Clear targets are dispatched first: model latency cannot delay those alerts.
        if use_ai and uncertain:
            before_ai = copy.deepcopy(self.state["items"])
            uncertain.sort(key=lambda item: (not bool(item.get("stock")), item["id"]))
            results = self.classifier.classify(uncertain)
            for key, target in results.items():
                if key in self.state["items"] and type(target) is bool:
                    self.state["items"][key]["target"] = target
                    self.state["items"][key]["classification_pending"] = False
            count += self.queue_events(before_ai, time.time(), initialized_shop_ids=initialized_before_poll)
            self.deliver(time.time())
        if self.config.get("analytics", {}).get("enabled") and not self.dry_run and time.time() >= self.analytics_retry_after:
            try:
                if self.analytics is None:
                    from analytics_jobs import AnalyticsJobs
                    self.analytics = AnalyticsJobs(self.config, self.state_path.parent, self.analytics_config_path)
                self.analytics.capture(self.state["items"], self.config["shops"], successful,
                    [shop["id"] for shop in due], self.state["shop_health"], now,
                    service_status=self.openai_status)
                self.state.pop("analytics_error", None)
            except Exception as exc:
                self.state["analytics_error"] = type(exc).__name__
                self.analytics_retry_after = time.time() + 60
                LOG.warning("analytics_failed type=%s", type(exc).__name__)
        self.state["last_complete"] = time.time()
        self.save()
        LOG.info("poll shops=%d/%d targets=%d events=%d pending=%d", len(successful), len(due),
                 sum(1 for item in self.state["items"].values() if item.get("target") and item.get("active")),
                 count, len(self.state["pending"]))
        return {"shops_ok": len(successful), "shops_due": len(due), "events": count,
                "pending": len(self.state["pending"]), "dry_run": self.dry_run}

    def refresh_service_status(self, now):
        if not self.config.get("service_status", {}).get("enabled", False):
            return
        try:
            self.openai_status = self.status_client.poll(now)
            self.state["service_status"] = self.openai_status
            self.save()
        except Exception as exc:
            # Status is a conservative purchase hint, never a reason to drop
            # a shop event. The client itself normally converts failures into
            # an explicit unknown/failed result.
            LOG.warning("service_status_failed type=%s", type(exc).__name__)
            self.openai_status = {"openai": {"ok": False, "error": type(exc).__name__,
                                              "description": "状态采集失败", "indicator": "unknown"}}
            self.state["service_status"] = self.openai_status
            self.save()


def instance_lock(path):
    # OS releases this lock on crash; no stale PID deletion required.
    stream = open(path, "a+b")
    stream.seek(0)
    if not stream.read(1):
        stream.write(b"0")
        stream.flush()
    stream.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    return stream


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(Path(__file__).with_name("config.json")))
    parser.add_argument("--state")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-ai", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    config = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
    if not isinstance(config, dict) or not isinstance(config.get("shops"), list) or not config["shops"]:
        raise ValueError("configure at least one shop in the mounted config before startup")
    poll_seconds = config.get("poll_seconds", 20)
    if isinstance(poll_seconds, bool) or not isinstance(poll_seconds, (int, float)) or poll_seconds < 15:
        raise ValueError("poll_seconds_must_be_at_least_15")
    validate_notification_config(config)
    state_path = Path(args.state or root / "state" / ("dry-run.json" if args.dry_run else "monitor.json"))
    state_path.parent.mkdir(parents=True, exist_ok=True)
    log_dir = state_path.parent
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handler.setFormatter(formatter)
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    LOG.setLevel(logging.INFO)
    LOG.addHandler(handler)
    LOG.addHandler(console)
    logging.getLogger("deepseek_classifier").addHandler(handler)
    logging.getLogger("deepseek_classifier").addHandler(console)
    try:
        lock = instance_lock(state_path.with_suffix(".lock"))
    except OSError:
        print("monitor already running", flush=True)
        return
    with lock:
        log_telegram_configuration(config)
        monitor = Monitor(config, state_path, dry_run=args.dry_run, config_path=Path(args.config).resolve())
        atomic_json(state_path.parent / "process.json", {"pid": os.getpid(), "start": time.time(), "source": str(root)})
        while True:
            started = time.monotonic()
            result = monitor.poll(use_ai=not args.no_ai)
            if args.once:
                print(json.dumps(result, ensure_ascii=False), flush=True)
                if result["shops_ok"] != result["shops_due"]:
                    raise SystemExit(2)
                return
            time.sleep(max(1, config["poll_seconds"] - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
