# Workflow Mode Contract

VoidCode exposes workflow selection only through `workflow_mode`. Workflow modes are runtime-owned guidance declarations, not an execution engine, scheduler, DAG, authorization layer, or agent selector.

## Built-in modes

The built-in mode ids are `default`, `deep_work`, `review`, `product`, and `sustain`. A mode declares an id, description, and guidance-only hook preset references.

Resolution order is deterministic:

1. command `workflow_mode`
2. request metadata `workflow_mode`
3. `default`

Unknown modes fail before provider execution.

## Snapshot

Every fresh run materializes workflow contract version 2:

```json
{
  "snapshot_version": 2,
  "requested": {"workflow_mode": "review"},
  "effective": {"mode": "review", "source": "workflow_mode"},
  "mode": "review",
  "source": "workflow_mode"
}
```

The snapshot is persisted in runtime config and capability metadata. Readers require version 2 and the complete `requested`, `effective`, and top-level `mode` fields. Missing versions, unsupported versions, malformed shapes, and mismatched modes fail immediately. Runtime does not promote top-level fields, infer missing selectors, or rebuild old snapshot shapes.

## Boundaries

- Workflow mode adds prompt and hook guidance only.
- Tool access comes from the materialized capability snapshot and explicit runtime policy.
- Read-only behavior comes only from explicit runtime `mode` and `read_only` state.
- Agent, skill, MCP, delegation, and verification materialization remain owned by their respective runtime contracts.
- Clients cannot submit an internal workflow snapshot on a fresh request.
- Resume, replay, debug, and bundle import consume the stored versioned snapshot rather than recomputing it from mutable defaults.
