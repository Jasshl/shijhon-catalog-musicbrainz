"""The adapter's pace toward one service: one request at a time, each starting at least
``interval`` seconds after the one before it was answered, in the order they asked, and
none while the service has asked for a pause.

MusicBrainz allows a client one request a second, counted across everything it does, and
answers 503 (with ``Retry-After``) to one that goes faster. So every request of the adapter
to one service is made inside ``Pace.request()`` - searches, views, the library pass and
covers alike - and whoever shares a ``Pace`` shares the one allowance.

The interval is counted from the end of the request before, not from its start, and the
next request waits until this one is over. A request spends an unknown time on its way out
(a connection to open, a slow network), so only this keeps two of them a full interval
apart where the service sees them; and what the service says in its answer - wait - is
known before the next request is let go.

A request that would wait too long for its turn is turned away (``Busy``) rather than
queued without end: when it asks, by what is waiting already, and again while it waits,
by the time that has passed.

A pace belongs to one event loop at a time. Shijhon has one; a second loop in another
thread that asks while the first is using the pace is turned away (``Busy``, ``elsewhere``)
- never served next to it, which would be two allowances.

Custom, because the limiter has to be held back by the service (``hold``), has to span the
request and has to turn requests away; it is one lock and one clock.
"""

from __future__ import annotations

import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import anyio

# A request that would wait longer than this for its turn is turned away.
MAX_WAIT = 20.0


class Busy(Exception):
    """No turn within ``max_wait``: too many requests are waiting, the service asked for a
    pause that long (``paused``), or another event loop is using the pace (``elsewhere``)."""

    def __init__(self, seconds: float, *, paused: bool = False, elsewhere: bool = False) -> None:
        super().__init__(f"no turn for {seconds:.0f}s")
        self.seconds = seconds
        self.paused = paused
        self.elsewhere = elsewhere


class Pace:
    def __init__(
        self,
        interval: float,
        *,
        max_wait: float = MAX_WAIT,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = anyio.sleep,
    ) -> None:
        self.interval = max(0.0, interval)
        self.max_wait = max_wait
        self._clock = clock
        self._sleep = sleep
        self._lock: anyio.Lock | None = None
        self._guard = threading.Lock()  # whose the pace is, decided by one thread at a time
        self._ended: float | None = None  # when the latest request was over
        self._held = float("-inf")  # no request before this (the service asked to wait)
        self._took = 0.0  # how long a request takes, lately
        self._waiting = 0
        self._users = 0  # requests waiting or under way
        self._thread: int | None = None  # where those are (an event loop's thread)
        self.turns = 0  # requests let go (observable in tests)

    def _free_in(self) -> float:
        """Seconds until a request may start, were none under way or waiting."""
        now = self._clock()
        after_last = 0.0 if self._ended is None else self._ended + self.interval - now
        return max(0.0, after_last, self._held - now)

    def paused_for(self) -> float:
        """Seconds left of the pause the service asked for (0: none)."""
        return max(0.0, self._held - self._clock())

    def hold(self, seconds: float) -> None:
        """The service asked to wait (``Retry-After``): no request starts before then."""
        self._held = max(self._held, self._clock() + max(0.0, seconds))

    @asynccontextmanager
    async def request(self) -> AsyncIterator[None]:
        """Entered when this request may start; the next one waits until it is left, and
        ``interval`` more. Raises :class:`Busy` rather than wait longer than ``max_wait``
        for that."""
        here = threading.get_ident()
        with self._guard:
            if self._users and self._thread != here:
                raise Busy(0.0, elsewhere=True)
            paused = self.paused_for()
            if paused > self.max_wait:
                raise Busy(paused, paused=True)
            ahead = self._free_in() + self._waiting * (self.interval + self._took)
            if ahead > self.max_wait:
                raise Busy(ahead)
            if self._lock is None:
                self._lock = anyio.Lock()
            lock = self._lock
            self._thread = here
            self._users += 1
            self._waiting += 1
        began = self._clock()
        waiting = True
        try:
            # First come, first served; one at a time. (The requests ahead may take longer
            # than was reckoned with: no longer than ``max_wait`` in line either.)
            with anyio.move_on_after(self.max_wait) as in_line:
                await lock.acquire()
            if in_line.cancelled_caught:
                raise Busy(self.max_wait)
            try:
                while (wait := self._free_in()) > 0:
                    waited = self._clock() - began
                    if waited + wait > self.max_wait:  # a pause asked for meanwhile, say
                        raise Busy(waited + wait, paused=self.paused_for() > 0)
                    await self._sleep(wait)
                waited = self._clock() - began
                if waited > self.max_wait:  # the wait itself took longer than it should
                    raise Busy(waited)
                self.turns += 1
                with self._guard:
                    self._waiting -= 1
                    waiting = False
                started = self._clock()
                try:
                    yield
                finally:
                    # Also for a request given up half way: it may have been sent.
                    self._ended = self._clock()
                    self._took = (self._took + self._ended - started) / 2
            finally:
                lock.release()
        finally:
            with self._guard:
                if waiting:
                    self._waiting -= 1
                self._users -= 1
