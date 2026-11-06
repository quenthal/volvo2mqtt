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

    def submit(self, vin, group, target, action, done=None):
        with self.condition:
            self.pending[(vin, group)] = (time.monotonic()+self.debounce, target, action, done)
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
                duplicate=previous and previous[0]==entry[1] and time.monotonic()-previous[1]<30
            if duplicate:
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
