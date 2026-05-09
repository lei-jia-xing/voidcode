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
        role_summary="Primary execution agent for tool-grounded software work.",
        capabilities=(
            "Inspect context before acting.",
            "Use runtime tools for focused execution.",
            "Verify before claiming completion.",
        ),
        prompt_sections=(delegation_envelope_block(),),
    ),
    "product": ProfileOverlay(
        profile_name="product",
        role_summary="Top-level planning agent for requirements, scope, and acceptance criteria.",
        capabilities=(
            "Clarify goals and hidden constraints.",
            "Shape minimum viable scope and non-goals.",
            "Draft acceptance criteria and issue content.",
        ),
        prompt_sections=(delegation_envelope_block(),),
    ),
    "worker": ProfileOverlay(
        profile_name="worker",
        role_summary="Delegated executor for narrow implementation tasks.",
        capabilities=(
            "Build local context for the assigned scope.",
            "Make the smallest correct change.",
            "Run targeted verification.",
        ),
    ),
    "advisor": ProfileOverlay(
        profile_name="advisor",
        role_summary="Delegated read-only advisor for architecture, debugging, risk, and review.",
        capabilities=(
            "Analyze evidence and tradeoffs.",
            "Identify risks and missing validation.",
            "Recommend the simplest viable direction.",
        ),
    ),
    "explore": ProfileOverlay(
        profile_name="explore",
        role_summary="Delegated local-code explorer for repository discovery.",
        capabilities=(
            "Search workspace files, symbols, and patterns.",
            "Map relevant paths and call flows.",
            "Report findings that unblock the caller.",
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
        ),
        prompt_sections=(search_agent_contract_block(),),
    ),
}


def get_profile_overlay(profile_name: str) -> ProfileOverlay | None:
    return _PROFILE_OVERLAYS.get(profile_name.strip())


__all__ = ["ProfileOverlay", "get_profile_overlay"]
