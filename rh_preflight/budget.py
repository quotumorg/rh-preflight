"""Deadline-bound waits, including adapters that ignore their read deadline."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError
import time


class Pool(ThreadPoolExecutor):
    def __exit__(self, *args):
        self.shutdown(wait=False, cancel_futures=True)
        return False


def result(future, deadline, fallback):
    try:
        return future.result(timeout=max(0, deadline - time.monotonic()))
    except TimeoutError:
        future.cancel()
        return fallback
