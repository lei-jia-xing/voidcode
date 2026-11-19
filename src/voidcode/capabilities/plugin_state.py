"""One plugin's session-state mechanics: strict key checks plus versioned CAS load/save.

Every state-owning plugin stores a single JSON object per session and repeats the
same shape: fetch the payload and its CAS version, reject unknown (and usually
missing) top-level keys with a plugin-specific message, parse the typed state,
then replace the payload under the version it read. The plugin supplies its key
set, empty value, parse/serialize functions, and message formats; all
``get_state`` / ``replace_state`` / version mechanics live here exactly once.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from voidcode.sdk.services import StateServices


def require_keys(
    data: Mapping[str, object],
    keys: frozenset[str],
    *,
    unknown: Callable[[list[str]], str],
    missing: Callable[[list[str]], str] | None = None,
) -> None:
    """Reject unknown keys, then (when asked) missing required keys.

    Unknown keys fail on the first of the sorted extras. Each plugin formats its
    own message, so long-pinned error strings stay byte-identical.
    """
    extra = sorted(str(key) for key in data if key not in keys)
    if extra:
        raise ValueError(unknown(extra))
    if missing is not None:
        absent = sorted(keys - data.keys())
        if absent:
            raise ValueError(missing(absent))


class PluginStateStore[T]:
    """Strict session-state load/save for one plugin over ``StateServices``."""

    def __init__(
        self,
        services: StateServices,
        *,
        keys: frozenset[str],
        parse: Callable[[Mapping[str, object]], T],
        serialize: Callable[[T], dict[str, object]],
        empty: Callable[[], T],
        unknown: Callable[[list[str]], str],
        missing: Callable[[list[str]], str] | None = None,
    ) -> None:
        self._services = services
        self._keys = keys
        self._parse = parse
        self._serialize = serialize
        self._empty = empty
        self._unknown = unknown
        self._missing = missing

    def load(self) -> tuple[T, int]:
        """Current typed state plus its CAS version; absent state yields the empty value."""
        data, version = self._services.get_state("session")
        if data is None:
            return self._empty(), version
        require_keys(data, self._keys, unknown=self._unknown, missing=self._missing)
        return self._parse(data), version

    def save(self, state: T, expected_version: int) -> int:
        """CAS-replace the stored payload; returns the new version."""
        return self._services.replace_state(self._serialize(state), expected_version=expected_version)
