"""Validation helpers for optional, local OneBot notification targets."""
from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

DEFAULT_ONEBOT_URL = "http://127.0.0.1:3002"
TARGET_FIELDS = (
    "onebot_priority_group_id",
    "onebot_all_group_id",
    "onebot_private_user_id",
)


def optional_target_id(config, key):
    """Return a positive numeric target ID, or None when that route is disabled."""
    value = config.get(key)
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("invalid_" + key)
    if isinstance(value, int):
        target = value
    elif isinstance(value, str) and re.fullmatch(r"[0-9]{1,20}", value.strip()):
        target = int(value.strip())
    else:
        raise ValueError("invalid_" + key)
    if target <= 0:
        raise ValueError("invalid_" + key)
    return target


def configured_mentions(config):
    value = config.get("onebot_priority_mentions", [])
    if not isinstance(value, list):
        raise ValueError("invalid_onebot_priority_mentions")
    result = []
    for item in value:
        if isinstance(item, bool):
            raise ValueError("invalid_onebot_priority_mentions")
        if isinstance(item, int) and item > 0:
            result.append(str(item))
        elif isinstance(item, str) and re.fullmatch(r"[0-9]{1,20}", item.strip()):
            result.append(item.strip())
        else:
            raise ValueError("invalid_onebot_priority_mentions")
    return tuple(result)


def onebot_api_base(config):
    """Allow loopback OneBot or the conventional private Compose sidecar alias."""
    value = config.get("onebot_url", DEFAULT_ONEBOT_URL)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("invalid_onebot_endpoint")
    try:
        parsed = urlsplit(value.strip())
        hostname = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        raise ValueError("invalid_onebot_endpoint") from None
    if (parsed.scheme != "http" or not hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
        raise ValueError("invalid_onebot_endpoint")
    is_loopback = hostname == "localhost"
    try:
        is_loopback = is_loopback or ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        pass
    if not is_loopback and hostname != "onebot":
        raise ValueError("onebot_endpoint_must_be_local")
    return f"http://{parsed.netloc}" if port else f"http://{hostname}"


def validate_notification_config(config):
    targets = {key: optional_target_id(config, key) for key in TARGET_FIELDS}
    mentions = configured_mentions(config)
    if (targets["onebot_priority_group_id"] is not None
            and targets["onebot_priority_group_id"] == targets["onebot_all_group_id"]):
        raise ValueError("onebot_group_targets_must_be_distinct")
    if mentions and targets["onebot_priority_group_id"] is None:
        raise ValueError("onebot_mentions_require_priority_group")
    if any(target is not None for target in targets.values()):
        onebot_api_base(config)
