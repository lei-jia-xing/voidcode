"""Per-model wire pins for providers whose upstream wire varies by model.

Mirrors OMP's ``api-routes`` axis (``packages/catalog/src/compat/behavior.ts:134-148``):
a provider's rows are consulted in declaration order, the first match wins, and a
provider with no matching row falls through to the caller's own default (the
provider table's wire). ``api_routes.json`` carries the pins; this module is the
only reader, and both the model-catalog generator and the gateway dispatch
resolve through it, so the shipped catalog and the runtime cannot disagree.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from importlib.resources import files as _resource_files
from typing import Final, cast

from .model_match import Matcher, is_matcher, matches
from .provider_table import require_provider_id


@dataclass(frozen=True, slots=True)
class ApiRoute:
    """One ``route`` declaration: what to match, and the wire a match selects."""

    provider: str
    matcher: Matcher
    value: str
    api: str
    strip_prefix: bool = False
    source: str = ""


@dataclass(frozen=True, slots=True)
class ApiRouteMatch:
    """The wire a route pins one model to.

    ``request_model_id`` is set only by a ``strip_prefix`` route: the id the
    request must carry once the matched prefix is removed.
    """

    api: str
    request_model_id: str | None = None


def _string(entry: dict[str, object], field: str, provider: str) -> str:
    value = entry.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"api route for provider {provider!r} is missing a non-empty {field!r}")
    return value


def _route(raw: object) -> ApiRoute:
    if not isinstance(raw, dict):
        raise ValueError("api route entries must be objects")
    entry = cast(dict[str, object], raw)
    provider = require_provider_id(_string(entry, "provider", "?"), source="api_routes.json")
    api = _string(entry, "api", provider)
    match = entry.get("match")
    if not isinstance(match, dict):
        raise ValueError(f"api route for provider {provider!r} is missing a 'match' object")
    match_entry = cast(dict[str, object], match)
    matcher = match_entry.get("type")
    if not is_matcher(matcher):
        raise ValueError(f"api route for provider {provider!r} has an unknown matcher: {matcher!r}")
    strip_prefix = match_entry.get("strip_prefix", False)
    if not isinstance(strip_prefix, bool):
        raise ValueError(f"api route for provider {provider!r} has a non-boolean 'strip_prefix'")
    source = entry.get("source")
    return ApiRoute(
        provider=provider,
        matcher=matcher,
        value=_string(match_entry, "value", provider),
        api=api,
        strip_prefix=strip_prefix,
        source=source if isinstance(source, str) else "",
    )


def _load() -> Mapping[str, tuple[ApiRoute, ...]]:
    payload = json.loads(_resource_files("voidcode.provider").joinpath("api_routes.json").read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("routes"), list):
        raise ValueError("api_routes.json must hold a 'routes' list")
    routes: dict[str, list[ApiRoute]] = {}
    for raw in cast(list[object], payload["routes"]):
        route = _route(raw)
        routes.setdefault(route.provider, []).append(route)
    return {provider: tuple(provider_routes) for provider, provider_routes in routes.items()}


#: Canonical provider id -> its route rows, in declaration (first-match-wins) order.
API_ROUTES: Final[Mapping[str, tuple[ApiRoute, ...]]] = _load()


def _match_route(routes: Iterable[ApiRoute], model_id: str) -> ApiRouteMatch | None:
    """First route in declaration order that matches ``model_id``, else ``None``."""
    for route in routes:
        if not matches(route.matcher, route.value, model_id):
            continue
        stripped = model_id[len(route.value) :] if route.strip_prefix and model_id.startswith(route.value) else None
        return ApiRouteMatch(api=route.api, request_model_id=stripped or None)
    return None


def api_route_for(provider_id: str, model_id: str) -> ApiRouteMatch | None:
    """The wire one provider's route rows pin ``model_id`` to, ``None`` when none match."""
    return _match_route(API_ROUTES.get(provider_id, ()), model_id)


__all__ = [
    "API_ROUTES",
    "Matcher",
    "ApiRoute",
    "ApiRouteMatch",
    "api_route_for",
]
