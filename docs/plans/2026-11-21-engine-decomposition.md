# Engine decomposition (2026-11-21)

Planning record for the next `engine.py` split after C1–C5. `Core` remains the
orchestrator; this document maps the current 168 methods, names the two safest
seams, and records the first implemented extraction. Anchors are from the
6,058-line tree on 2026-11-21; re-read before later steps.

Execution rules: serial pytest (`-n0`), no outer-layer edits, no forwarding
wrappers, no duplicate implementations. A moved method is deleted from `Core`;
internal callers use the collaborator. Public `Core` methods used by
bootstrap/transport/CLI stay on `Core`, so seams are drawn around internal-only
clusters.

## Landed status (2026-11-21, seam 1)

`core/execution.py` (87 lines) now owns `_Cursor`, `_Execution`,
`committed_event`, and the shared `wire_object` payload guard;
`core/context_projection.py` (303 lines) owns `ContextProjector.messages`,
adaptive re-projection, and the activation outcome note. `engine.py` is 5,736
lines (from 6,058; −322), `Core` has 164 methods (from 168), and the four
removed methods/helpers leave no wrapper or duplicate. Constructor/call-site
adaptation: `Core.__init__` creates `_context_projection`; four internal
`messages` call sites and two core-owned test files
(`test_ephemeral_steering.py`, `test_projection_headroom.py`) now address the
collaborator.

Serial verification: 33 passed (projection/steering/request-window/window/
model-query/streaming) + 33 passed (media/composite/activation integration),
`ruff check src/voidcode/core`, `ruff format --check src/voidcode/core`, and
`ty check src` green.

## Landed status (2026-11-21, seam 2)

`core/fact_delivery.py` (353 lines) now owns settlement candidates, pending
observer replay, eligible-owner decoding, durable gap diagnostics, observer root
authorization, ordered fact delivery, unknown-dispatch settlement, and the
rejected-proposal diagnostic path. `Core` is 5,499 lines (from 5,736; −237),
155 methods (from 164), method LOC 4,854 (from 5,088). The ten removed
methods/helpers have no callers and no wrappers; the only added `Core` method is
the narrow `birth_record` host decode.

Host protocol: `store`, `workspace`, `revision_id`,
`dispatch(description)`, `birth_record(session_id) -> BirthAuthorization |
None`, and the existing atomic `commit_staged_rebased` helper (renamed from
`_commit_staged_rebased`; same implementation, explicit host capability, no
wrapper). Core call sites route through `core._fact_delivery`; the two
generation-edge assertions use `eligible_observers` on the collaborator.

Zero-compat follow-up: the former `_observer_root_authorization` rootless
fallback is deleted. A child without a captured birth authorization, or any run
without a valid cursor root/budget, now raises `FactDeliveryContractError`
(a typed `ValueError`) instead of delivering on a rootless lease;
`test_generation_edges` pins both cases. The `_UnboundRun` sentinel is proven
current (public `cancel_session` passes it to mean “whichever run currently owns
the session”), so it is renamed `_UnboundCancel`/`_UNBOUND_CANCEL` and its
comments describe a real state, not legacy. Test-local `legacy` names in
`test_projection_headroom` remain as fixture labels for pre-inventory windows;
they are not delivery behavior and were left unchanged.

Serial verification: 85 passed (facts/generation/transcript/collaboration/
background-tool-wait) + 99 passed (seam-1 set, background actions/shutdown,
image/retry, activation integration); `ruff check src/voidcode/core`,
`ruff format --check src/voidcode/core`, and `ty check src` green. Zero-compat
follow-up rerun: 166 passed (fact/replay/delivery, cancellation, parked,
background actions/shutdown, projection headroom), same three checks green.

## Final status (2026-11-21, after public-fact routing and the type pass)

`Core.commit_public_staged`/`commit_public_fact` now give every non-turn public
fact (child spawn, command invoke/confirm, background invocation) the same
frozen-owner stamping and live delivery as a turn commit, and re-entrant
same-session delivery appends nested commits to the active queue so
observer-visible order equals durable sequence order. The typed lifecycle pass
decoded `SessionStatus`/`CheckpointKind`/`ContinuationStatus`/
`ConfirmationStatus` once in storage and made `StagedStateUpdate.schema_version`
required; the zero-compat batch removed the remaining fallbacks listed in the
seam-2 status.

Final tree: `engine.py` 5,633 lines (6,058 before seam 1; −425),
`fact_delivery.py` 371 lines (353 at seam 2; +18 for the delivery guard),
`context_projection.py` 305 lines, and `execution.py` 87 lines. `Core` still
defines 157 methods; no removed collaborator method has a wrapper or a second
implementation.

## 1. Method map

| Cluster | Methods (LOC) | State / dependencies | Writes | Verdict |
|---|---|---|---|---|
| Session truth/read projection | `create_session` 5, `_create_session_row` 9, `session` 7, `sessions` 4, `show` 28, `events_after` 5, `transcript_page` 9, `context_history` 7, `usage` 5, `_get_session_usage` 10 (89) | `store`, `workspace`, `revision_id`, `plugin_runtime`, `_usage` | none | Public surface; extraction would keep wrappers or require transport/CLI edits. Reject. |
| Run/resume driver | `run` 19, `_run` 135, `resume` 184, `_execute_user_followup` 120, `_drive` 404, `drain_continuations` 238, `_drive_pending` 45, `_consume_resume_intent` 40, `_drive_pending_activations` 53 (~1,238) | aborts, locks, delegation, activation, store | commits, checkpoints, dispatch | Highest governance risk. Last. |
| Ask park/resume | 9 methods, 324 LOC | store, commit, drive, background | commits + park adoption | Public `answer_ask`/`cancel_ask`; coupled to resume. Later. |
| Command invoke/confirm | 10 methods, ~491 LOC | `run`/`resume`/`show`/`usage`/drains, dispatcher | commits | Public; host protocol would be large. Later. |
| Structured model query | `governed_model_query*` 89, `_model_query_turn` 44, `_commit_model_query_event` 16 (~149; `_stream_provider_turn` belongs to dispatch) | snapshot, abort, dispatch, messages | one best-effort control event | Isolated, but public entrypoints. Candidate after seam 1. |
| **Context projection** | `_messages` 160, `_apply_projection_allocation` 54, `_activation_outcome_note` 26, `_ShrinkableToolProjection` 6 (246) | committed events, `store.window_projection`, `dispatch("context")`, `artifact_page`, `_event` lookup | caller `execution` fields only | **Seam 1**: internal-only, read-only over committed facts. |
| Observer/fact delivery | `_settlement_candidates` 22, `_replay_pending_observers` 29, `_eligible_observers` 13, `_observer_gap_event` 12, `_observer_root_authorization` 25, `_deliver_fact_for_event` 68, `_deliver_facts` 46, `_record_unknown_dispatch` 19, `_settle_unsettled_dispatches` 12 (246) | stored events, delegation birth records, receipts, atomic commit | observer receipts + diagnostic/usage events through `_commit_staged_rebased` | **Seam 2**: coherent settlement domain, but commit host is wide. Defer. |
| Durable commit/reconcile | `_assert_execution_prefix` 12, `_reconcile_execution_events` 34, `_commit_staged_rebased` 91, `_bind_checkpoint_refs` 15, `_stamp_fact_owners` 22, `_commit` 45 (219) | store, background waits, fact delivery | the only atomic event write path | Core invariant; move only with fact delivery as one commit owner. Last. |
| Result report/suspension | `_report` 165, `_interrupt_batch` 37, `_park_suspended_tool` 108, `artifact_page` 2, `read_artifact` 4 (316) | projector, snapshot, cursor, commit | cursor + tool facts | Tool-lifecycle central. Later. |
| Governance/visibility | `_request_window` 35, `_lifecycle_events` 25, `_gate_denial` 33, `session_read_only` 5, `_visible_tools` 13, `visible_tool_names_for_session` 8, `_visible_tool_definition` 21, `_denial` 14, `_needs_approval` 10, `_validate_*` 18, `_tool_schema_characters` 3, `_post_tool_hooks` 55 (240) | snapshot, store | none | Mixed public host members; later. |
| Cancellation/abort settlement | `_cancel_session_run` 45, `_settle_parked_cancel` 67, `_parked_run_id` 18, `_cancels_claimed` 11, `owner_cancel_claimed_work` 31, `_drive_session_abort` 5 (177 plus `cancel` 11) | session abort maps, store, commit | interruption facts | Owns real in-memory state but commit-coupled. Later. |
| Activation/background delegates | `_build_runtime` 39, `build_candidate_runtime` 7, `publish_activation` 18, `record_activation_refusal` 35, `_bound_target` 13, `_pending_activation_request` 16, `begin_activation` 10, `_clear_fence` 4 (+ thin delegates) | `ActivationCoordinator`, snapshot, fence, background | activation state | Already has a coordinator; no second owner. Later. |
| Wakeup/tick loop | `tick` 35, `_wakeup_loop` 10, `_start_background_work` 15, `_stop_background_work` 10 (70) | thread/event state, store, commit clock | thread lifecycle | Stateful and isolated, but small. Later. |

## 2. Chosen seams

### Seam 1 — context projection (implemented)

New `core/execution.py` owns the shared execution projection types
(`_Cursor`, `_Execution`) and two pure helpers: `committed_event(execution,
sequence, kind)` — the existing `_event` invariant — and `wire_object`, the
durable JSON-object guard. `_ShrinkableToolProjection` is projection-local and
lives in `core/context_projection.py`.

New `core/context_projection.py` owns provider-context construction:
committed-history replay, window-summary prefix, deferred tool-request lookup,
plugin context contributions, activation note, ephemeral steering consumption,
and shrink-only adaptive tool re-projection.

Why safe: every input is already-committed state; the only writes are the
caller's `_Execution` projection fields (`projection_allocation`,
`allocation_ineffective_tools`, cleared `ephemeral_steering`); it never commits,
never reads a mutable gen outside `dispatch`, and fails closed with
`ValueError`/`OSError`/`RuntimeError` as before.

Exact host protocol:

```python
class ProjectionSnapshot(Protocol):
    @property
    def plugin_runtime(self) -> PluginRuntime: ...


class ContextProjectionHost(Protocol):
    @property
    def store(self) -> SqliteStore: ...
    def dispatch(self, description: str) -> AbstractContextManager[ProjectionSnapshot]: ...
    def artifact_page(self, digest: str, offset: int, limit: int) -> ArtifactPage: ...


class ContextProjector:
    def messages(
        self, execution, *, allow_pending=False, completed_only=False, model=None, tool_schema_characters=0
    ) -> tuple[ProviderMessage, ...]: ...
```

`Core` keeps `store`, `dispatch`, `artifact_page`; `_messages` and
`_apply_projection_allocation` are deleted. Internal call sites
(`resume`, `_ask_resume_prelude`, `_model_query_turn`, `_drive`) and the two
core-owned tests that call `core._messages` use `core._context_projection.messages`.

Expected deltas: `engine.py` 6,058 → ~5,760 (−300 ± 15, including the moved
types/helper); new `execution.py` ~85 lines; new `context_projection.py` ~300
lines. `Core` gains one constructor pointer.

Acceptance (serial):
`tests/unit/core/test_projection_headroom.py`,
`test_ephemeral_steering.py`, `test_request_window.py`,
`test_window_projection.py`, `test_model_query_structured.py`,
`test_provider_streaming.py`, `test_media_projection.py`,
`test_composite_projection.py`, plus `tests/integration/test_core_activation.py`;
then `ruff check src/voidcode/core`, `ruff format --check src/voidcode/core`,
`ty check src`.

Stop conditions: any assertion change beyond constructor/call-site adaptation;
a provider request byte/length difference; a host capability beyond the three
members above; an import cycle; needing a second implementation of `_event`.

### Seam 2 — observer fact settlement/delivery (implemented)

`core/fact_delivery.py` owns `settlement_candidates`, `replay_pending`,
`eligible_observers`, `_observer_gap_event`, `_observer_root_authorization`,
`_deliver_event`, `deliver`, `_record_unknown_dispatch`, and
`settle_unsettled_dispatches`.

Host protocol: `store`, `workspace`, `revision_id`,
`dispatch(description)`, `birth_record(session_id) -> BirthAuthorization |
None`, and the existing atomic `commit_staged_rebased(execution, *, status,
kind, events, staged_state, staged_continuations, staged_cancellations,
staged_budgets, observer_receipts)`. The wide commit capability is the reason
this was second: it is the same atomic path every turn uses, so the commit
helper is a stable host contract, not reimplemented. Acceptance ran over the
facts/delegation suites (`test_facts.py`, `test_generation_edges.py`,
`test_transcript_contract.py`, `test_collaboration.py`,
`test_background_tool_wait.py`) plus the activation integration suite, and the
final tree adds the public-fact routing/order regression in `test_sdk_facts.py`.

## 3. Ordered next steps

1. Implement seam 1 (`execution.py`, `context_projection.py`, delete methods, adapt two tests). **Done.**
2. Run the seam-1 acceptance list and the three static checks; record counts. **Done.**
3. Only then extract seam 2 fact delivery, keeping `commit_staged_rebased` in
   `Core` as the durable commit authority. **Done.**
4. Re-map the remainder (run driver, command invoke, cancellation) after those
   two seams land; do not start with `_drive` or `invoke_command`. **Next.**
