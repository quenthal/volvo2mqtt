"""Measures how fresh Volvo data is: age of the newest field timestamp per endpoint. Logs no values."""
import logging
import re
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

REPORT_INTERVAL = 3600
TIMESTAMP_KEYS = ("timestamp", "updatedAt")
# The server stamps statistics at request time; their timestamp says nothing about the car.
SERVER_STAMPED = ("statistics",)


def newest_timestamp(payload):
    """Newest ISO-8601 'timestamp' anywhere in the payload, as epoch seconds."""
    newest = None
    stack = [payload]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for key, value in item.items():
                if key in TIMESTAMP_KEYS and isinstance(value, str):
                    try:
                        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    if stamp.tzinfo is None:
                        stamp = stamp.replace(tzinfo=timezone.utc)
                    newest = max(newest or stamp.timestamp(), stamp.timestamp())
                elif isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(item, list):
            stack.extend(item)
    return newest


SAFE_KEY = re.compile(r"[a-z][A-Za-z]{0,39}")


def _key(key):
    # Schema keys are camelCase words; anything else (ids, VIN-like strings) is masked.
    return key if isinstance(key, str) and SAFE_KEY.fullmatch(key) else "<key>"


def structure(payload, depth=0):
    """Key names of a payload, two levels deep, without any values."""
    if isinstance(payload, dict):
        if depth >= 2:return "{" + ",".join(sorted(_key(key) for key in payload)) + "}"
        return "{" + ",".join(f"{_key(key)}:{structure(value, depth + 1)}" if isinstance(value, (dict, list)) else _key(key)
                              for key, value in sorted(payload.items(), key=lambda item: str(item[0]))) + "}"
    if isinstance(payload, list):
        return "[" + (structure(payload[0], depth + 1) if payload else "") + "]"
    return ""


def label(url):
    path = urlsplit(url).path.rstrip("/")
    parts = path.split("/")
    if parts and parts[-1] == "state" and "/energy/" in path:
        return "energy"
    if parts and parts[-1].startswith("{"):
        return "vehicle"
    return parts[-1] if parts else path


class FreshnessStats:
    def __init__(self, interval=REPORT_INTERVAL, clock=time.time):
        self.interval, self.clock = interval, clock
        self.lock = threading.Lock()
        self.samples = {}
        self.previous = {}
        self.started = clock()
        self.structure_logged = set()

    def record(self, url, vin, payload, received=None):
        stamp = newest_timestamp(payload)
        now = self.clock()
        received = now if received is None else received
        with self.lock:
            name = label(url)
            ages, counts = self.samples.setdefault(name, ([], {"new": 0, "untimed": 0}))
            if stamp is None:
                counts["untimed"] += 1
            else:
                ages.append(max(0.0, received - stamp))
                key = (vin, name)
                # Only an advancing timestamp means the car reported something new.
                if stamp > self.previous.get(key, float("-inf")):
                    counts["new"] += 1
                    if key in self.previous and name not in SERVER_STAMPED:
                        logging.info("Fresh data: %s car_time=%s age=%.0fs", name,
                                     datetime.fromtimestamp(stamp, timezone.utc).strftime("%H:%M:%SZ"), max(0.0, received - stamp))
                    self.previous[key] = stamp
            if stamp is None and name not in self.structure_logged:
                # Field names only, to find this endpoint's timestamp field. Never values.
                self.structure_logged.add(name)
                logging.info("Untimed payload fields (%s): %s", name, structure(payload))
            elapsed = now - self.started
            if 0 <= elapsed < self.interval:
                return None
            report, self.samples, self.started = self._report(), {}, now
        logging.info("Data freshness (last %d min): %s", max(0, elapsed) // 60, report or "no fetches")
        return report

    def _report(self):
        parts = []
        for name in sorted(self.samples):
            ages, counts = self.samples[name]
            text = f"{name} n={len(ages) + counts['untimed']} new={counts['new']}"
            if counts["untimed"]:
                text += f" untimed={counts['untimed']}"
            if ages:
                ordered = sorted(ages)
                p50 = ordered[len(ordered) // 2]
                p90 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.9))]
                text += f" age_p50={p50:.0f}s p90={p90:.0f}s"
            parts.append(text)
        return "; ".join(parts)
