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
Domain policy — what absence means for a concrete consumer, which answers are
malformed — lives with the domain contracts in the owning plugin READMEs
(files, lsp, delegation), not here.

`voidcode.capabilities.agent_queries` shares one more contract the same way
(`AGENT_DEFINITIONS.ask`/`bind`). The named-agent answer shape, provider
behavior, substitution rules, and profile narrowing are canonical in the
[delegation](../../src/voidcode/plugins/delegation/README.md) README.

## Distinct outcomes

- `QueryUnavailable`: no admitted implementation. The consumer chooses its documented
  optional behavior; it must not pretend the capability succeeded.
- `None`: a provider answered, but knows no matching resource.
- `QueryContractError`: a provider returned malformed or mismatched data. Report the failure;
  never treat it as ordinary absence.
- Other handler exceptions: preserve them as provider failures, without a broad fallback.

Absent-provider behavior for concrete file consumers lives in the owning
READMEs: [files](../../src/voidcode/plugins/files/README.md) and
[lsp](../../src/voidcode/plugins/lsp/README.md). Malformed answers remain
failures.

## Behavioral verification

`tests/integration/test_optional_workspace_queries.py` admits an independent SDK-only provider
with all official plugins disabled and drives real file tools through the core. It checks
substitution, provider absence, malformed answers, and unchanged files after refused edits.
`tests/integration/test_agent_definition_query.py` does the same for named agents:
delegation answers, any admitted plugin may substitute, an unknown name is `None`,
and absence is a visible `QueryUnavailable`.
The review and delegation integration suites also run their workflows without optional
inspection plugins or synthetic stand-in tools.
