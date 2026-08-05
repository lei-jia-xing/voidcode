# Runtime Architecture Refactor Contract

## Objective

Runtime control-plane behavior must have one current, versioned contract. Request configuration, persisted session facts, turn-local state, and observability records must not share ambiguous fallback semantics.

## Hard Rules

1. Public request fields are validated before provider or tool execution.
2. Persisted snapshots require an explicit current version and complete required fields.
3. Missing, unsupported, or malformed persisted contracts fail immediately.
4. Runtime never reconstructs persisted truth from mutable workspace defaults.
5. A behavior has one selector and one authoritative owner.
6. Clients, graph implementations, providers, and tools do not duplicate runtime governance state.
7. Internal collaborator ownership is reflected directly in call sites; `VoidCodeRuntime` does not expose proxy properties for collaborator internals.

## Metadata Ownership

| Class | Owner | Examples |
|---|---|---|
| Request configuration | request validator/materializer | agent, workflow mode, skills, runtime mode |
| Persisted facts | runtime snapshot contracts | capability, policy, workflow, skill binding |
| Turn-local state | run/resume coordinators | provider attempt, abort state, pending interaction |
| Observability | event/debug surfaces | policy decisions, tool status, context pressure |

Fields cannot move between these classes through inference. Persisted facts are read through their versioned parser; request-only fields cannot be injected as internal runtime state.

## Current Boundaries

- `runtime/service.py` owns the public runtime facade and delegates execution to runtime collaborators.
- `runtime/run_loop.py` owns graph/provider/tool progression.
- `runtime/resume.py` owns approval, question, and provider-failure continuation.
- `runtime/background_tasks.py` owns task lifecycle and worker state.
- `runtime/tool_scope.py` owns effective tool visibility and raw-call policy decisions.
- `runtime/config_materializer.py` owns persisted runtime configuration parsing.
- Snapshot modules own strict version validation for their respective artifacts.

## Change Gate

A runtime change is complete only when:

- the authoritative contract is explicit;
- removed fields and versions are rejected, not ignored;
- fresh run, resume, replay, debug, and bundle behavior agree;
- tests cover current materialization and rejection boundaries;
- repository searches show no alternate parser, alias, or silent synthesis path.
