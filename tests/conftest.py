import pytest

from demo.fake_hey import fake_server


@pytest.fixture
def server():
    with fake_server() as running:
        yield running


@pytest.fixture
def blocked():
    """A worker cannot finish until test assertions finish; always release it."""
    import threading
    from contextlib import contextmanager

    @contextmanager
    def gate():
        entered, release, returned = (threading.Event() for _ in range(3))
        def wait(*args, **kwargs):
            entered.set()
            release.wait()
            returned.set()
        try:
            yield wait, entered, returned
        finally:
            release.set()
    return gate


@pytest.fixture
def scheduled_service():
    """Model dispatcher completion times through Service's existing seams."""
    from rh_preflight.card import Card
    from rh_preflight.extract import tx_intent
    from rh_preflight.transactions import local_card, project

    def install(service, request, builder_at, tx_at):
        ticks, submitted, waits = [0.0], [], []
        class Executor:
            def submit(self, fn, *args, **kwargs):
                tx = fn.__name__ == '_tx_cards'
                future = {'at': tx_at if tx else builder_at, 'tx': tx, 'args': args}
                submitted.append(future)
                return future
        def wait(future, deadline, fallback):
            waits.append(deadline)
            ticks[0] = max(ticks[0], min(deadline, future['at']))
            if future['at'] > deadline:
                return fallback
            if future['tx']:
                if future['at'] > future['args'][3]:
                    return fallback
                intent = tx_intent(request)[0]
                return [project(local_card(intent), intent,
                    {'x-toll': {'simulation': {'status': 'success', 'block': 10}}})]
            return [Card(t, 'identifier') for t in future['args'][0]]
        service.clock = lambda: ticks[0]
        service.executor, service.wait = Executor(), wait
        return ticks, submitted, waits
    return install


@pytest.fixture
def controlled_budget(monkeypatch):
    """Explicitly expire selected jobs; all other waits only guard against hangs."""
    import time
    from concurrent.futures import ThreadPoolExecutor
    from rh_preflight import hooks, network

    monkeypatch.setattr(time, 'monotonic', lambda: 100.0)
    jobs = []
    submit = ThreadPoolExecutor.submit

    def tracked(pool, fn, *args, **kwargs):
        future = submit(pool, fn, *args, **kwargs)
        future.test_job = (fn.__name__, args)
        jobs.append(future)
        return future

    monkeypatch.setattr(ThreadPoolExecutor, 'submit', tracked)

    def install(expired, service=None, before_expire=lambda: None):
        def wait(future, deadline, fallback):
            if expired(*future.test_job):
                before_expire()
                assert not future.done(), 'expired job must still be pending'
                return fallback
            return future.result(timeout=30)  # Hang guard, never a fallback verdict.
        monkeypatch.setattr(hooks, 'budget_result', wait)
        monkeypatch.setattr(network, 'budget_result', wait)
        if service is not None:
            service.clock, service.wait = time.monotonic, wait
            for client in [service.client, *service.network.clients.values()]:
                client.clock = time.monotonic
        return jobs
    return install


@pytest.fixture
def expire_transport(monkeypatch):
    """A transport deadline expires on open, with no socket or wall-clock wait."""
    from types import SimpleNamespace

    def install(client):
        ticks, calls = [100.0], []
        client.clock = lambda: ticks[0]
        def open(request, timeout):
            calls.append((request, timeout))
            raise TimeoutError('test-controlled transport deadline')
        monkeypatch.setattr(client, 'opener', SimpleNamespace(open=open))
        return calls
    return install
