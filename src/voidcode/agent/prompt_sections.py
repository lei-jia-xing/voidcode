from __future__ import annotations

_DYNAMIC_BOUNDARY_MARKER = "<!-- voidcode:dynamic-boundary -->"


def _clean_lines(values: list[str]) -> list[str]:
    return [value.strip() for value in values if value.strip()]


def identity_header(profile_name: str, role_summary: str) -> str:
    profile = profile_name.strip()
    summary = role_summary.strip()
    return f"""<identity_header>
Profile: {profile}
Role: {summary}
</identity_header>"""


def capability_block(capabilities: list[str]) -> str:
    lines = _clean_lines(capabilities)
    if not lines:
        return ""
    bullets = "\n".join(f"- {line}" for line in lines)
    return f"""<capabilities>
{bullets}
</capabilities>"""


def delegation_envelope_block() -> str:
    return """<delegation_envelope>
Use this structure when handing work to another bounded executor:
- [CONTEXT] Facts, files, constraints, prior results.
- [GOAL] Concrete outcome and acceptance criteria.
- [DOWNSTREAM] Evidence or follow-up the caller needs.
- [REQUEST] Immediate scoped action.
Keep delegation narrow and verifiable.
</delegation_envelope>"""


def search_agent_contract_block() -> str:
    return """<search_agent_contract>
Return repository or research discovery in this format:
<findings>
- Relevant facts, paths, APIs, or source references.
- Separate confirmed evidence from uncertainty.
</findings>
<results>
- Answer the underlying question directly.
- Give the next useful step when implied.
</results>
</search_agent_contract>"""


def prompt_activation_guidance_block(
    *,
    activation_id: str,
    mode: str,
    intent_slot: str,
    profile_refs: list[str],
) -> str:
    refs = _clean_lines(profile_refs)
    refs_line = ", ".join(refs) if refs else "runtime default"
    return (
        f"Activation {activation_id.strip()} mode={mode.strip()} intent={intent_slot.strip()}. "
        f"Policy refs: {refs_line}. "
        "Guidance-only; does not grant tools, delegation, or capabilities. "
        "Runtime policy and tool checks remain authoritative."
    )


def dynamic_boundary_marker() -> str:
    return _DYNAMIC_BOUNDARY_MARKER


__all__ = [
    "capability_block",
    "delegation_envelope_block",
    "dynamic_boundary_marker",
    "identity_header",
    "prompt_activation_guidance_block",
    "search_agent_contract_block",
]
