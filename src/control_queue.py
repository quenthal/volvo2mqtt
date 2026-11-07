"""One worker per vehicle; pending toggle requests collapse to the latest target."""
import threading
import time
import logging

class ControlQueue:
    def __init__(self, debounce=1.0, cooldown=3.0):
        self.debounce, self.cooldown = debounce, cooldown
        self.condition = threading.Condition()
        self.pending = {}
        self.workers = set()
        self.last = {}

    def submit(self, vin, group, target, action, done=None, suppress_repeat=True):
        """suppress_repeat: skip a target already accepted within 30 s (toggles); buttons send every press."""
        with self.condition:
            if (vin, group) in self.pending:
                logging.info("Pending %s request replaced by a newer %s request", group, target)
            self.pending[(vin, group)] = (time.monotonic()+self.debounce, target, action, done, suppress_repeat)
            if vin not in self.workers:
                self.workers.add(vin)
                threading.Thread(target=self._work,args=(vin,),daemon=True,name="vehicle_control").start()
            self.condition.notify_all()

    def _work(self, vin):
        while True:
            with self.condition:
                choices=[(key,entry) for key,entry in self.pending.items() if key[0]==vin]
                if not choices:
                    self.workers.remove(vin)
                    return
                key,entry=min(choices,key=lambda item:item[1][0])
                remaining=entry[0]-time.monotonic()
                if remaining>0:
                    self.condition.wait(remaining)
                    continue
                del self.pending[key]
                previous=self.last.get(key)
                duplicate=entry[4] and previous and previous[0]==entry[1] and time.monotonic()-previous[1]<30
            if duplicate:
                logging.info("Vehicle control %s %s skipped: same request accepted %.0f s ago",
                             key[1], entry[1], time.monotonic()-previous[1])
                # Callbacks run outside the condition; callers may hold their own locks while submitting.
                if entry[3]:entry[3]()
                continue
            try:
                accepted=entry[2]()
                if accepted:
                    with self.condition:self.last[key]=(entry[1],time.monotonic())
            except Exception as error:
                logging.warning("Vehicle control failed (%s); not retried",type(error).__name__)
            finally:
                if entry[3]:entry[3]()
            # Do not hold the condition while sleeping: new intent remains replaceable.
            time.sleep(self.cooldown)
