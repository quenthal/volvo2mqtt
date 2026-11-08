"""Shared endpoint cache and vehicle-API request counters."""
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import urlsplit
import requests
import activity
import freshness

class CountingSession(requests.Session):
    def __init__(self):
        super().__init__()
        self.count_lock=threading.Lock()
        self.counts=Counter()
        self.day=None
    def send(self, request, **kwargs):
        if urlsplit(request.url).hostname=="api.volvocars.com":
            day=datetime.now(timezone.utc).date().isoformat()
            with self.count_lock:
                if day!=self.day:self.day=day;self.counts.clear()
                self.counts[request.method]+=1
        return super().send(request,**kwargs)
    def snapshot(self):
        with self.count_lock:return dict(self.counts)

class EndpointCache:
    def __init__(self):
        self.lock=threading.RLock()
        self.items={}
        self.charging={}
        self.freshness=freshness.FreshnessStats()
        self.activity=activity.VehicleActivity()
        self.daily_gets=lambda:0
    def interval(self,url,vin,default):
        return self.activity.interval(url,vin,default,self.charging.get(vin,False),self.daily_gets())
    def invalidate(self,vin,groups):
        # Next refresh loop (<= 30 s) refetches these groups together.
        with self.lock:
            for key in [k for k in self.items if k[0]==vin and activity.group(k[1]) in groups]:
                del self.items[key]
    def touch(self,vin,reason):
        self.invalidate(vin,self.activity.touch(vin,reason))
    def get(self,session,url,vin,default=300,force=False,bypass=False):
        key=(vin,url)
        if isinstance(session,CountingSession):self.daily_gets=lambda:session.snapshot().get("GET",0)
        with self.lock:
            now=time.monotonic()
            old=self.items.get(key)
            ttl=10 if force else self.interval(url,vin,default)
            if old and (old[1] is None or old[1].status_code != 200):ttl=min(ttl,30)
            if old and not bypass and now-old[0]<ttl:return old[1]
            try:
                response=session.get(url.format(vin),timeout=15,allow_redirects=False)
            except requests.RequestException:
                self.items[key]=(now,None)
                return None
            self.items[key]=(time.monotonic(),response)
            received=time.time()
            if response.status_code==200 and '/energy/' in url:
                try:self.charging[vin]=response.json().get('chargingStatus',{}).get('value')=='CHARGING'
                except (ValueError,AttributeError):pass
        # Measurement and activity tracking run outside the shared cache lock.
        if response.status_code==200:
            try:payload=response.json()
            except ValueError:payload=None
            if payload is not None:
                newest=freshness.newest_timestamp(payload)
                self.freshness.record(url,vin,payload,received)
                event=newest
                if activity.group(url)=="engine":
                    # Engine stop time from the engineStatus field only, not unrelated timestamps.
                    data=payload.get("data",payload) if isinstance(payload,dict) else None
                    event=freshness.newest_timestamp(data.get("engineStatus")) if isinstance(data,dict) else None
                refetch=self.activity.observe(url,vin,payload,newest,event)
                refetch=set(refetch)-{activity.group(url)}
                if refetch:self.invalidate(vin,refetch)
        return response
