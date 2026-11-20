"""Bus d'événements en mémoire (un abonné = une file bornée ; le plus ancien message est sacrifié si elle est pleine)."""
from __future__ import annotations

import asyncio


class Subscription:
    def __init__(self, bus: "EventBus", maxsize: int):
        self._bus = bus
        self.queue: asyncio.Queue = asyncio.Queue(maxsize)

    async def get(self) -> dict:
        return await self.queue.get()

    def close(self) -> None:
        self._bus._subs.discard(self)


class EventBus:
    def __init__(self, maxsize: int = 500):
        self._subs: set[Subscription] = set()
        self._max = maxsize

    def subscribe(self) -> Subscription:
        s = Subscription(self, self._max)
        self._subs.add(s)
        return s

    async def publish(self, topic: str, data: dict) -> None:
        msg = {"type": topic, "data": data}
        for s in list(self._subs):
            if s.queue.full():
                try:
                    s.queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            s.queue.put_nowait(msg)

    @property
    def subscribers(self) -> int:
        return len(self._subs)
