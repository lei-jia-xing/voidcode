"""Fixture plugin source whose revision, state schema, and behavior can be rewritten in place.

Used by the P3 activation tests: the same plugin id is admitted as v1 and later re-admitted as
v2 with different tool output, a higher state schema version, and controlled migration or
deactivation behavior.
"""

from __future__ import annotations

import hashlib
import json
import textwrap
from pathlib import Path

from voidcode.sdk.manifest import (
    PluginCapabilityRequest,
    PluginFileIdentity,
    PluginManifest,
    PluginToolDeclaration,
)
from voidcode.sdk.tools import ToolEffect

PROBE_TOOL = "reload_probe"
DEACTIVATE_TOOL = "reload_state_note"


def write_reload_plugin(
    source: Path,
    *,
    plugin_id: str = "reload-fixture",
    version: str = "1.0.0",
    tool_output: str = "v1",
    module_name: str = "reload_plugin_impl",
    state_schema_version: int = 1,
    state_schema: dict | None = None,
    migration: str = "none",
    deactivation: str = "none",
    observe: bool = False,
    depends: tuple[str, ...] = (),
    syntax_error: bool = False,
    tools: bool = True,
    marker: Path | None = None,
    factory_error: bool = False,
) -> Path:
    """Write (or rewrite) the plugin source directory and return it."""
    source.mkdir(parents=True, exist_ok=True)
    # A stale manifest would hide an unchanged source from the revision identity.
    (source / "voidcode-plugin.json").write_text("{}", encoding="utf-8")
    body = _plugin_code(
        plugin_id=plugin_id,
        tool_output=tool_output,
        module_name=module_name,
        migration=migration,
        deactivation=deactivation,
        observe=observe,
        marker=marker,
        factory_error=factory_error,
    )
    module_path = source / f"{module_name}.py"
    module_path.write_text(body, encoding="utf-8")
    if syntax_error:
        module_path.write_text(body + "\nthis is not python(\n", encoding="utf-8")
    (source / "__init__.py").write_text("", encoding="utf-8")

    files = (
        PluginFileIdentity(path="__init__.py", sha256=hashlib.sha256(b"").hexdigest()),
        PluginFileIdentity(path=f"{module_name}.py", sha256=hashlib.sha256(module_path.read_bytes()).hexdigest()),
    )
    declarations = ()
    if tools:
        declarations = (
            PluginToolDeclaration(
                name=PROBE_TOOL,
                description=f"Probe the loaded revision ({tool_output}).",
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                effects=(ToolEffect.WRITE,),
            ),
            PluginToolDeclaration(
                name=DEACTIVATE_TOOL,
                description="Read the owner state this revision loaded.",
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                effects=(ToolEffect.READ,),
            ),
        )
    manifest = PluginManifest(
        plugin_id=plugin_id,
        version=version,
        entrypoint=f"{module_name}:create_plugin",
        files=files,
        capabilities=(PluginCapabilityRequest(name="workspace.write", reason="fixture probe"),),
        tools=declarations,
        depends=depends,
        state_schema_version=state_schema_version,
        state_schema=state_schema,
        observed_facts=("invocation_finished",) if observe else (),
    )
    (source / "voidcode-plugin.json").write_text(json.dumps(manifest.as_payload()), encoding="utf-8")
    return source


def _indent(body: str) -> str:
    """Indent a generated hook body to one level inside its function definition."""
    return "\n".join(("    " + line) if line.strip() else line for line in body.splitlines())


def _plugin_code(
    *,
    plugin_id: str,
    tool_output: str,
    module_name: str,
    migration: str,
    deactivation: str,
    observe: bool,
    marker: Path | None,
    factory_error: bool,
) -> str:
    marker_repr = "" if marker is None else str(marker)
    hang_body = (
        "import time\nfrom pathlib import Path\n"
        f"Path({marker_repr!r}).write_text('migrating')\n"
        "deadline = time.time() + 60\n"
        "while time.time() < deadline:\n"
        "    time.sleep(0.05)\n"
        "return state"
    )
    migrate_body = {
        "none": "return None",
        "ok": 'migrated = dict(state)\nmigrated["migrated_to"] = to_version\nreturn migrated',
        "raise": 'raise RuntimeError("migration refused by the candidate")',
        # A parametrized refusal: ValueError text is forwarded to the user verbatim.
        "refuse": 'raise ValueError("candidate refuses to migrate this state")',
        "crash": "import os\nos._exit(9)",
        "invalid": "return {'migrated_to': 'not-an-int' if True else 0, 'note': [1, 2]}",
        # Blocks (bounded) inside the candidate host so a test can kill the kernel at a
        # deterministic point before the activation transaction commits.
        "hang": hang_body,
    }[migration]
    deactivate_body = {
        "none": "return None",
        "pause": 'paused = dict(state)\npaused["paused"] = True\npaused["tip"] = tip_sequence\nreturn paused',
        "raise": 'raise RuntimeError("deactivation refused")',
    }[deactivation]
    observer_body = (
        textwrap.dedent(
            """
            def _observe(fact, context, services):
                state, version = services.get_state("workspace")
                merged = dict(state or {})
                merged["observed"] = merged.get("observed", 0) + 1
                services.replace_state(merged, expected_version=version, scope="workspace")
                return None
            """
        ).strip()
        if observe
        else ""
    )
    return f'''"""Generated reload fixture for plugin {plugin_id} (output {tool_output})."""

from __future__ import annotations

import json
import os
from pathlib import Path

from voidcode.sdk.plugin import Plugin, tool_definition
from voidcode.sdk.manifest import parse_plugin_manifest
from voidcode.sdk.tools import TextOutput, ToolSuccess

OUTPUT = {tool_output!r}


class Probe:
    def __init__(self, definition):
        self.definition = definition

    def invoke(self, call, *, context, services):
        state, version = services.get_state("session")
        merged = dict(state or {{}})
        merged["note"] = OUTPUT
        services.replace_state(merged, expected_version=version, scope="session")
        return ToolSuccess(call.tool_name, TextOutput(f"probe={{OUTPUT}}"))


class ReadNote:
    def __init__(self, definition):
        self.definition = definition

    def invoke(self, call, *, context, services):
        state, _version = services.get_state("session")
        return ToolSuccess(call.tool_name, TextOutput(json.dumps(dict(state or {{}}), sort_keys=True)))


def migrate_state(state, from_version, to_version):
{_indent(migrate_body)}


def prepare_deactivate(state, scope, scope_key, tip_sequence):
{_indent(deactivate_body)}
{observer_body}

def create_plugin(config):
    if {factory_error!r}:
        raise RuntimeError("fixture factory failure")
    if {str(marker) if marker else ""!r}:
        Path({str(marker) if marker else ""!r}).write_text(str(os.getpid()), encoding="utf-8")
    manifest = parse_plugin_manifest(json.loads(Path(__file__).with_name("voidcode-plugin.json").read_text()))
    tools = tuple(
        (Probe(tool_definition(declaration)) if declaration.name == "{PROBE_TOOL}" else ReadNote(tool_definition(declaration)))
        for declaration in manifest.tools
    )
    return Plugin(
        tools=tools,
        migrate_state=migrate_state if {migration!r} != "none" else None,
        prepare_deactivate=prepare_deactivate if {deactivation!r} != "none" else None,
        observer=_observe if {observe!r} else None,
    )
'''
