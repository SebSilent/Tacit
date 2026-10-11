"""The session's event stream: one buffer per session and channel, many viewers.

The stream belongs to the session, not to a turn and not to a viewer. A turn's
driver publishes into the buffer; every open socket of that session is a
subscriber and stays one across turns; a viewer that arrives late is replayed
the buffer before it goes live. Nothing here depends on a socket: the driver
publishes whether or not anyone is watching, which is what makes the stream
survive a browser closing, a reload, or a second device opening the same
session mid-turn.

Two channels per session: ``turn`` (the agent) and ``assistant`` (the side
conversation). Each has its own buffer and its own lifecycle, so an assistant
reply streaming beside a running turn never mixes with it.

The buffer is emptied when the turn that produced it ends: from then on the
transcript is in the session record, so a viewer arriving later is served by
history, not by a replay that would double it. Subscribers are kept — they are
the session's open windows, not the turn's property. A bus nobody is watching
is dropped entirely, so an idle session costs nothing.
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

# The streams a session owns. A channel is per session, not per run: a viewer
# that connects mid-plan replays the plan's own events, and a second turn on the
# same session continues the same stream rather than starting a private one.
# "plan" is the third because plan mode is now session-owned work like the other
# two — it runs in a thread that outlives the socket that started it, so it needs
# a buffer to be handed to whoever is watching next.
CHANNELS = ("turn", "assistant", "plan")

_lock = threading.Lock()
_buses: dict[tuple[str, str], "TurnBus"] = {}


def _key(sid: str, channel: str) -> tuple[str, str]:
    return (str(sid or ""), channel if channel in CHANNELS else "turn")


class TurnBus:
    """One session channel's buffer and its live subscribers."""

    def __init__(self, loop: asyncio.AbstractEventLoop | None):
        self.loop = loop
        self.buffer: deque = deque(maxlen=BUFFER_MAX)
        self.subscribers: list[asyncio.Queue] = []
        # True while the turn that owns this buffer is running. The bus
        # outlives the turn; this flag is what "a turn is running here" reads.
        self.live = False

    def publish(self, ev: dict) -> None:
        """One event into the buffer and out to every live subscriber.

        Called from the turn's driver thread, so subscriber queues — asyncio
        objects — are fed through ``call_soon_threadsafe``. A dead loop drops
        the event for everyone rather than raising into the turn.
        """
        with _lock:
            self.buffer.append(dict(ev))
            subs = list(self.subscribers)
        loop = self.loop
        if loop is None:
            return
        for q in subs:
            try:
                loop.call_soon_threadsafe(q.put_nowait, dict(ev))
            except Exception:  # noqa: BLE001 — a closed loop is a dead viewer
                pass

    def subscribe(self) -> tuple[list[dict], asyncio.Queue]:
        """Join as a viewer: (replay, live queue).

        The replay and the subscription are decided under the lock, so a turn
        finishing between "read the buffer" and "join the subscribers" is
        impossible: either both happen, or the buffer is already empty and the
        viewer is served by history.
        """
        q: asyncio.Queue = asyncio.Queue(maxsize=SUBQUEUE_MAX)
        with _lock:
            replay = [dict(e) for e in self.buffer]
            self.subscribers.append(q)
        return replay, q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        """One viewer left (its socket died, or the tab closed). The stream
        itself is untouched: other viewers keep theirs."""
        with _lock:
            try:
                self.subscribers.remove(q)
            except ValueError:
                pass

    def clear(self) -> None:
        with _lock:
            self.buffer.clear()


def start(sid: str, loop: asyncio.AbstractEventLoop | None,
          channel: str = "turn") -> TurnBus:
    """Open the turn: take the session's bus, empty it, mark it live.

    Called before the session is marked busy, so any viewer that can observe
    busy can also subscribe to the turn behind it. Existing subscribers are
    kept — they are the session's open windows, and they simply start
    receiving the new turn's events.
    """
    key = _key(sid, channel)
    with _lock:
        bus = _buses.get(key)
        if bus is None:
            bus = TurnBus(loop)
            _buses[key] = bus
        bus.loop = loop
        bus.buffer.clear()
        bus.live = True
        return bus


def get(sid: str, channel: str = "turn") -> TurnBus | None:
    with _lock:
        return _buses.get(_key(sid, channel))


def alive(sid: str, channel: str = "turn") -> bool:
    """A turn is running on this channel right now."""
    with _lock:
        bus = _buses.get(_key(sid, channel))
    return bool(bus and bus.live)


def replay(sid: str, channel: str = "turn") -> list[dict]:
    """The channel's buffered events so far, without joining as a subscriber.

    The copy is taken under the lock, so it is the buffer as of one instant,
    never a torn mix.
    """
    with _lock:
        bus = _buses.get(_key(sid, channel))
        return [dict(e) for e in bus.buffer] if bus else []


def subscribe(sid: str, loop: asyncio.AbstractEventLoop | None = None,
              channel: str = "turn") -> tuple[list[dict], asyncio.Queue]:
    """Join the session's stream: (replay, live queue). Never None.

    The stream exists whether or not a turn is running — an empty replay just
    means the viewer is served by the transcript in the session record. This
    is the property that makes the socket irrelevant: a window that opens at
    any moment, on any device, is caught up exactly once, from the buffer.
    """
    key = _key(sid, channel)
    with _lock:
        bus = _buses.get(key)
        if bus is None:
            bus = TurnBus(loop)
            _buses[key] = bus
    return bus.subscribe()


def unsubscribe(sid: str, q: asyncio.Queue, channel: str = "turn") -> None:
    with _lock:
        bus = _buses.get(_key(sid, channel))
    if bus is not None:
        bus.unsubscribe(q)


def publish(sid: str, ev: dict, channel: str = "turn") -> None:
    """One event into the named channel. A no-op when the session has no bus —
    which cannot happen while a turn is running, because start() made one."""
    with _lock:
        bus = _buses.get(_key(sid, channel))
    if bus is not None:
        bus.publish(ev)


def finish(sid: str, channel: str = "turn") -> None:
    """The turn is over: empty the buffer, mark the stream idle, keep the
    subscribers.

    A viewer arriving now is served by the transcript in the session record —
    an empty replay is what keeps a replay from ever doubling onto it. A bus
    nobody is watching is dropped entirely: the next turn or the next viewer
    makes a fresh one, and an idle session is left holding nothing.
    """
    key = _key(sid, channel)
    with _lock:
        bus = _buses.get(key)
        if bus is None:
            return
        bus.buffer.clear()
        bus.live = False
        if not bus.subscribers:
            _buses.pop(key, None)