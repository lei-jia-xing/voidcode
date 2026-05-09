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


def env_card_stable(platform: str, model_id: str) -> str:
    return f"""<environment_stable>
Platform: {platform.strip()}
Model: {model_id.strip()}
</environment_stable>"""


def env_card_dynamic(cwd: str, today_iso: str, git_status_summary: str | None) -> str:
    git_status = "not provided"
    if git_status_summary is not None and git_status_summary.strip():
        git_status = git_status_summary.strip()
    return f"""<environment_dynamic>
Working directory: {cwd.strip()}
Date: {today_iso.strip()}
Git status: {git_status}
</environment_dynamic>"""


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


def dynamic_boundary_marker() -> str:
    return _DYNAMIC_BOUNDARY_MARKER


def assemble_sections(stable: list[str], dynamic: list[str], boundary: str) -> str:
    stable_sections = _clean_lines(stable)
    dynamic_sections = _clean_lines(dynamic)
    marker = boundary.strip()
    ordered_sections = [*stable_sections, marker, *dynamic_sections]
    return "\n\n".join(ordered_sections)


__all__ = [
    "assemble_sections",
    "capability_block",
    "delegation_envelope_block",
    "dynamic_boundary_marker",
    "env_card_dynamic",
    "env_card_stable",
    "identity_header",
    "search_agent_contract_block",
]
