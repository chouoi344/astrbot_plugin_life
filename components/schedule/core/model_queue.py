"""One model request at a time, with bounded priority and FIFO within classes."""

import asyncio
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass


@dataclass(eq=False)
class _Ticket:
    future: asyncio.Future
    bucket: int


class ModelQueue:
    def __init__(self, profile_empty):
        self.profile_empty = profile_empty
        # Empty-profile work gets the first turn, then chat, then other work.
        # Rotating after every grant prevents either priority from starving
        # wardrobe/schedule calls; each class itself remains FIFO.
        self._waiting = [deque(), deque(), deque()]
        self._turn = 0
        self._owner = None
        self._scheduled = False
        self._closed = False

    def _bucket(self, purpose):
        if purpose == "profile" and self.profile_empty():
            return 0
        return 1 if purpose == "chat" else 2

    def _schedule(self):
        if not self._scheduled and not self._closed:
            self._scheduled = True
            asyncio.get_running_loop().call_soon(self._pump)

    def _pump(self):
        self._scheduled = False
        if self._closed or self._owner is not None:
            return
        for offset in range(3):
            bucket = (self._turn + offset) % 3
            waiting = self._waiting[bucket]
            while waiting and waiting[0].future.done():
                waiting.popleft()
            if waiting:
                self._owner = waiting.popleft()
                self._turn = (bucket + 1) % 3
                self._owner.future.set_result(None)
                return

    @asynccontextmanager
    async def slot(self, purpose):
        if self._closed:
            raise RuntimeError("插件正在关闭，请重新加载后重试")
        ticket = _Ticket(asyncio.get_running_loop().create_future(), self._bucket(purpose))
        self._waiting[ticket.bucket].append(ticket)
        self._schedule()
        try:
            await ticket.future
        except BaseException:
            if self._owner is ticket:
                self._owner = None
            else:
                try:
                    self._waiting[ticket.bucket].remove(ticket)
                except ValueError:
                    pass
            self._schedule()
            raise
        try:
            yield
        finally:
            if self._owner is ticket:
                self._owner = None
            self._schedule()

    def close(self):
        self._closed = True
        for waiting in self._waiting:
            while waiting:
                waiting.popleft().future.cancel()
