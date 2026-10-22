import datetime
import time
from threading import Lock

# Performance Decorator
def performance(fn):
    def wrap(*args,**kwargs):
        start = time()
        res = fn(*args,**kwargs)
        stop = time()
        print(f"exec time: {stop-start:0.2f} sec")
        return res
    return wrap

# timestamp decorator
def timestamp(fn):
    def wrap(*args,**kwargs):
        ts = datetime.datetime.fromtimestamp(time.time()).strftime("%Y-%m-%d %H:%M:%S")
        print(f"{ts} - {fn.__name__} called")
        res = fn(*args,**kwargs)
        return res
    return wrap

# Critical Section Lock decorator
def critical_section_lock(func, mutex : Lock):
    def wrap(*args,**kwargs):
        mutex.acquire()
        res = func(*args,**kwargs)
        mutex.release()
        return res
    return wrap

# Print timestamp
def print_timestamp(msg : str):
        ts = datetime.datetime.fromtimestamp(time.time()).strftime("%Y-%m-%d %H:%M:%S.%f")
        print(f"{ts}:: {msg}")
