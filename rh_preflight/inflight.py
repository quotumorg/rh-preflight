"""Per-key locks retained only while an owner or waiter uses them."""
import threading


class KeyedLocks:
    def __init__(self):
        self._guard = threading.RLock()
        self._entries = {}

    def acquire(self, key, *, timeout):
        with self._guard:
            entry = self._entries.get(key)
            if entry is None:
                entry = [threading.Lock(), 0]
                self._entries[key] = entry
            entry[1] += 1
        acquired = False
        try:
            acquired = entry[0].acquire(timeout=timeout)
            return acquired
        finally:
            if not acquired:
                self._drop(key, entry)

    def release(self, key):
        with self._guard:
            entry = self._entries[key]
            entry[0].release()
            self._drop(key, entry)

    def _drop(self, key, entry):
        # The reference includes waiters registered before the lock is acquired.
        with self._guard:
            entry[1] -= 1
            if entry[1] == 0:
                del self._entries[key]
