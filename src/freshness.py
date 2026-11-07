"""Measures how fresh Volvo data is: age of the newest field timestamp per endpoint. Logs no values."""
import logging
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

REPORT_INTERVAL = 3600


def newest_timestamp(payload):
    """Newest ISO-8601 'timestamp' anywhere in the payload, as epoch seconds."""
    newest = None
    stack = [payload]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for key, value in item.items():
                if key == "timestamp" and isinstance(value, str):
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
                    self.previous[key] = stamp
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
