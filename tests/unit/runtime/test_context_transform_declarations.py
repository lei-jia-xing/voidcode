from __future__ import annotations

import pytest

from voidcode.runtime.context.transforms import (
    HookPresetGuidanceTransformProvider,
    ModeGuidanceTransformProvider,
    RuntimeContextTransformDeclaration,
    RuntimeContextTransformFailurePolicy,
    RuntimeContextTransformInjection,
    RuntimeContextTransformRegistry,
    RuntimeContextTransformRequest,
    RuntimeContextTransformResult,
    RuntimeContextTransformScope,
    RuntimeContextTransformTrace,
    context_transform_applied_payloads,
)


class _Context:
    provider_id = "consumer-context"
    provider_version = "2"
    scope: RuntimeContextTransformScope = "provider_context"
    priority = 150
    failure_policy: RuntimeContextTransformFailurePolicy = "block"

    def build_result(self, request: RuntimeContextTransformRequest) -> RuntimeContextTransformResult:
        if request.mode_guidance_context == "fail":
            raise ValueError("actual consumer failure")
        return RuntimeContextTransformResult(
            injections=(RuntimeContextTransformInjection("system", f"v{self.provider_version}: {request.mode_guidance_context}"),),
            traces=(
                RuntimeContextTransformTrace(
                    provider_id=self.provider_id,
                    provider_version=self.provider_version,
                    scope=self.scope,
                    failure_policy=self.failure_policy,
                    priority=self.priority,
                    injection_count=1,
                    sources=(__name__,),
                ),
            ),
        )


class _NextContext(_Context):
    provider_version = "3"


def _request(text: str = "consumer input") -> RuntimeContextTransformRequest:
    return RuntimeContextTransformRequest(workspace=None, tool_results=(), hook_preset_context="", mode_guidance_context=text)


def _bound(owner: type[_Context]) -> RuntimeContextTransformRegistry:
    return RuntimeContextTransformRegistry.from_declarations((RuntimeContextTransformDeclaration.from_provider(owner),)).bind(lambda _name: owner())


def test_actual_provider_version_changes_context_and_applied_identity() -> None:
    current = _bound(_Context).build_result(_request())
    updated = _bound(_NextContext).build_result(_request())
    assert current.injections[0].content == "v2: consumer input"
    assert updated.injections[0].content == "v3: consumer input"
    first_identity, first = context_transform_applied_payloads(
        context_metadata={"context_transforms": current.metadata_payload()},
        tool_result_count=1,
    )[0]
    next_identity, second = context_transform_applied_payloads(
        context_metadata={"context_transforms": updated.metadata_payload()},
        tool_result_count=1,
    )[0]
    assert first_identity != next_identity
    assert first["provider_version"] == "2" and second["provider_version"] == "3"
    assert first["scope"] == "provider_context" and first["failure_policy"] == "block"
    assert {key: value for key, value in first.items() if key != "provider_version"} == {
        key: value for key, value in second.items() if key != "provider_version"
    }
    same_identity, _ = context_transform_applied_payloads(
        context_metadata={"context_transforms": current.metadata_payload()},
        tool_result_count=99,
    )[0]
    assert same_identity == first_identity


def test_context_ties_follow_id_not_registration_and_failure_retains_declared_policy() -> None:
    registry = RuntimeContextTransformRegistry(providers=(ModeGuidanceTransformProvider(), _Context()))
    result = registry.build_result(_request())
    assert [injection.content for injection in result.injections] == ["v2: consumer input", "consumer input"]
    failure = registry.build_result(_request("fail"))
    assert failure.traces[0].status == "error"
    assert failure.traces[0].provider_version == "2" and failure.traces[0].failure_policy == "block"
    assert failure.failure_policy == "warn"
    assert failure.injections[0].content == "fail"


def test_unbound_unknown_selection_and_wrong_actual_provider_are_refused() -> None:
    declaration = RuntimeContextTransformDeclaration.from_provider(ModeGuidanceTransformProvider)
    registry = RuntimeContextTransformRegistry.from_declarations((declaration,))
    with pytest.raises(RuntimeError):
        registry.build_result(_request())
    with pytest.raises(ValueError):
        registry.filtered(("not-admitted",))
    with pytest.raises(ValueError):
        registry.bind(lambda _name: HookPresetGuidanceTransformProvider())


@pytest.mark.parametrize("change", [{"version": 1}, {"version": True}, {"extra": "unknown"}])
def test_old_or_unclosed_context_envelope_is_refused(change: dict[str, object]) -> None:
    metadata = _bound(_Context).build_result(_request()).metadata_payload()
    with pytest.raises(ValueError):
        context_transform_applied_payloads(context_metadata={"context_transforms": {**metadata, **change}}, tool_result_count=0)
