"""Situation-aware polling: infer REST/ACTIVE/DRIVING per vehicle from data already fetched.

Measured 7.-8.11.2026 (C40): a parked car only reports on events (unlock, doors, start, stop,
plugging); location and energy are not sent while driving. So poll fast only after events,
fetch location/odometer/energy repeatedly right after parking, and slow down otherwise.
Logs modes and reasons only, never values.
"""
import logging
import threading
import time
from urllib.parse import urlsplit

REST, ACTIVE, DRIVING = "REST", "ACTIVE", "DRIVING"
ACTIVE_SECONDS = 600       # after a change, command or engine stop
PARK_BURST_FAST = 200      # first minutes after the engine stops: about once a minute
PARK_BURST_SECONDS = 600   # then every two minutes until the car has reported
PARK_BURST_TTL = (60, 120)
DAILY_BUDGET = 7000        # above this, every vehicle uses REST intervals

# Intervals in seconds per endpoint group and mode.
INTERVALS = {
    "doors":      {REST: 120,  ACTIVE: 30,  DRIVING: 300},
    "windows":    {REST: 180,  ACTIVE: 30,  DRIVING: 300},
    "engine":     {REST: 180,  ACTIVE: 60,  DRIVING: 60},
    "energy":     {REST: 180,  ACTIVE: 120, DRIVING: 600},
    "location":   {REST: 3600, ACTIVE: 900, DRIVING: 3600},
    "odometer":   {REST: 3600, ACTIVE: 900, DRIVING: 1800},
    "statistics": {REST: 3600, ACTIVE: 900, DRIVING: 3600},
    "slow":       {REST: 1800, ACTIVE: 1800, DRIVING: 1800},
    "details":    {REST: 86400, ACTIVE: 86400, DRIVING: 86400},
}
CHARGING_ENERGY = 120
PARK_BURST_GROUPS = ("location", "odometer", "energy")
# A door or lock change also refreshes location: covers arrivals whose engine stop fell between polls.
SYNC_GROUPS = ("doors", "engine", "energy", "location")
RUNNING_VALUES = ("RUNNING", "true", True)
STOPPED_VALUES = ("STOPPED", "false", False)


def group(url):
    path = urlsplit(url).path
    if path.endswith("/doors"):return "doors"
    if path.endswith("/windows"):return "windows"
    if path.endswith("/engine-status"):return "engine"
    if "/energy/" in path:return "energy"
    if path.endswith("/location"):return "location"
    if path.endswith("/odometer"):return "odometer"
    if path.endswith("/statistics"):return "statistics"
    if path.endswith(("/diagnostics", "/engine", "/tyres", "/warnings")):return "slow"
    if "/vehicles/" in path and path.rsplit("/", 1)[-1].startswith("{"):return "details"
    return None


def _values(payload):
    """{field: value} for Volvo's {'data': {field: {'value': ...}}} shape; kept in memory only."""
    data = payload.get("data", payload) if isinstance(payload, dict) else {}
    if not isinstance(data, dict):return {}
    return {key: item.get("value") for key, item in data.items() if isinstance(item, dict)}


class VehicleActivity:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.state = {}
        self.budget_exceeded = False

    def _vehicle(self, vin):
        return self.state.setdefault(vin, {"engine": None, "active_until": 0.0, "burst_until": 0.0,
                                           "burst_done": set(), "signatures": {}, "mode": REST})

    def mode(self, vin):
        with self.lock:
            return self._mode(self._vehicle(vin))

    def _mode(self, vehicle):
        if vehicle["engine"] == "RUNNING":return DRIVING
        if self.clock() < vehicle["active_until"]:return ACTIVE
        return REST

    def interval(self, url, vin, default, charging=False, daily_gets=0):
        name = group(url)
        if name is None:return max(60, default)
        with self.lock:
            if daily_gets > DAILY_BUDGET and not self.budget_exceeded:
                self.budget_exceeded = True
                logging.warning("Daily vehicle API budget %s exceeded; using REST intervals until restart or UTC day change", DAILY_BUDGET)
            elif daily_gets <= DAILY_BUDGET and self.budget_exceeded:
                self.budget_exceeded = False
                logging.info("Vehicle API budget back below %s; situation-aware intervals restored", DAILY_BUDGET)
            vehicle = self._vehicle(vin)
            current = self._mode(vehicle)
            if current != vehicle["mode"]:
                logging.info("Vehicle activity %s -> %s (timer)", vehicle["mode"], current)
                vehicle["mode"] = current
            mode = REST if self.budget_exceeded else current
            seconds = INTERVALS[name][mode]
            if name == "energy" and charging:
                seconds = min(seconds, CHARGING_ENERGY)
            if (not self.budget_exceeded and name in PARK_BURST_GROUPS and self.clock() < vehicle["burst_until"]
                    and name not in vehicle["burst_done"]):
                fast = self.clock() < vehicle["burst_until"] - PARK_BURST_SECONDS + PARK_BURST_FAST
                seconds = min(seconds, PARK_BURST_TTL[0] if fast else PARK_BURST_TTL[1])
            return seconds

    def _start_burst(self, vehicle, since):
        vehicle["burst_until"] = self.clock() + PARK_BURST_SECONDS
        vehicle["burst_since"] = since
        vehicle["burst_done"] = set()

    def touch(self, vin, reason):
        """After a command response: poll actively for a while and refetch the sync groups."""
        with self.lock:
            vehicle = self._vehicle(vin)
            self._activate(vin, vehicle, reason)
        return SYNC_GROUPS

    def _activate(self, vin, vehicle, reason):
        before = self._mode(vehicle)
        vehicle["active_until"] = max(vehicle["active_until"], self.clock() + ACTIVE_SECONDS)
        after = self._mode(vehicle)
        if before != after:
            logging.info("Vehicle activity %s -> %s (%s)", before, after, reason)
        vehicle["mode"] = after

    def observe(self, url, vin, payload, newest=None, event_time=None):
        """Feed a fresh 200 response. Returns endpoint groups to refetch now (cache invalidation)."""
        name = group(url)
        values = _values(payload)
        refetch = ()
        with self.lock:
            vehicle = self._vehicle(vin)
            before = self._mode(vehicle)
            raw = values.get("engineStatus") if name == "engine" else None
            # Unknown or missing engine values keep the previous state instead of inventing a stop.
            engine = "RUNNING" if raw in RUNNING_VALUES else "STOPPED" if raw in STOPPED_VALUES else None
            if engine is not None:
                previous, vehicle["engine"] = vehicle["engine"], engine
                if previous == "RUNNING" and engine != "RUNNING":
                    # Car reports location, odometer and energy around parking: fetch them repeatedly.
                    self._start_burst(vehicle, event_time)
                    vehicle["active_until"] = self.clock() + ACTIVE_SECONDS
                    refetch = PARK_BURST_GROUPS
                elif engine == "RUNNING":
                    # Location and energy are not reported while driving: no burst during a trip.
                    vehicle["burst_until"] = 0.0
                    if previous is not None and previous != "RUNNING":
                        refetch = ("energy",)
            elif name in ("doors", "windows") and values:
                signature = tuple(sorted(values.items()))
                previous = vehicle["signatures"].get(name)
                vehicle["signatures"][name] = signature
                if previous is not None and previous != signature:
                    vehicle["active_until"] = self.clock() + ACTIVE_SECONDS
                    refetch = SYNC_GROUPS
                    # Fallback arrival: an engine stop may have fallen between polls. A departure
                    # cancels this burst as soon as the engine is seen running.
                    if vehicle["engine"] != "RUNNING" and self.clock() >= vehicle["burst_until"]:
                        self._start_burst(vehicle, newest)
            elif name in PARK_BURST_GROUPS and self.clock() < vehicle["burst_until"]:
                since = vehicle.get("burst_since")
                # Stop the burst for this group once the car has reported after the stop.
                if newest is not None and since is not None and newest >= since:
                    vehicle["burst_done"].add(name)
            after = self._mode(vehicle)
            if before != after:
                logging.info("Vehicle activity %s -> %s (%s update)", before, after, name)
            vehicle["mode"] = after
        return refetch
