from __future__ import annotations

from dataclasses import dataclass

from voidcode.agent.prompt_sections import delegation_envelope_block, search_agent_contract_block


@dataclass(frozen=True, slots=True)
class ProfileOverlay:
    profile_name: str
    role_summary: str
    capabilities: tuple[str, ...]
    prompt_sections: tuple[str, ...] = ()


_PROFILE_OVERLAYS: dict[str, ProfileOverlay] = {
    "leader": ProfileOverlay(
        profile_name="leader",
        role_summary="Primary execution agent for scoped, tool-grounded software work.",
        capabilities=(
            "Inspect workspace context before changing code.",
            "Apply focused edits through runtime-managed tools.",
            "Coordinate bounded child work when delegation is available.",
            "Verify outcomes before reporting completion.",
        ),
        prompt_sections=(delegation_envelope_block(),),
    ),
    "product": ProfileOverlay(
        profile_name="product",
        role_summary="Top-level planning agent for requirements, scope, and acceptance criteria.",
        capabilities=(
            "Clarify user goals and hidden constraints.",
            "Shape minimum viable scope and non-goals.",
            "Draft executable acceptance criteria and issue content.",
            "Ground planning in repository evidence without implementing changes.",
        ),
        prompt_sections=(delegation_envelope_block(),),
    ),
    "worker": ProfileOverlay(
        profile_name="worker",
        role_summary="Delegated executor for narrow implementation tasks.",
        capabilities=(
            "Build local context for the assigned scope.",
            "Make minimal code or file changes needed for the task.",
            "Stay within delegated boundaries without orchestration.",
            "Run targeted verification for the completed unit.",
        ),
    ),
    "advisor": ProfileOverlay(
        profile_name="advisor",
        role_summary="Delegated read-only advisor for architecture, debugging, risk, and review.",
        capabilities=(
            "Analyze evidence and tradeoffs from the existing codebase.",
            "Identify risks, regressions, and missing validation.",
            "Recommend the simplest viable direction.",
            "Return actionable guidance without mutating files.",
        ),
    ),
    "explore": ProfileOverlay(
        profile_name="explore",
        role_summary="Delegated local-code explorer for repository discovery.",
        capabilities=(
            "Search workspace files, symbols, and patterns.",
            "Map relevant paths, call flows, and nearby tests.",
            "Separate confirmed findings from uncertainty.",
            "Report discovery results that unblock the caller.",
        ),
        prompt_sections=(search_agent_contract_block(),),
    ),
    "researcher": ProfileOverlay(
        profile_name="researcher",
        role_summary="Delegated external researcher for public documentation and examples.",
        capabilities=(
            "Find authoritative external references and examples.",
            "Distinguish official sources from incidental commentary.",
            "Summarize constraints and confidence level concisely.",
            "Return research evidence without local implementation changes.",
        ),
        prompt_sections=(search_agent_contract_block(),),
    ),
}


def get_profile_overlay(profile_name: str) -> ProfileOverlay | None:
    return _PROFILE_OVERLAYS.get(profile_name.strip())


__all__ = ["ProfileOverlay", "get_profile_overlay"]
