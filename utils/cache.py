import time
from collections import OrderedDict
from typing import Callable, Generic, Hashable, Iterator, Optional, TypeVar

K = TypeVar("K", bound=Hashable)
V = TypeVar("V")


class TTLCache(Generic[K, V]):
    """
    Lightweight, zero-dependency bounded LRU + TTL cache (`OrderedDict`).
    Prevents unbounded memory growth during 24/7 PM2 / systemd uptime.
    """

    __slots__ = ("maxsize", "ttl", "_clock", "_data")

    def __init__(
        self,
        maxsize: int = 1024,
        ttl: float = 120.0,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if maxsize <= 0:
            raise ValueError("maxsize must be > 0")
        if ttl <= 0:
            raise ValueError("ttl must be > 0")
        self.maxsize = int(maxsize)
        self.ttl = float(ttl)
        self._clock = clock
        self._data: OrderedDict[K, tuple[V, float]] = OrderedDict()

    def get(self, key: K, default: Optional[V] = None) -> Optional[V]:
        item = self._data.get(key)
        if item is None:
            return default
        val, expires_at = item
        if self._clock() >= expires_at:
            self._data.pop(key, None)
            return default
        self._data.move_to_end(key)
        return val

    def set(self, key: K, value: V, *, ttl: Optional[float] = None) -> None:
        exp_ttl = self.ttl if ttl is None else float(ttl)
        expires_at = self._clock() + exp_ttl
        if key in self._data:
            self._data.move_to_end(key)
        self._data[key] = (value, expires_at)
        if len(self._data) > self.maxsize:
            self.purge_expired()
            while len(self._data) > self.maxsize:
                self._data.popitem(last=False)

    def __setitem__(self, key: K, value: V) -> None:
        self.set(key, value)

    def __getitem__(self, key: K) -> V:
        marker = object()
        val = self.get(key, marker)  # type: ignore[arg-type]
        if val is marker:
            raise KeyError(key)
        return val  # type: ignore[return-value]

    def __contains__(self, key: object) -> bool:
        if key not in self._data:
            return False
        _, expires_at = self._data[key]  # type: ignore[index]
        if self._clock() >= expires_at:
            self._data.pop(key, None)  # type: ignore[arg-type]
            return False
        return True

    def pop(self, key: K, default: Optional[V] = None) -> Optional[V]:
        item = self._data.pop(key, None)
        if item is None:
            return default
        val, expires_at = item
        if self._clock() >= expires_at:
            return default
        return val

    def invalidate(self, key: K) -> None:
        self._data.pop(key, None)

    def invalidate_where(self, predicate: Callable[[K, V], bool]) -> int:
        now = self._clock()
        to_remove = [
            k
            for k, (v, exp) in self._data.items()
            if now >= exp or predicate(k, v)
        ]
        for k in to_remove:
            self._data.pop(k, None)
        return len(to_remove)

    def purge_expired(self) -> int:
        now = self._clock()
        expired = [k for k, (_, exp) in self._data.items() if now >= exp]
        for k in expired:
            self._data.pop(k, None)
        return len(expired)

    def clear(self) -> None:
        self._data.clear()

    def keys(self) -> Iterator[K]:
        self.purge_expired()
        return iter(list(self._data.keys()))

    def __len__(self) -> int:
        self.purge_expired()
        return len(self._data)
