# Optional queries between plugins

A consumer depends on a named capability contract, never on an implementing plugin id,
module, owner state, or startup order. Both sides import the same shared capability contract. The core
routes its bounded JSON transport inside the caller's invocation; queries gain no writable
services and must remain read-only. Queries are a trusted-code convention, not a sandbox.

## Typed requests and answers

`voidcode.capabilities.workspace_queries` binds Pydantic request types to typed answer decoders.
The request model defines strict ingress validation, unknown-field rejection, and defaults.
The answer decoder validates the consumer's ingress, including request/answer correlation
where needed (a document answer must name the exact requested canonical path).

A provider registers the shared contract, without restating parameter parsing:

```python
from voidcode.sdk.plugin import Plugin, PluginQueryCaller
from voidcode.capabilities.workspace_queries import DOCUMENT_STATES, DocumentState, DocumentStateRequest


def document(request: DocumentStateRequest, caller: PluginQueryCaller) -> DocumentState | None:
    # Read your own document state, scoped by caller.workspace and caller.session_id.
    # Return None when the requested document is not open.
    ...


def create_plugin(config):
    return Plugin(queries=(DOCUMENT_STATES.bind(document),))
```

The consumer uses the same contract:

```python
from voidcode.capabilities.workspace_queries import DOCUMENT_STATES, DocumentStateRequest

state = DOCUMENT_STATES.ask(services, DocumentStateRequest(path="/workspace/style.py"))
```

No consumer checks whether `lsp` is enabled. `DOCUMENT_STATES` can be implemented
by any admitted plugin. Duplicate registrations for the same name still
fail visibly: disable one provider before admitting another. A breaking request/answer change
requires a new query name; do not silently change the meaning of an existing contract.
Custom contracts can use the same SDK API; the low-level `PluginServices.query` and
`PluginQuery` transport remain available for custom contracts.

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

Delegation uses [Markdown agent definitions](./agent-definitions.md). Built-ins
have no fixed tool tables. Unless a file declares standard `tools`, children
inherit the parent's scope. Explicit caller `tool_allowlist` and file `tools`
restrictions intersect; neither can widen the parent. The core validates names
and authority, and spawn failures are never retried with a wider scope.

The former custom profile metadata and serialization paths are removed. Standard
fields without an execution implementation fail visibly, rather than generating
warnings that a model preference or skill reference was not applied.

A built-in read-only stance restricts external mutation through core policy; it
does not mean filesystem-only inspection. Network and delegation tools within
the inherited scope can remain visible.

The separate review workflow still intersects its fixed inspection list with
`PluginServices.tool_definition`. This remaining concrete-name policy has not
been migrated as part of delegation simplification.

## Behavioral verification

`tests/integration/test_optional_workspace_queries.py` admits an independent SDK-only provider
with all official plugins disabled and drives real file tools through the core. It checks
substitution, provider absence, malformed answers, and unchanged files after refused edits.
The review and delegation integration suites also run their workflows without optional
inspection plugins or synthetic stand-in tools.
