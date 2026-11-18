# SDK interaction contracts: (A) foreground suspension + eval bridge, (B) model-initiated persistent ask

Read-only design. No files changed. All anchors below were read in this session.

Status: ACCEPTED DESIGN, UNIMPLEMENTED (recorded 2026-11-18). Source: sdk-interact turn-1 design + turn-2 review corrections (both parent-accepted). Review corrections integrated in §0 (catch-site mechanics; swallowed-SuspendPark rule). Risks + open questions in A§7/B§7 are verbatim from the design and still open. Decisions D1-D3 recorded 2026-11-18.

## §0 Shared machinery (stated once — both contracts ride it, no forks)

Both contracts share ONE park/resume discipline: **suspend-by-unwind, park-by-commit, resume-by-explicit-entry, adopt-without-re-invoke**. There is no frame-holding primitive anywhere.

- **Suspend = unwind, not block.** A tool callback suspends by calling a `PluginServices` method that stages a wait-intent on the lease and then raises a dedicated `SuspendPark(BaseException)` carrying only JSON-ownable intent (ids + validated data, never a frame, never a services object). Raising (not returning, not blocking) is what makes "no retained frame" structural: the callback frame is unwound by definition. `BaseException` (not `Exception`) so ordinary plugin `except Exception` cannot swallow it. Core catches it at exactly one place: the `PluginRuntime.invoke` boundary (`src/voidcode/core/plugins.py:1136-1146`, direct synchronous call on the dispatch thread, staged work frozen at return, lease revoked).

**Accepted review correction — catch-site mechanics (integrated; supersedes the catch-site sentence above).** Current code catches only `Exception` (`src/voidcode/core/plugins.py:1139`), so a `SuspendPark(BaseException)` runs the `finally` at `:1147-1149` (watcher join) and then bypasses `:1150-1175` entirely — no staged freeze, no `:1172` revoke. Without explicit handling the lease leaks: active-lease entry, thread-count increment (`:740-741`), cancel event, claimed services, and artifact owner are all released only in `_revoke_lease` (`:744-758`); the thread-count leak would poison the leased-thread refusal (`:694-697`, enforced at `engine.py:972-979`) for that thread thereafter. Implementation adds a **new `except SuspendPark` clause** on the `invoke` try (`:1135-1149`) that explicitly: (a) revokes the lease via `_revoke_lease` in every sub-case; (b) freezes staged changes exactly like the return path — `_staged_changes(lease)` (mirror of `:1161`) **plus** `staged_observations` (mirror of `:1170`; `_staged_changes` itself excludes them), freeze-before-revoke mirroring `:1161`→`:1172`; (c) attaches the frozen staged outcome to the propagating `SuspendPark` (or a Core-internal wrapper) and re-raises — the invoke boundary never writes the store; **engine's `_invoke` park-catch performs the atomic park commit** (park event + row + checkpoint binding + `waiting`, per the approval-commit shape at `engine.py:5966-5973`; outcome-to-commit handoff mirrors `engine.py:6108-6119`). Clause order vs `except Exception` is moot; the `finally` (`:1147-1149`) runs unchanged. `cancelled`: **mirror, then engine re-checks** — compute the `:1150` expression in the handler; if set, discard all staged including the intent, run `_cancel_claimed_services` (mirror of `:1151-1155`), revoke, and return the cancelled-shaped outcome instead of parking, so `engine.py:6101-6104` settles the run with no new engine handling. The handler read is pre-join and best-effort, so the engine park-commit must re-check the abort (same `:6101-6104` check) before committing. `tool_threw`: **skip** — no failure; intent was validated at stage time (same site discipline as `invoke_tool` validation, `core/services.py:1316-1325`), unlike generic throws which keep only observations (`:1156-1159`).
- **Park = atomic commit, not a live handle.** On catching `SuspendPark`, Core commits (park event + durable row + checkpoint binding + session status `waiting`) in one `commit_staged`, exactly like the approval park (`src/voidcode/core/engine.py:5966-5973`: `waiting` + `APPROVAL_REQUESTED` + `checkpoint_bindings`). The callback's `ToolResult` return value is never produced (the raise precedes `return`), so there is no ambiguous "is it terminal or pending" value. Thrown-tool semantics otherwise stay: generic throws keep no staged state (`src/voidcode/core/plugins.py:1156-1159`); `SuspendPark` is the single typed exception that commits staged state, because its intent is explicit and pre-validated.

**Accepted review correction — swallowed-`SuspendPark` rule (integrated).** Staged suspend-intent + normal return = visible failure: never a silent park, never a silent drop. Plugin code can catch `BaseException` and return normally; the invariant ("staged intent requires `SuspendPark` unwind") is enforced **inside the `try`, at result validation (`:1136-1138`)** — not at outcome assembly (`:1160-1171` sits outside the guarded region, so raising there would propagate unguarded past `:1172`) and not at engine commit (the lease is revoked by then; engine must never recognize uncommittable lease residue). The suspend services stage the intent into a **dedicated lease field** (e.g. `lease.staged_suspend`), never an `EventDraft`, so it is uncommittable by construction. Exact check after `result = tool_instance.invoke(...)` (`:1136`), alongside the invalid-result check (`:1137-1138`): `if lease.staged_suspend is not None: raise TypeError(f"Tool {call.tool_name} staged a suspension but returned normally (SuspendPark was swallowed); staged intent discarded")` (`TypeError` matches the adjacent contract-violation style at `:1138`). The existing `except Exception` (`:1139-1146`) converts it to an ordinary `ToolFailure`, the `tool_threw` branch (`:1156-1159`) discards the staged intent by rule, the lease revokes normally at `:1172`, the run continues, and the discard is named in the committed failure. Throw paths need no check: a swallowed-then-differently-thrown suspend already discards all staged state by the documented throw rule.
- **Resume = explicit entry with identity checks, never replay.** Re-entry happens only through a named Core entry that re-takes the session lock, revalidates checkpoint identity and the parked binding, and continues `_drive` — the `resume()` precedent (`src/voidcode/core/engine.py:1633-1647`: pending-intent match, exact approval-request identity + decision). Resumption **adopts a staged terminal result without re-invoking plugin code** (identity-checked like deferred adoption: `src/voidcode/core/engine.py:6098-6099` result-identity check, `src/voidcode/core/engine.py:4071-4072` terminal-identity check). User code and question payloads therefore never execute twice.
- **Waiters hold no lease.** Any thread that blocks for a parked outcome must satisfy the `_assert_background_wait_unleased` discipline (`src/voidcode/core/engine.py:972-979`: refuses threads holding a plugin lease per `src/voidcode/core/plugins.py:694-697`, or a Core dispatch slot per `src/voidcode/core/engine.py:944-970`). Post-return service use keeps raising (`src/voidcode/core/services.py:62-68`). This is why the eval *driver* frame (below) is safe to retain while a *callback* frame is not: the driver holds a context handle, never a lease, across waits — the same argument that makes `BackgroundContext` sound (`src/voidcode/sdk/services.py:773-808`).
- **Cancellation/restart settle, never wedge.** Parked work settles at its safe boundary on cancel (`_settle_parked_cancel`, `src/voidcode/core/engine.py:1041-1053`), claimed-but-unattended rows report `interrupted` and are never auto-replayed (`src/voidcode/core/engine.py:4249-4252`, `src/voidcode/core/engine.py:3964-3971` `kernel_lost`), orphaned acting sessions are adopted only as `interrupted` (`src/voidcode/core/storage/store.py:2547-2548`), and `close()` cancels, drains admitted work, and joins drivers (`src/voidcode/core/engine.py:1152-1176`). Only committed `user_stop`/`owner_cancel` counts as cancellation (existing `ToolAwaitResult` rule, `src/voidcode/sdk/services.py:644-710`).
- **Revision binding everywhere.** Every park/resume carries `revision_id`; mismatches fail visibly (`invoke_tool` precedent `src/voidcode/core/services.py:1318-1319`; confirmation precedent `src/voidcode/core/engine.py:2474-2475`; gates dropped on activation, `src/voidcode/sdk/gates.py:177-181` as implemented in the reducer).

Shared SDK surface (new module `src/voidcode/sdk/suspend.py`; stdlib + pydantic only, no core imports — SDK is core-free today, verified by grep over `src/voidcode/sdk`):

```python
# src/voidcode/sdk/suspend.py
@dataclass(frozen=True, slots=True)
class ParkIdentity:
    park_id: str          # Core-assigned uuid4 hex at park commit (cf. TOOL_DEFERRED request_id, core/services.py:1326)
    session_id: str
    revision_id: str
    owner: str            # lease.owner of the suspending callback (sdk/services.py:815-818)
    def as_payload(self) -> dict[str, object]: ...
    @staticmethod
    def from_payload(payload: Mapping[str, object]) -> ParkIdentity: ...

class SuspendPark(BaseException):
    """Unwind the callback to park it. Carries intent only; caught only at PluginRuntime.invoke."""
    def __init__(self, park_kind: Literal["composition", "ask"], intent: Mapping[str, object]) -> None: ...
    @property
    def park_kind(self) -> Literal["composition", "ask"]: ...
    @property
    def intent(self) -> Mapping[str, object]: ...  # owned JSON (own_json_object)

class ParkError(ValueError): ...        # unknown / not-pending / already-terminal park
class ParkRevisionError(ValueError): ... # expected_revision / active-revision mismatch
```

Per-contract intent/terminal types live in their own modules (`foreground.py`, `ask.py`) and reuse this base. The two contracts differ only in *what* is waited on (driver-staged tool result vs. client-delivered answer) and *who* resumes (driver vs. client RPC).

---

# Contract A — Foreground suspension + the complete eval product bridge

## A(1) Exact proposed surface

New SDK module `src/voidcode/sdk/foreground.py` (frozen DTOs + one Protocol + pure validators; no core imports):

```python
# src/voidcode/sdk/foreground.py
@dataclass(frozen=True, slots=True)
class CompositionReceipt:
    composition_id: str   # stable id for logs/cancel correlation; equals the park_id
    tool_name: str        # the suspending (parked) tool, e.g. "eval_exec"
    depth: int            # lease.invocation_depth + 1, core-counted like ToolInvocationReceipt (sdk/services.py:628-642)
    def as_payload(self) -> dict[str, object]: ...

@dataclass(frozen=True, slots=True)
class CompositionDraft:
    """What the suspending callback asks the driver to run. JSON-ownable, size-honest."""
    cell_source: str
    namespace_key: str    # plugin-state key holding the cross-cell namespace
    tool_allowlist: tuple[str, ...] | None = None  # further narrowing only; never widening
    def as_payload(self) -> dict[str, object]: ...
    @staticmethod
    def from_payload(payload: Mapping[str, object]) -> CompositionDraft: ...

@runtime_checkable
class ForegroundContext(Protocol):
    """Driver handle for one claimed foreground composition. Mirrors BackgroundContext
    (sdk/services.py:773-808); the driver thread holds this, never a lease, across waits."""
    @property
    def composition_id(self) -> str: ...
    @property
    def session_id(self) -> str: ...
    @property
    def owner(self) -> str: ...
    @property
    def payload(self) -> Mapping[str, object]: ...  # immutable CompositionDraft.as_payload()
    @property
    def abort_signal(self) -> threading.Event: ...  # set on cancel/shutdown; driver polls (cf. sdk/services.py:787-790)
    def invoke[T](self, callback: Callable[[ToolContext, PluginServices], T]) -> T:
        """One atomic lease-bounded step: fresh owner/revision-bound lease, staged work commits
        before return; raises InterruptedError and commits nothing when aborted."""
        ...
    def wait_tool(self, receipt: ToolInvocationReceipt) -> ToolAwaitResult:
        """Wait unleased for this composition's exact deferred request. Reuses ToolAwaitResult
        (sdk/services.py:644-710) verbatim — no forked outcome type. Refused on any leased or
        dispatch thread (engine.py:972-979 discipline)."""
        ...
    def stage_terminal(self, result: ToolResult, *, for_tool: str) -> None:
        """Stage the parked call's terminal result for adoption. Single-shot (second call refused);
        data-only, no services cross. Core adopts it without re-invoking the tool."""
        ...

class SuspendComposition(SuspendPark):
    def __init__(self, draft: CompositionDraft, *, idempotency_key: str | None = None) -> None: ...
```

`PluginServices` (`src/voidcode/sdk/services.py:811-1385`) gains exactly one method:

```python
def suspend_foreground_composition(self, draft: CompositionDraft, *, idempotency_key: str | None = None) -> NoReturn:
    """Stage this composition's wait-intent on the lease and raise SuspendComposition (never returns).
    Tool-callback leases only; hook/command/background leases are refused visibly.
    With idempotency_key a retried stage returns the existing composition instead of a second one
    (spawn_child precedent, sdk/services.py:877-879)."""
    ...
```

Core surface (Core owns governance/persistence; names for the implementer, not SDK): `Core.park_foreground_composition(...)` (called from the `SuspendPark` catch site), `Core.stage_composition_terminal(...)` (driver entry; session-locked adopt + continue `_drive`), `Core.cancel_foreground_composition(...)` (via existing `cancel_session` + extended parked settle), `PluginRuntime.foreground_driver(owner)` (driver factory registry, mirroring `background_driver` used at `src/voidcode/core/engine.py:3469-3471`), claim/complete/settle row methods on the store mirroring `background_action` / `complete_background_action` (`src/voidcode/core/storage/store.py:3889`, `:3854`), new `EventType.COMPOSITION_PARKED` (+ reuse of `TOOL_DEFERRED` / `TOOL_COMPLETED[deferred]` / `TOOL_DEFERRED_CANCELLED` for inner traffic, `src/voidcode/core/events.py:15-16`). New cursor slots + checkpoint kind `composition_wait` (extends the `ready/approval_wait/terminal/interrupted` kinds, `src/voidcode/core/storage/store.py:2221`).

Product surface (plugin-owned, NOT Core — plugin-only Core): an `eval` plugin providing tool `eval_exec` (effects `{EXECUTE}` + `SESSION` for namespace staging; input = `{cell_source, namespace_key?}`; `output_schema` declaring its result body per `src/voidcode/sdk/tools.py:169-173`) and tool `eval_reset` (ordinary synchronous tool clearing the namespace key; never suspends). No builtin tools in Core.

## A(2) Semantics

**Lifecycle (owner / approval / lease / cancellation / persistence / restart):**
- *Owner.* The composition is owned by the suspending tool's plugin (`lease.owner`). The row records `(composition_id, session_id, owner, revision_id, root_action_id, initial_root_budget, origin_visible_tools, origin_readonly)` captured from the suspending lease exactly like deferred authority capture (`src/voidcode/core/services.py:1329-1345`) and background-claim validation (`src/voidcode/core/engine.py:3433-3455`). Only that owner’s registered foreground driver may be dispatched for it; anything else is a visible `StoreConflict`/config error, never a fallback.
- *Approval.* Two levels, both ordinary. (i) The `eval_exec` call itself is admitted like any tool: `_denial` (schema + read-only stance, `src/voidcode/core/engine.py:5533-5546`), `_gate_denial`, `_needs_approval` (`src/voidcode/core/engine.py:5548-5557`; `EXECUTE` needs approval in non-yolo modes). Arbitrary code execution is thus an *execution capability* governed by effects — explicitly not a sandbox claim. (ii) Every inner `await tool.*` goes through `invoke_tool` (`src/voidcode/core/services.py:1302-1351`) → `TOOL_DEFERRED` → `_drain_deferred_requests` (`src/voidcode/core/engine.py:1788-1886`) with the *same* admission stack as model calls: profile scope, captured-origin scope (`src/voidcode/core/engine.py:5934-5940`), definition-in-revision, read-only denial, gate denial, `_needs_approval`. Inner approvals reuse `APPROVAL_REQUESTED`/`APPROVAL_DECIDED` and the existing `resume(approval_request_id, decision)` entry; the session stays `waiting` throughout, and `RunResult.pending_approval` (existing field, `src/voidcode/sdk/client.py:281`) surfaces the inner gate — no new RunResult field. Approval is never authority: on inner-approval resume the gate is re-asked (`src/voidcode/core/engine.py:1690-1692`).
- *Lease.* The driver loop runs on a Core-supervised worker thread (claim/spawn shape of `_background_loop`, `src/voidcode/core/engine.py:3375-3422`). Each step runs under a fresh short lease via `ForegroundContext.invoke` (mirrors `_BackgroundContext.invoke`, `src/voidcode/core/engine.py:262-274`); staged state/events/enqueues commit per step. `wait_tool` and the park itself execute only on threads with no active lease and no dispatch slot (refusal precedent `src/voidcode/core/engine.py:972-979`). The services object never escapes the callback (`src/voidcode/sdk/services.py:792-800` doc rule). A suspended callback retains nothing: its frame was unwound by the raise, its lease revoked (`src/voidcode/core/plugins.py:1172`), and any post-return service use raises (`src/voidcode/core/services.py:66-68`).
- *Why no frame-holding (rejection, precise).* A callback frame cannot be suspended because: it runs synchronously on the dispatch thread (`src/voidcode/core/plugins.py:1136`); staged work freezes only at return (`src/voidcode/core/plugins.py:1161-1171`); the `TOOL_DEFERRED` request it staged commits atomically with its result and therefore cannot exist while the frame is still suspended; draining happens only after the lease returns (`src/voidcode/core/engine.py:1788-1796`); and any wait attempt from the leased thread is refused rather than deadlocked (`src/voidcode/core/engine.py:972-979`). Holding the frame would additionally pin the dispatch slot and the session lock. The driver-frame alternative is safe under exactly the lease/dispatch discipline: the driver thread is neither a lease thread nor a dispatch thread while waiting, stages work only through lease-bounded `invoke` commits, observes cancellation through the abort event (20 Hz mirror precedent `src/voidcode/core/plugins.py:814-830`), and dies with its abort/close join.
- *Cancellation.* `cancel_session` signals the live abort and settles parked work at its safe boundary (extended `_settle_parked_cancel`, `src/voidcode/core/engine.py:1041-1053`); the composition abort event is set; in-flight inner deferreds are settled via `TOOL_DEFERRED_CANCELLED` (precedent `src/voidcode/core/engine.py:3672-3794`, including kernel-denial of a live inner approval at `:3740-3759`); the driver exits; the row settles `cancelled`; the parked run settles `interrupted` with `stop_cause user_stop/owner_cancel`. Partial cell results are never reported as success (composite rule, SDK TODO acceptance). Claimed service trees of the last live step are stopped at cancellation time (precedent `src/voidcode/core/plugins.py:828-830`).
- *Persistence.* Append-only: `COMPOSITION_PARKED` event (binding target, carries `composition_id` + draft digest, never the full namespace), inner `TOOL_DEFERRED`/`TOOL_COMPLETED[deferred]`/`TOOL_DEFERRED_CANCELLED` traffic, and the adoption `TOOL_COMPLETED` for the parked call. One composition row (pending/claimed/running/terminal) holding the draft + step cursor so crash reports name what was lost. Checkpoint binding `composition_sequence` (+ `composition_id`) alongside the untouched `approval_sequence` (`src/voidcode/core/engine.py:585`), so an inner approval park and the outer composition park coexist in one cursor: kind flips `composition_wait` → `approval_wait` → back to `composition_wait` as the inner gate resolves, all fields preserved in the dumped cursor (dump precedent `src/voidcode/core/engine.py:3779`).
- *Restart.* Process loss: claimed/running rows become `interrupted` on next touch (stale-row precedent `src/voidcode/core/engine.py:4249-4252`); parked runs on orphaned sessions are adopted only as `interrupted` (`src/voidcode/core/storage/store.py:2547-2548`); waiters observe `kernel_lost`/`interrupted`, never replay (`src/voidcode/core/engine.py:3964-3971`); `close()` cancels, drains admitted work, and joins composition drivers alongside background drivers (`src/voidcode/core/engine.py:1152-1176`); a reload keeps supervised *processes* (`src/voidcode/core/engine.py:1173-1175`) but composition *driver threads* do not survive — user cell code is never auto-rerun. Exactly-once adoption: `stage_terminal` transitions the row pending→terminal under CAS (confirmation-CAS precedent `src/voidcode/core/storage/store.py:3002-3016`); a second stage raises visibly and the adoption commit happens once.

**Eval bridge (cells call visible tools through the ordinary pipeline):** the cell runs in the driver thread where the `tool.*` proxy does `ctx.invoke(lambda _c, s: s.invoke_tool(name, args, expected_revision=rev))` then `ctx.wait_tool(receipt)`. Visibility comes from `services.tool_definition(name)` (`src/voidcode/sdk/services.py:1065-1074` — same snapshot + same predicate as dispatch, so no second catalog) intersected with captured origin (`_visible_tools`, `src/voidcode/core/engine.py:5488-5500`); hidden/stale names fail visibly at drain (`src/voidcode/core/engine.py:5934-5944`). Typed bodies are consumed with `try_validate_tool_body` (`src/voidcode/sdk/result_schema.py:70-79`; `None` = undeclared, never valid; failures raise, never coerce). Cell outputs use `CompositeOutput` parts (`src/voidcode/sdk/tools.py:354-377`); model text via `composite_ordered_text` (`src/voidcode/sdk/tools.py:403-406`); complete data stays behind `ArtifactReference`s (never inline base64/JSON in model context); large/producer output spills via `spill_artifact_stream` (`src/voidcode/sdk/services.py:1174-1181`); mid-cell progress reuses `ToolContext.emit_tool_progress` + `TOOL_PROGRESS` (`src/voidcode/sdk/invocation.py:43`, `src/voidcode/core/plugins.py:1131-1133`, `src/voidcode/core/events.py:17`) — no new progress bus. Cross-cell namespace persists in session-scoped plugin state (`get_state`/`replace_state`, `src/voidcode/sdk/services.py:820-831`); `eval_reset` clears it synchronously.

**Error taxonomy (every failure named):** `SuspendOutsideTool` (`PermissionError`: hook lease per `src/voidcode/core/services.py:74-76`, command lease, or background-driver lease attempted suspension — commands/background have no run cursor to park); `CompositionAuthorityMissing` (`PermissionError`: suspending lease lacks root/origin capture, deferred-authority precedent `src/voidcode/core/engine.py:1851-1853`); `CompositionRevisionMismatch` (`ParkRevisionError`: draft/terminal revision ≠ active, precedent `src/voidcode/core/services.py:1318-1319`); `CompositionNotPending`/`CompositionUnknown` (`ParkError`); `CompositionAlreadyTerminal` (second `stage_terminal`; `StoreConflict` surfaced visibly, precedent `src/voidcode/core/storage/store.py:3015-3016`); `CompositionAdoptionMismatch` (`ValueError`: staged result `tool_name`/call ≠ parked call, precedents `src/voidcode/core/engine.py:6098-6099`, `:4071-4072`); `CompositionDriverMissing` (`RuntimeError`: owner registers no foreground driver, precedent `src/voidcode/core/engine.py:3470-3471`); `CompositionDriverFailed` (terminal failure carrying `Type: message`, precedent `src/voidcode/core/engine.py:3477-3483`); inner outcomes reuse `ToolAwaitResult.status` verbatim (`completed/interrupted/cancelled/kernel_lost/budget_exhausted`, `src/voidcode/sdk/services.py:658`) including typed `BudgetExhaustion` and `ExecutionObservation` (settled vs unknown stop, `src/voidcode/sdk/tools.py:432-445`); run cancel reuses `StopCause` (`user_stop/owner_cancel/shutdown`, `src/voidcode/sdk/hooks.py:20`) — no new cause registry.

## A(3) Layer placement
- *SDK (`sdk/suspend.py`, `sdk/foreground.py`):* frozen DTOs, the `ForegroundContext` Protocol, `SuspendComposition`, error types. Pure data + signatures; stdlib + pydantic + jsonschema only (SDK imports no core/capabilities/plugins — verified). Rationale: neutral contract any plugin, test fake, or future driver can implement; `FakeServices` gains a raising stub (model-query-stub precedent).
- *Core:* the `SuspendPark` catch site in `PluginRuntime.invoke`, park/resume/settle commits, composition row claim/complete, driver-thread pump + joins, inner-deferred drain for composition waiters (sibling of `_drain_pending_background_tool_waits`, `src/voidcode/core/engine.py:3835-3871`), generalized waiter-identity match (shared helper taking the waiter binding; additive `composition_id` payload key — existing `background_action_id` matching at `src/voidcode/core/engine.py:3926-3935` untouched), checkpoint/cursor slots, cancellation + restart settle. Rationale: Core owns governance, persistence, and scheduling; only Core may commit adoption without re-invoke.
- *Plugin (`eval` product):* `eval_exec`/`eval_reset` tools, the cell interpreter loop, the `tool.*` proxy, namespace (de)serialization into plugin state, `output_schema` declaration. Rationale: product capability; Core stays plugin-only (no builtin tools, no builtin providers).
- *TUI/Web:* render `COMPOSITION_PARKED` as a waiting status row with cancel (existing session-cancel path); no new views, no new RPC.

## A(4) Non-goals
No retained/reconstructed callback frames (no pickle/cloudpickle/fiber capture); no remote execution or sandbox claims (execution capability only, effects-governed); no second language runtime (Python stack only, per plugins TODO); no inline code in workflows (workflows keeps trusted-file-only semantics); no nested-privilege escape (inner calls can only narrow origin scope/stance/budget); no new progress bus, blob store, capability registry, or permission model; no suspension from commands, hooks, resource readers, or background drivers (refused, not emulated); no plugin-internal cancel API beyond session cancel + abort polling; no nested compositions (eval-in-cell refused via `CompositionNestedRefused` — cursor keeps a single composition slot, no stack; inner ASK inside a composition stays allowed, parking with both composition + ask bindings like the already-designed inner-approval coexistence).

## A(5) Compatibility
- *Codec/wire:* `encode/decode_tool_result` (`src/voidcode/sdk/codec.py:245-309`) byte-identical — suspension introduces no result variant. `COMPOSITION_PARKED` is a new event-type string flowing through existing frames (`src/voidcode/transport/projection.py:45-58` passes `type`+`payload` through); `transcript_entry` returns `None` for unknown types (`src/voidcode/core/transcript.py:90-91`) so `transcript` is honestly `null`; old TUI `_gate_changes` ignores unknown types (`src/voidcode/tui/terminal_app.py:462`). No `RunSpec`/`ResumeSpec`/`RunResult`/RPC changes. Cursor/checkpoint: purely additive optional fields + additive kind strings (approval path untouched).
- *Migration:* no existing callers (new surface). `PluginServices` gains one method: out-of-tree fakes implementing the Protocol need the raising stub (same as every prior services addition).
- *Byte-identical:* all existing flows — background waits, approvals, confirmations, deferred drains — produce identical events/payloads; waiter-match refactor must keep `background_action_id` semantics exact.

## A(6) Acceptance sketch
- `tests/unit/sdk/test_suspend.py`: `SuspendPark`/`ParkIdentity` strictness (non-empty ids, owned JSON, `BaseException`-ness, `except Exception` non-capture).
- `tests/unit/sdk/test_foreground.py`: draft/receipt validation, `ForegroundContext` protocol shape, double-`stage_terminal` refusal at the fake level.
- `tests/unit/core/test_foreground_composition.py` (real Core, native-SDK style of `tests/unit/core/test_background_tool_wait.py`): park → inner tool completes → adopt → run continues; inner approval allow/deny through existing resume; inner hidden/stale/revision-mismatch refusal; cancel mid-cell → `interrupted` + no partial success; driver failure → typed failure; driver loss → `kernel_lost`, never replay; second terminal → visible refusal; lease-use-after-suspend raises; suspend from command/hook/background refused.
- `tests/unit/plugins/test_eval_plugin.py`: cell → visible-tool call with typed-body validation (good + tampered), composite cell output ordering, namespace persist/reset across cells, `EXECUTE` denial under read-only stance.
- `tests/integration/test_eval_composition.py`: model turn → `eval_exec` → approval-gated inner write → allow → adopted result; restart-interrupted composition resumes the session honestly.
- TUI: composition-wait status row + cancel; no new views to test.
- `tests/unit/core/test_foreground_composition.py` (decided 2026-11-18): nested composition refused — `suspend_foreground_composition` from a composition-bound driver-step lease raises `CompositionNestedRefused` with nothing staged (single composition slot, no stack); inner ask from a composition-bound lease still parks with both composition + ask bindings.

## A(7) Risks + open questions
1. `_Cursor` strictness/old-checkpoint decodability: additive cursor fields assumed backward-compatible — not verified (did not read the `_Cursor` definition or checkpoint schema constraints). Core implementer must confirm pydantic `extra` policy + SQLite kind constraints (if any) before adding `composition_*` slots/kinds.
2. Nested `eval_exec`-in-cell (cell calls eval): proposal allows it (depth counted via `invocation_depth`, `src/voidcode/sdk/invocation.py:39`, bounded by captured root budget), but nested parks multiply checkpoint bindings — needs a Core design pass on cursor slot cardinality (single vs. stack).

   Decided (2026-11-18): against nesting — eval-in-cell is refused, so the cursor keeps a single composition slot (no stack). Exact rule: driver-step leases opened via `ForegroundContext.invoke` carry the composition binding (lease `composition_id`); `suspend_foreground_composition` raises `CompositionNestedRefused` (a `ParkError`) when the suspending lease is already composition-bound, failing visibly at stage time with nothing staged. `suspend_for_ask` from a composition-bound lease REMAINS allowed (inner ask parks with both composition + ask bindings, exactly like the already-designed inner-approval coexistence) — the Eval×ask synergy section stays valid.
3. Foreground driver + activation fence: driver holds `revision_id`; terminal staging after activation must be refused like `expected_revision` mismatches — rule stated, race with `_dispatch` fence (`src/voidcode/core/engine.py:944-970`) needs Core review.
4. Cell-namespace size: plugin state has no byte bound (stage path `src/voidcode/core/plugins.py:760-780` shows no size check); reusing it as-is means huge namespaces persist unbounded. No new bound proposed (would be MAX-by-fiat without a measured consumer); Core may later need an honest store-bound error.
5. Inner-approval UX while composition-parked: `pending_approval` reuse means the client sees an approval gate with no visible composition parent; acceptable for v1, may need a parent link later.

---

# Contract B — Model-initiated persistent user ask

## B(1) Exact proposed surface

New SDK module `src/voidcode/sdk/ask.py` (frozen DTOs + one pure validator + errors; stdlib + pydantic + jsonschema only):

```python
# src/voidcode/sdk/ask.py
AskAnswerStatus = Literal["answered", "cancelled", "expired"]

@dataclass(frozen=True, slots=True)
class AskMultiSelect:
    """The ONE new field shape: multi-choice. Everything single-valued reuses FormField."""
    name: str
    label: str
    options: tuple[ChoiceOption, ...]          # reused (sdk/views.py:107-111)
    required: bool = True
    min_selections: int = 0
    max_selections: int | None = None
    context: str | None = None                 # per-question help text (new, plain markdown)
    def as_payload(self) -> dict[str, object]: ...
    @staticmethod
    def from_payload(payload: Mapping[str, object]) -> AskMultiSelect: ...

type AskQuestion = FormField | AskMultiSelect   # FormField reused verbatim (sdk/views.py:142-157):
                                                # text/multiline/integer/number/boolean + single-select options

AskContextKind = Literal["notice", "markdown", "fields", "diff", "status", "table", "json", "media"]
@dataclass(frozen=True, slots=True)
class AskContextBlock:
    """One context block reusing an existing view DTO (validated by validate_view_data,
    sdk/views.py:320-332, hence MAX_VIEW_DATA_BYTES-bounded, sdk/views.py:311). No new renderer."""
    view_type: AskContextKind
    data: Mapping[str, object]
    def as_payload(self) -> dict[str, object]: ...

@dataclass(frozen=True, slots=True)
class AskRequestDraft:
    """What the model/plugin stages. No ids (Core assigns), no permissions, no defaults-as-answers."""
    title: str
    questions: tuple[AskQuestion, ...]
    context: tuple[AskContextBlock, ...] = ()
    media: tuple[ArtifactReference, ...] = ()  # optional question images; content-addressed store reuse
    timeout_ms: int | None = None              # caller-declared only; None = wait (no Core default)
    def as_payload(self) -> dict[str, object]: ...
    @staticmethod
    def from_payload(payload: Mapping[str, object]) -> AskRequestDraft: ...

@dataclass(frozen=True, slots=True)
class AskRequest:
    request_id: str                            # Core-assigned at park (approval precedent engine.py:5954)
    session_id: str
    revision_id: str
    owner: str
    title: str
    questions: tuple[AskQuestion, ...]
    context: tuple[AskContextBlock, ...]
    media: tuple[ArtifactReference, ...]
    timeout_ms: int | None
    def as_payload(self) -> dict[str, object]: ...
    @staticmethod
    def from_payload(payload: Mapping[str, object]) -> AskRequest: ...

@dataclass(frozen=True, slots=True)
class AskAnswer:
    request_id: str
    values: Mapping[str, object]               # Core-validated typed values (bool/int/float/str/tuple-of-ids)
    status: AskAnswerStatus
    terminal_sequence: int
    def as_payload(self) -> dict[str, object]: ...

@dataclass(frozen=True, slots=True)
class AskResultBody:
    """Typed tool-result body for the adopted ask success (validated by consumers via
    try_validate_tool_body against the ask tool's output_schema)."""
    answers: Mapping[str, object]
    status: Literal["answered"] = "answered"
    def as_payload(self) -> dict[str, object]: ...

def validate_ask_answer(request: AskRequest, values: Mapping[str, object]) -> dict[str, object]:
    """Strict shared validator used by BOTH clients and Core (single implementation, no drift).
    Mirrors validate_form_submission (sdk/views.py:481-556): absent-vs-blank tri-state, strict integer/
    number literals (sdk/views.py:454-455), boolean tri-state, options membership, text verbatim
    (sdk/views.py:553-555), omit-rather-than-fabricate (sdk/views.py:538); plus multi membership +
    min/max_selections. Raises ValueError visibly; no coercion, no default filling."""
    ...

class SuspendAsk(SuspendPark):
    def __init__(self, draft: AskRequestDraft, *, idempotency_key: str | None = None) -> None: ...
class AskError(ValueError): ...          # unknown / not-pending / already-answered / already-parked
class AskValidationError(ValueError): ... # validate_ask_answer failure, carrying field + reason
```

`PluginServices` gains one method (sibling of the A-method):

```python
def suspend_for_ask(self, draft: AskRequestDraft, *, idempotency_key: str | None = None) -> NoReturn:
    """Validate refs/shape on the lease, stage the ask intent, raise SuspendAsk (never returns).
    Tool-callback leases only. Media digests are shape-checked here; existence is resolved by the
    plugin via read_artifact BEFORE suspending (FileNotFoundError = visible stage error, no park;
    sdk/services.py:1183-1196)."""
    ...
```

Client/run surface: `RunSpec`/`ResumeSpec` (`src/voidcode/sdk/client.py:232-268`) gain one optional field each:

```python
interaction: Literal["interactive", "unattended"] | None = None  # None = interactive (default preserves behavior)
```

Request-scoped declaration by the caller (a headless runner declares `unattended`); NOT a registry. Transport `RunRequest`/`ResumeRequest` (`src/voidcode/transport/models.py:52-72`) gain the same optional field, omitted-when-None. `RunResult` (`src/voidcode/sdk/client.py:271-314`) gains `pending_ask: Mapping[str, object] | None = None`, **omitted from `as_payload` when None** (unlike always-present `pending_approval`) so all existing flows stay byte-identical. When `unattended`, both ask and approval parks are refused with a typed `ToolFailure` — see the B§7 item 3 decision (2026-11-18).

Core surface: `Core.answer_ask(session_id, request_id, values, *, expected_revision) -> CoreResult`, `Core.cancel_ask(session_id, request_id, *, expected_revision) -> CoreResult` (both session-locked, resume-like); new `EventType.ASK_REQUESTED` / `EventType.ASK_ANSWERED`; ask row table mirroring `action_confirmations` (`src/voidcode/core/storage/store.py:2971-3016`: request/session/owner/revision/questions/status/answer/decided_at + CAS transition); cursor slots `ask_sequence`/`ask_request_id` + checkpoint kind `ask_wait`; expiry enforcement on session-touch + the existing 1s background-loop wake (`src/voidcode/core/engine.py:3404`, no new thread). RPC: `ask.answer`, `ask.cancel` (mirroring `action.confirm`, `src/voidcode/transport/rpc.py:92-96`).

Gates surface (SDK `gates.py`, additive): `AskGate` (request + revision_id, mirroring `ConfirmationGate`, `src/voidcode/sdk/gates.py:56-85`), `AskRequested` (mirroring `ApprovalRequested`/`ConfirmationRequested`, `:91-98`); `GateDecided` reused for terminal tombstones; `pending_gates` order (`:194-198`) becomes approval-first, then ask+confirmations in arrival order; activation drops stale asks (reducer precedent `:177-181`).

Product surface (plugin-owned): an `ask` plugin tool (effects `{SESSION}` — internal session data, available under read-only per `src/voidcode/core/plugins.py:1101-1103`, never approval-gated per `_needs_approval`, `src/voidcode/core/engine.py:5548-5553`); `output_schema` declaring `AskResultBody`; callback builds the draft from arguments and calls `suspend_for_ask`. Questioning the user in plan/read-only mode stays allowed (asking mutates nothing external); answers grant nothing.

## B(2) Semantics

**Question types / context / validation / media:** single-valued questions reuse `FormField` verbatim (5 scalar kinds + single-select `options` with unique non-empty ids, `src/voidcode/sdk/views.py:142-157`); multi-choice is the one new shape (`AskMultiSelect` with caller-declared `min/max_selections`); free input is `text`/`multiline` (verbatim, never trimmed/coerced); validation = `required` + options membership (reused) + multi bounds (new) enforced by the single shared `validate_ask_answer` (Core revalidates on answer; clients pre-validate for inline errors — same function, no drift); per-question `context` help text (new, plain string) + per-request `context` blocks reusing 8 existing view DTOs through `validate_view_data` (so each block inherits the `MAX_VIEW_DATA_BYTES` client-render bound — consumer: TUI/Web render buffers + durable event payloads; the bound is reused, not invented); option-diffs render via reused `DiffViewData`, tables/images via `Table/Json/MediaViewData` (acceptance: 方案差异 + 图片 + 3 validated fields, all from existing DTOs); media travels as `ArtifactReference` (digest-typed, `src/voidcode/sdk/services.py:481-493`), read through digest-only `read_artifact` (resource boundary preserved; no writable paths, no second blob store).

**Association + lifecycle:** park commits `ASK_REQUESTED` (full `AskRequest` JSON — the durable source clients render from) + row (`pending`) + checkpoint binding + session `waiting`, atomically on the `SuspendAsk` catch path (approval-commit precedent). Answer path (`answer_ask`): session lock → row must be `pending` and session/revision must match (confirmation precedents `src/voidcode/core/engine.py:2469-2475`) → `validate_ask_answer` → CAS row → commit `ASK_ANSWERED{status: answered}` + adopt `TOOL_COMPLETED` for the parked call (`ToolSuccess(body=AskResultBody, output=TextOutput(rendered Q&A))` — text carries every answer for model projection since bodies are omitted from projection like other tools, while the body preserves bool/numeric/array types for scripted consumers such as eval) → clear park binding → `_drive` continues. The run **continues** after answer/cancel/expire (deny precedent `src/voidcode/core/engine.py:1678` reports failure and continues — the model adapts; only session/owner cancel interrupts the run).
- *Reconnect:* parked ask is durable; client resubscribes from its applied cursor (`src/voidcode/transport/rpc.py:174-176` never skips) or reads `session.show(events=true)` (`src/voidcode/transport/rpc.py:58`); the `ASK_REQUESTED` event re-renders the identical form; in-flight drafts are client-local and lost on disconnect (honest: drafts were never committed).
- *Switch:* answer/cancel require `expected_revision` = row `revision_id` = active revision (precedent `src/voidcode/core/engine.py:2467-2475`); activation drops rendered asks via gate reducer + client overlay revision checks (`src/voidcode/tui/terminal_app.py:1639-1641` queued-confirmation drop precedent); stale answers raise visibly, never apply.
- *Timeout:* `timeout_ms` is caller-declared (positive SQLite INTEGER; grant-validation shape `src/voidcode/sdk/client.py:226-229`); `None` waits indefinitely like approvals. Expiry settles row → `expired`, commits `ASK_ANSWERED{expired}` + `TOOL_COMPLETED` typed-expiry failure, run continues. Enforcement: every session-locked entry + the existing 1s loop wake (no new thread; the wake's consumer is caller-declared timeouts + parked-session resource hold — not MAX-by-fiat).
- *Unattended:* a run declaring `interaction="unattended"` refuses the ask park at suspend-commit with a typed `ToolFailure` (run continues; model sees explicit unsupported, never a fabricated default per `src/voidcode/sdk/views.py:538`, never a hang). Interactive runs with no client attached wait honestly (like approvals) until answer/cancel/expire/session-cancel. The same per-run gate refuses the approval park at the park-commit site — see the B§7 item 3 decision (2026-11-18).
- *User cancel:* `cancel_ask` CASes the row and adopts a `ToolFailure("user cancelled…")` (deny-message precedent), run continues. Distinct from session cancel (`cancel_session` → parked settle → `TOOL_INTERRUPTED` + `RUN_INTERRUPTED`, `stop_cause user_stop/owner_cancel`).
- *Exactly-once:* row CAS `pending`→terminal (precedent `src/voidcode/core/storage/store.py:3002-3016` raises when not pending); second answer/cancel raises `AskError(already …)`; client double-submit is further absorbed by `GateDecided` tombstones (`src/voidcode/sdk/gates.py:170-175`); adoption commits once (no re-invoke, so no double-apply even under retry; `idempotency_key` dedupes the *stage* side like `spawn_child`).
- *No permission grant / no approval bypass:* the answer is data in a result body; downstream tool calls are re-admitted from scratch ("approval is not authority" precedent `src/voidcode/core/engine.py:1690-1692`); no answer field names tools, scopes, or decisions; the ask tool's own `{SESSION}` effects cannot mutate the world and cannot satisfy any gate.

**TUI/Web consumption (reuse + only the missing parts):** TUI handles `ASK_REQUESTED`/`ASK_ANSWERED` in `_gate_changes` (`src/voidcode/tui/terminal_app.py:431-462`) exactly like `APPROVAL_REQUESTED` (bespoke gate, NOT the `validate_view_data` pipeline — so no `ConfirmationUIType`/`_VIEW_ADAPTERS` change; old clients ignore the event honestly at `:462`). Rendering reuses: `InteractiveForm` field editors + `validation_error` + `Esc`-cancel + `Ctrl+S`-submit (`src/voidcode/tui/views.py:873-996`), `render_plugin_view` for context blocks (`:1066-1096`), `render_media`/`render_diff`/`render_table` for attachments, `revision_id` binding (`:881-883`), `InteractiveApproval` as the gate-panel pattern (`:785-870`). Missing parts to add (only these): multi-select toggle editor, per-question context line, `AskGate` panel + `AskRequested` fold, `pending_ask` inline handling on run return. Web consumes the same `AskRequest` JSON (composite-frontend precedent). Unsupported clients: unknown events ignored + session stays cancellably `waiting`; old strict decoders fail visibly on new shapes (never misrender); headless declares `unattended` (never hangs, never fabricates).

**Error taxonomy:** `SuspendOutsideTool` (`PermissionError`, same rule as A); `AskAlreadyParked` (`AskError`: session holds a live park — one park binding per cursor, second suspend refused); `AskUnknown`/`AskNotPending`/`AskAlreadyAnswered` (`AskError`; CAS precedent); `AskRevisionStale` (`ParkRevisionError`); `AskValidationFailed` (`AskValidationError`: field + reason; no coercion); `AskExpired` / `AskCancelled` / `AskUnattended` (terminal `ToolFailure`s with typed messages; run continues); `AskMediaMissing` (`FileNotFoundError` at pre-suspend `read_artifact`, no park); `AskTimeoutInvalid` (`ValueError` at draft validation). Stop causes reuse `StopCause` only.

## B(3) Layer placement
- *SDK (`sdk/suspend.py` shared + `sdk/ask.py`):* DTOs, `AskMultiSelect`, `validate_ask_answer`, `SuspendAsk`, errors, `AskGate`/`AskRequested` gate additions, `interaction` field on `RunSpec`/`ResumeSpec`. Rationale: neutral, client-shared contract (both TUI/Web and Core link the same validator — the anti-drift property); no core imports.
- *Core:* suspend-catch park commit, ask rows + CAS, `answer_ask`/`cancel_ask` resume entries, expiry sweep, unattended refusal, adoption commits, revision checks, event types, cursor/kind slots. Rationale: governance (validation enforcement, exactly-once), persistence (rows/events/checkpoints), scheduling (expiry, settle). Answers are client data entering the trusted commit path — only Core may admit them.
- *Plugin (`ask` product):* the `ask` tool (argument→draft mapping, media pre-resolution, `output_schema`). Rationale: product capability; Core gains no builtin tools.
- *TUI/Web:* gate panels + form editors consuming SDK DTOs (above). Rationale: presentation only; validation logic stays in SDK.

## B(4) Non-goals
No HTML/DSL form language; no new renderer protocol or view kinds; no permission/role/multi-user routing; no post-commit answer editing (append-only — a correction is a new ask); no polling (subscriptions + existing conds); no regex/pattern validation (free text stays verbatim); no media *answers* (user uploads have no ask-path ingress; v1 answers are typed values only); no second question/answer registry (questions live on the row); no approval semantic reuse (asks are never approvals — separate events, separate gates, separate resume entries).

## B(5) Compatibility
- *Codec/wire:* no `ToolResult`/codec change (answers are body JSON + rendered text); no `ConfirmationUIType`/`_VIEW_ADAPTERS` change (ask renders via the approval-style gate path, not the views pipeline); new event types are inert to old readers (`transcript_entry → None`, `src/voidcode/core/transcript.py:90-91`; projection `transcript: null`, `src/voidcode/transport/projection.py:57`; TUI ignores, `src/voidcode/tui/terminal_app.py:462`). `interaction` and `pending_ask` are omitted-when-None → existing `RunSpec`/`ResumeSpec`/`RunRequest`/`ResumeRequest`/`RunResult` payloads byte-identical. New RPCs `ask.answer`/`ask.cancel` are additive (`METHODS` table precedent `src/voidcode/transport/rpc.py:46-121`).
- *Migration:* no existing callers; `PluginServices` +1 method (fake stub needed, as usual); TUI adds gate arms + multi-select editor; Web adds one form renderer.
- *Byte-identical:* every flow not involving the new `ask` tool — approvals, confirmations, runs, resumes, drains — is untouched.

## B(6) Acceptance sketch
- `tests/unit/sdk/test_ask.py`: draft/request strictness; `validate_ask_answer` matrix (tri-state bools, strict numerics incl. `1e3`/non-finite rejection, single/multi membership, min/max selections, absent-vs-blank nullability, verbatim text, no coercion, media digest shape); unknown-extra rejection.
- `tests/unit/core/test_ask.py` (real Core): park → answer → adopted typed body + continued run; cancel → failure + continued run; double answer → visible refusal; stale revision → refusal; expiry (short caller timeout) → typed failure + continued run; unattended run → immediate typed refusal, no park; reconnect (re-read `ASK_REQUESTED` after close/reopen) re-renders; session-cancel while parked → `interrupted`; suspend from command/hook/background refused; second suspend while parked refused.
- `tests/unit/plugins/test_ask_plugin.py`: argument→draft mapping, media pre-resolution failure (no park), `output_schema` + consumer `try_validate_tool_body` round-trip, `{SESSION}` effects under read-only stance (allowed) and approval-mode (never gated).
- `tests/integration/test_ask_flow.py`: model turn → ask → TUI-equivalent answer via RPC → downstream tool still approval-gated (no bypass); expired ask + follow-up run.
- TUI (`tui` suite) + frontend (vitest): multi-select/context/media render, inline validation errors, Esc-cancel, double-submit absorption, revision-drop on activation.
- `tests/unit/core/test_ask.py` (decided 2026-11-18, D1): model batch with two parallel asks fails the second visibly with `AskAlreadyParked`; sequential asks park and resolve in order; no park queue.
- `tests/unit/core/test_ask.py` (decided 2026-11-18, D3): run with `interaction="unattended"` refuses both ask and approval parks with a typed `ToolFailure` (run continues, explicit unsupported); `ResumeSpec.interaction` overrides the mode for the resumed run; interactive runs unchanged.

## B(7) Risks + open questions
1. Same `_Cursor`/checkpoint-kind verification as A7.1 (shared slots/kinds change) — must confirm before implementation.
2. One-park-per-cursor: second suspend while parked is refused (`AskAlreadyParked`). A model batch with two parallel asks therefore fails the second visibly rather than queueing. Alternative (park queue) was rejected as scope creep; confirm product accepts sequential asks.

   Decided (2026-11-18): sequential asks accepted — a model batch with two parallel asks fails the second visibly with `AskAlreadyParked` rather than queueing. Sequential asks are the supported shape; no park queue, now or later, unless a new consumer justifies it.
3. `interaction` placement: run-scoped declaration chosen over session config so headless and interactive callers can alternate on one session. Open: should `ResumeSpec.interaction` override the parked run's mode (proposed yes — resume re-declares), and should approvals also honor `unattended` (proposed no — out of scope, but headless runners parking on approvals is the adjacent gap).

   Decided (2026-11-18): wider scope — a run declaring `interaction="unattended"` refuses the approval park at the park-commit site with a typed `ToolFailure` (approval unsupported while unattended; the run continues and the model sees explicit unsupported, never a hang, never a fabricated allow). `ResumeSpec.interaction` re-declares the mode for the resumed run (override, per-run). Interactive runs are unchanged. Approvals and asks share the unattended gate; the gate is per-run declaration, never session config, never a registry.
4. Expiry sweep piggybacks the 1s background loop; worst-case expiry latency ~1s + lock contention. No new thread proposed. If Core ever idles the loop, expiry stalls — implementer must keep the loop unconditional (`src/voidcode/core/engine.py:3382-3404` shape).
5. Client drafts are uncommitted and lost on disconnect; only committed answers are durable. Confirm product accepts draft loss (alternative: draft staging RPC — rejected, no consumer).
6. No numeric min/max proposed for scalar fields (only multi bounds) — v1 validation is required/membership/multi-bounds. If acceptance's "three validated fields" needs ranges, add caller-declared `min_value`/`max_value` then (caller-declared, not fiat) — left open deliberately.

---

## Cross-contract notes
- Shared implementation surface in Core: one `SuspendPark` catch site, one park-commit helper (event + row + binding + `waiting`), one waiter-identity matcher generalized over `background_action_id`/`composition_id`, one parked-settle extension (cancel/close/orphan), one cursor-slots migration. Two row tables (compositions, asks) — separate because their payloads, resumers, and terminals genuinely differ; unified only in discipline (§0).
- Eval×ask synergy is free: an eval cell can call the `ask` tool through the ordinary inner pipeline (park-within-composition: inner ask parks the session with both bindings, like inner approval) and consume `AskResultBody` via `try_validate_tool_body`. No extra contract needed.
- Available for review iterations; no files written, no tests run (design task, read-only as instructed).

Recording note (2026-11-18): accepted, unimplemented. Implement from this doc; open questions live in A§7/B§7 (B7.2 sequential asks decided 2026-11-18 — sequential asks accepted; all other items still open).
