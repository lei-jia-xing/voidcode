# Capability Activation

Same-session hot update for the kernel: one workspace runs exactly one active
generation, and a switch is a single SQLite transaction that either commits once or
leaves the previous generation untouched. This document is the authoritative contract for
admission, activation, and continuation behavior.

## Scope and limits

- Trusted Python, not an OS sandbox. Plugins are trusted code equal to the
  application: the enabled bundled defaults plus configured external packages.
- One active generation per workspace. There is no long-lived mixed-version routing and no
  in-place module reloading; a candidate is a new runtime over the current source.
- A changed SDK or image is a controlled kernel upgrade
  (`voidcode upgrade run --candidate`, see [`kernel-upgrade.md`](./kernel-upgrade.md)),
  never a hot reload: activation refuses when the installed image pin
  differs from the revision's.

## Admission loads packages; it never copies code

`kernel/activation.py:admit_live` resolves each configured plugin id to its source
directory, validates the configuration, injects the resolved providers map, and hands the
runtime `{id, root, config}` specs. Nothing is copied, hashed, or pinned: the runtime imports
the current source, and the returned `Plugin` registration is the single truth the runtime
serves as the init catalog.

The public revision payload never carries credentials or raw plugin configuration: an opaque
UUID names the recorded config/composition, and a single `private_config_sha256` digest of the
credential-bearing snapshot remains for config integrity. Rotating a credential changes the
revision identity without publishing a secret.

## One immutable snapshot per generation

`Kernel` holds exactly one `_Snapshot` pointer: revision id, the effective
`KernelConfig` (model, providers, policy, plugin entries), the admission, the plugin runtime, the tool
definitions/validators, the command catalog, and the kernel-owned tools. Every read of
`Kernel.plugin_runtime`, `Kernel.config`, `Kernel.revision_id`, `Kernel.command_catalog()` and
`Kernel.plugin_views()` comes from that one pointer, so configuration, catalog, tool schema,
context and dispatch always describe the same generation. Publication is a single assignment.

### Dispatch units

Each runtime dispatch (provider turn, tool call, command, view render, observer notification, context
contribution) runs inside `Kernel._dispatch(...)`: under a short lock it checks the fence, captures
the current snapshot and registers an active call; the call stays registered until the callback's
result is committed. No lock and no SQLite transaction is ever held across a plugin call.

### Activation fence

`Kernel.activate` refuses when another activation is already running, and refuses (reports busy with
the in-flight identities) while any foreign dispatch is active. New dispatches during a final
switch are rejected with a visible "activation is in progress" error instead of blocking a run.
Non-gated mid-run commands and confirmations keep working because the fence is only set during an
actual activation attempt.

## Safe boundary

`kernel/engine.py:_drive` consumes a staged activation only at the loop top when
`cursor.batch_sequence is None`, i.e. after the whole native batch was reported and committed.
The model's request is therefore durable while the old code finishes every member of the batch it
started, and the next provider dispatch observes the new generation. Nothing about the acting
run's own tool stack waits for or triggers the switch.

## The two control surfaces

- Reserved tool `kernel_reload` (kernel-owned, `EXECUTE` effect, so `ask` mode parks it for user
  approval): it only records `kernel.activation_requested` with the prepared target and returns. It
  never waits, never imports, and never switches anything inside the tool call.
- Reserved command `kernel.reload` (client-initiated, same activation service): refused in
  `read_only` mode and for busy sessions, and it owes no automatic continuation.

When an approval is required, the target revision is prepared *before* the user is asked
(`Kernel._prepare_target`): the approval payload carries `target_revision_id`, and after `allow`
the activation adopts that exact recorded config/composition and its private configuration from
the durable records. The approval covers config/composition only: source on disk can change
before the runtime loads it.

## State migration and deactivation are pure copies

- `Plugin.migrate_state(payload, from_version, to_version) -> Mapping`, arity 3, no services, no
  leases, no store access: run in the candidate runtime for owners whose `StateSchema.version`
  increased. The migrated copy is validated against the candidate registration's optional
  `state.schema` before the commit; a downgrade (candidate version < stored version) is refused,
  and a missing transform fails the activation visibly. A schema increase on a plain restart is
  refused the same way; the supervised `serve --session-id` path preflights and adopts the
  migration even on the same image.
- `Plugin.prepare_deactivate(payload, scope, scope_key, tip_sequence) -> Mapping`, arity 4, run in
  the *current* runtime for owners the candidate drops (disabled or removed from config). This
  is what turns "an owner was disabled" into an explicit lifecycle state instead of leaving opaque
  durable state claiming to be active. An enabled owner whose source is missing refuses instead —
  it never runs a deactivation transform. Session-scope rows receive that row's own session event
  tip; workspace-scope rows receive `None`; any other combination is refused. To remove a plugin,
  disable it via `kernel.reload` on the old running kernel first so its real transform runs.
- Every `(scope, scope_key, owner)` row is transformed at most once per activation, with its own
  optimistic-concurrency version; an owner admitted unchanged with the same schema version is only
  validated, never rewritten.
- Disabling an owner cancels its pending and claimed continuations and clears its session budgets in
  the same transaction; its durable state is kept. Re-enabling restores state and constraints but
  never resumes cancelled work, so re-arming is an explicit owner action that stages its own budget
  in the same commit. A cancelled continuation is terminal.

## The activation transaction

`SqliteStore.commit_activation` performs one `BEGIN IMMEDIATE` transaction:

1. session CAS on `(last_event_sequence, run_id, active_revision_id)`;
2. blocker re-check: no other session in `running`/`waiting`, no foreign pending confirmation, no
   foreign pending/claimed continuation, no foreign claim in the acting session;
3. the immutable revision row, the workspace pointer (`workspace_state`) and **every** session ref in
   the workspace — including the acting `running` session;
4. every migrated/deactivated state copy with its CAS version and core-owned
   `schema_version`/`revision_id` metadata;
5. cancellation and budget clearing for owners the candidate does not admit, and retargeting of the
   acting session's still-enabled queued work to the new revision (owner, source, root id and the
   shared root budget are preserved);
6. the activation events and the acting session's projection;
7. the acting session's checkpoint **with its existing kind preserved** (no checkpoint is invented
   for a session that had none), plus the durable resume intent described below.

A blocker found here raises a conflict: the refusal is recorded as `kernel.activation_blocked` (with
the blocker list) or `kernel.activation_failed`, the candidate runtime is closed, and the previous
generation stays active. Nothing is cancelled implicitly.

After the commit: pending facts of the old generation were already flushed through the old runtime;
then the snapshot pointer is swapped under a short lock and the old runtime is closed, which revokes
every handle and lease of the old generation and can never respawn.

## Durable automatic continuation

A model-requested switch records `resume_intent = <activation request id>` in the acting session's
checkpoint. The in-process run that requested the switch *is* the automatic continuation: it clears
the intent before its next dispatch, and the next commit persists that. A crash between the commit
and that dispatch leaves the intent durable, and `Kernel._drive_pending_activations` (the kernel's
startup driver, gated by `auto_drain`) claims it exactly once — one CAS commit clears the intent
before any provider call — and drives the session on the committed generation with the root action
budget the chain already owns. No client resume, prompt, or drain is involved, and a user-issued
`kernel.reload` records no intent, so an idle reload never bills a turn.

## Restart restore

`open_kernel` inspects the state read-only first. If no revision was ever adopted, the live
configuration is admitted (validated before anything is written, and only after the workspace is
verified to be free) and its private snapshot is written before the pointer. Otherwise the kernel
restores the *persisted* revision: the revision payload from the database plus the current source
at the recorded locations, with the credential-bearing snapshot from
`<state>/private/<revision_id>.json` immutable, mode 0600. Changed configuration is never admitted
implicitly — that is what an explicit reload is for — and a missing snapshot is a hard error with
recovery guidance instead of a silent fallback.

## Observer eligibility across generations

Fact-eligible owners are stamped onto the producing durable event itself, so eligibility is
immutable per event across source changes and restarts: a newly subscribing owner never receives
old facts, an unchanged owner's counters are never double-accounted, and receipts remain one per
`(event, owner)`. An eligible owner that is not loaded aborts an activation (undelivered facts must
never be papered over); during ordinary replay it surfaces visibly and stays pending.

## Client contract

- `GET /api/commands` and the session views return `revision_id` captured together with their data.
- Mutating invokes and confirmations send that `expected_revision`; a mismatch is a visible stale
  error with no side effect. An omitted expected revision is an error, never "current".
- `kernel.activation_committed` (`from_revision_id`, `to_revision_id`, `migrated_owners`,
  `deactivated_owners`, `source`) on the session stream tells clients to refetch catalog and views;
  `kernel.activation_requested`, `kernel.activation_failed` and `kernel.activation_blocked` are the
  request lifecycle around it.
- The model sees the outcome of a staged request as a bounded system note before its next dispatch
  (committed revision, or the refusal reason), so a refused switch never looks applied.

## Crash semantics

- Before the commit, including a crash inside the activation transaction itself: the switch never
  happened, so nothing is owed and no continuation is recorded. Revision, workspace pointer,
  session refs, owner state and queued work are unchanged, and the durable activation request
  stays pending. This is an ordinary run crash under the existing contract: the session requires
  an explicit resume, and that resume re-activates the prepared target, consumes the request
  exactly once and executes the new tool only after that resume. The kernel never spends provider
  tokens on a switch that did not commit, and it never auto-retries the request on cold boot.
- After the commit, before publication: the database is the single truth. A restart restores the
  committed target, performs no second migration, and the startup driver claims the durable resume
  intent once and finishes the continuation in the same session with no client resume, prompt or
  drain; the new tool executes in the fresh process.
- Rollback is only ever "activate a revision whose state schema version can read the stored rows".
  The kernel never restores an old state snapshot over newer facts.
