# Optional queries between plugins

A consumer depends on a named capability contract, never on an implementing plugin id,
module, owner state, or startup order. Both sides import the same SDK contract. The core
routes its bounded JSON transport inside the caller's invocation; queries gain no writable
services and must remain read-only. Queries are a trusted-code convention, not a sandbox.

## Typed requests and answers

`voidcode.sdk.queries.QueryContract` binds a Pydantic request type to a typed answer decoder.
The request model defines strict ingress validation, unknown-field rejection, and defaults.
The answer decoder validates the consumer's ingress, including request/answer correlation
where needed (a document answer must name the exact requested canonical path).

A provider registers the shared contract, without restating parameter parsing:

```python
from voidcode.sdk.plugin import Plugin, PluginQueryCaller
from voidcode.sdk.workspace_queries import RULE_PAGES, RulePage, RulePageRequest


def page(request: RulePageRequest, caller: PluginQueryCaller) -> RulePage | None:
    # Read your own catalog, scoped by caller.workspace and caller.session_id.
    # Return None when the requested rule is absent.
    ...


def create_plugin(config):
    return Plugin(queries=(RULE_PAGES.bind(page),))
```

The consumer uses the same contract:

```python
from voidcode.sdk.workspace_queries import RULE_PAGES, RulePageRequest

page = RULE_PAGES.ask(services, RulePageRequest(uri="voidcode://rule/style"))
```

No consumer checks whether `skills` or `lsp` is enabled. `RULE_PAGES` and `DOCUMENT_STATES`
can be implemented by any admitted plugin. Duplicate registrations for the same name still
fail visibly: disable one provider before admitting another. A breaking request/answer change
requires a new query name; do not silently change the meaning of an existing contract.
Custom contracts can use the same SDK API; the low-level `PluginServices.query` and
`PluginQuery` transport remain available for compatibility.

## Distinct outcomes

- `QueryUnavailable`: no admitted implementation. The consumer chooses its documented
  optional behavior; it must not pretend the capability succeeded.
- `None`: a provider answered, but knows no matching resource.
- `QueryContractError`: a provider returned malformed or mismatched data. Report the failure;
  never treat it as ordinary absence.
- Other handler exceptions: preserve them as provider failures, without a broad fallback.

Ordinary file reads/writes work without either workspace query. A rule URI reports an absent
catalog; a version-constrained edit is refused when no document version exists. Hash-only
edits remain available without a document-state provider. Malformed answers remain failures.

## Optional tools in workflow profiles

Delegation profiles and review intersect their desired tools with the session's visible
catalog using `PluginServices.tool_definition`. Disabling `lsp` or `ast_grep` leaves the
available file tools usable; delegation reports the omitted names. If none of a profile's
inspection tools is visible, the workflow fails before spawning a child.

A profile never retries a rejected spawn with an inherited, wider parent scope. Spawn,
budget, policy, and lifecycle errors are preserved. Explicit caller-supplied allowlists still
undergo the core's strict validation rather than silently dropping unknown names.

## Behavioral verification

`tests/integration/test_optional_workspace_queries.py` admits an independent SDK-only provider
with all official plugins disabled and drives real file tools through the core. It checks
substitution, provider absence, malformed answers, and unchanged files after refused edits.
The review and delegation integration suites also run their workflows without optional
inspection plugins or synthetic stand-in tools.
