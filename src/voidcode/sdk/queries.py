"""Typed optional queries over the runtime's bounded JSON query transport."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel

from .plugin import PluginQuery, PluginQueryCaller
from .services import PluginServices


class QueryAnswer(Protocol):
    def as_payload(self) -> dict[str, object]: ...


class QueryContractError(ValueError):
    """A query answered, but its answer violated the agreed contract."""


@dataclass(frozen=True, slots=True)
class QueryContract[Request: BaseModel, Answer: QueryAnswer]:
    """One provider-independent request/answer contract.

    Keep the name stable for compatible changes; breaking changes need a new name.
    QueryUnavailable and handler errors propagate; None means a missing resource.
    """

    name: str
    request_type: type[Request]
    decode_answer: Callable[[Mapping[str, object], Request], Answer | None]

    def _answer(self, value: Mapping[str, object], request: Request) -> Answer:
        if not isinstance(value, Mapping):
            raise QueryContractError(f"query {self.name!r} must answer with a JSON object")
        answer = self.decode_answer(value, request)
        if answer is None:
            raise QueryContractError(f"query {self.name!r} answered outside its request/answer contract")
        return answer

    def ask(self, services: PluginServices, request: Request) -> Answer | None:
        value = services.query(self.name, request.model_dump())
        return None if value is None else self._answer(value, request)

    def bind(self, handler: Callable[[Request, PluginQueryCaller], Answer | None]) -> PluginQuery:
        """Register a typed implementation without repeating argument decoding."""

        def invoke(arguments: Mapping[str, object], caller: PluginQueryCaller) -> Mapping[str, object] | None:
            request = self.request_type.model_validate(arguments)
            answer = handler(request, caller)
            if answer is None:
                return None
            return answer.as_payload()

        return PluginQuery(name=self.name, handler=invoke)
