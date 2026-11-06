"""Shared endpoint cache and vehicle-API request counters."""
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import urlsplit
import requests

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
    def interval(self,url,vin,default):
        path=urlsplit(url).path
        if path.endswith(("/doors","/windows")):return 60
        if path.endswith('/engine-status'):return 120
        if '/energy/' in path:return 120 if self.charging.get(vin,False) else 300
        if path.endswith(("/diagnostics","/engine","/tyres","/warnings")):return 1800
        if path.endswith(("/odometer","/statistics","/location")):return 900
        if '/vehicles/' in path and path.rsplit('/',1)[-1].startswith('{'):return 86400
        return max(60,default)
    def get(self,session,url,vin,default=300,force=False,bypass=False):
        key=(vin,url)
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
            if response.status_code==200 and '/energy/' in url:
                try:self.charging[vin]=response.json().get('chargingStatus',{}).get('value')=='CHARGING'
                except (ValueError,AttributeError):pass
            return response
