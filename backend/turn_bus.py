"""The turn's event bus: one buffer per running turn, many viewers.

Stage 3.6. The turn's events used to flow through one asyncio.Queue owned by
the one socket that started the turn, so a viewer that arrived after the turn
began — a reload, a second window, a switch-back — could not see what had
already streamed past, and a socket that died took the turn's epilogue with it
(the transcript save ran in the socket's context). The queue is replaced by a
bus: the turn publishes into a bounded buffer, every viewer subscribes, and a
subscriber that arrives late is replayed the buffer before it goes live. The
socket that started the turn is no longer special; it is just the first
subscriber.

The bus is also what makes replay honest: the buffer holds each event exactly
once, so a replayed stream is the turn as it happened, not a copy of what a
particular viewer happened to have seen.
"""

import asyncio
import threading
from collections import deque

# The buffer is the replay, and a replay is only ever asked for by a viewer
# that just connected. Two thousand events is past any turn's step budget, so
# the bound exists for the pathological case only: a turn that long has lost
# its opening moments to a viewer, which is the honest cost of a bound.
BUFFER_MAX = 2000

# A live subscriber that stops draining (a stalled socket) must not grow its
# queue without limit. When it is full, new events are dropped for that
# viewer only — the buffer still has them, and the viewer's next fresh socket
# replays them.
SUBQUEUE_MAX = 600

_lock = threading.Lock()
_buses: dict[str, "TurnBus"] = {}


class TurnBus:
    """One running turn's buffer and its live subscribers."""

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        self.buffer: deque = deque(maxlen=BUFFER_MAX)
        self.subscribers: list[asyncio.Queue] = []

    def publish(self, ev: dict) -> None:
        """One event into the buffer and out to every live subscriber.

        Called from the turn's worker thread, so subscriber queues — asyncio
        objects — are fed through ``call_soon_threadsafe``, exactly as the old
        single-queue pump was. A dead loop drops the event for everyone rather
        than raising into the turn.
        """
        with _lock:
            self.buffer.append(dict(ev))
            subs = list(self.subscribers)
        for q in subs:
            try:
                self.loop.call_soon_threadsafe(q.put_nowait, dict(ev))
            except Exception:  # noqa: BLE001 — a closed loop is a dead viewer
                pass

    def subscribe(self) -> tuple[list[dict], asyncio.Queue]:
        """Join as a viewer: (replay, live queue).

        Only ever called from :func:`subscribe`, which already holds the lock —
        the replay and the subscription are decided under it, so a turn
        finishing between "read the buffer" and "join the subscribers" is
        impossible: either both happen, or the turn is already gone.
        """
        q: asyncio.Queue = asyncio.Queue(maxsize=SUBQUEUE_MAX)
        replay = [dict(e) for e in self.buffer]
        self.subscribers.append(q)
        return replay, q

    def _release(self) -> None:
        """End every subscription. Called by :func:`finish` under the lock."""
        subs = list(self.subscribers)
        self.subscribers = []
        for q in subs:
            try:
                self.loop.call_soon_threadsafe(q.put_nowait, None)
            except Exception:  # noqa: BLE001
                pass


def start(sid: str, loop: asyncio.AbstractEventLoop) -> TurnBus:
    """Open the turn's bus. Called before the session is marked busy, so any
    viewer that can observe "busy" can also subscribe to the turn behind it."""
    with _lock:
        bus = TurnBus(loop)
        _buses[sid] = bus
        return bus


def get(sid: str) -> TurnBus | None:
    with _lock:
        return _buses.get(sid)


def alive(sid: str) -> bool:
    with _lock:
        return sid in _buses


def replay(sid: str) -> list[dict]:
    """The turn's buffered events so far, without joining as a subscriber.

    For a hello that lands mid-turn: that viewer wants the past, not the
    future — its socket is not the turn's pump. The copy is taken under the
    lock, so it is the buffer as of one instant, never a torn mix.
    """
    with _lock:
        bus = _buses.get(sid)
        return [dict(e) for e in bus.buffer] if bus else []


def subscribe(sid: str) -> tuple[list[dict], asyncio.Queue] | None:
    """Subscribe to the sid's live turn, or None when there is none.

    Lookup and join happen under one lock, so a turn cannot finish between
    them: either the viewer is in before ``finish`` releases everyone, or the
    turn is already gone and the viewer is served by history.
    """
    with _lock:
        bus = _buses.get(sid)
        if bus is None:
            return None
        return bus.subscribe()


def finish(sid: str) -> None:
    """The turn is over: end every subscription and forget the bus.

    Forgetting is the point — a finished turn's transcript is in the session
    record, so a viewer arriving now is served by history, not by replay. The
    buffer dying with the turn is what keeps a replay from ever doubling onto
    what the viewer already has.
    """
    with _lock:
        bus = _buses.pop(sid, None)
    if bus is not None:
        bus._release()