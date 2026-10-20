# Capability Activation (P3)

Same-session execution-code hot update for the kernel: one workspace runs exactly one active
capability generation, and a switch is a single SQLite transaction that either commits once or
leaves the previous generation untouched. This document is the authoritative contract for the
admission, activation, trust, and continuation behavior added in P3.

## Scope and limits

- Trusted Python, not an OS sandbox. An admitted plugin can still reach everything the kernel
  process can. Only code the user explicitly trusted is admitted.
- One active generation per workspace. There is no long-lived mixed-version routing and no
  in-place module reloading; a candidate is a new host process over an immutable artifact.
- A mutated SDK is a controlled kernel upgrade (`voidcode upgrade run --candidate`, see
  [`kernel-upgrade.md`](./kernel-upgrade.md)), never a hot reload: activation refuses when
  the installed SDK bytes differ from the revision's pinned `sdk_sha256`.

## Admission pins bytes; it never imports candidates

`kernel/activation.py:admit_plugin` verifies `trusted`, parses the manifest, requires the
entrypoint to be a declared implementation file, rejects undeclared Python sources, checks the
requested capabilities and the configuration schema, then copies every declared file (hash
verified) into the content-addressed immutable artifact directory `<state>/artifacts/<sha256>`.
A second admission of the same manifest re-verifies the pinned bytes instead of trusting the
source tree. The candidate's code is imported only later, inside a fresh host process.

The public revision payload never carries credentials or raw plugin configuration: it stores each
plugin's manifest and artifact identity plus a digest of the redacted configuration, and a single
`private_config_sha256` digest of the credential-bearing snapshot. Rotating a credential changes
the revision identity without publishing a secret.

## Trust is bound to canonical source paths

- Trust comes only from the explicit startup flag (`--trust-plugin <plugin-id>`) or from a path
  that a previous generation already authorized.
- At startup the flag is resolved **once** into canonical source paths
  (`kernel/bootstrap.py:_configured_source`); reloads never map an id to a live path again, so
  changing a plugin's configured `path` cannot inherit authorization.
- Configuration entries that are trusted but disabled keep their authorized path, so a later
  re-enable does not silently lose or re-acquire authorization.
- Neither the agent nor a client can grant trust through `kernel.reload`; an unauthorized candidate
  is refused with the explicit-trust guidance and the previous generation stays active.

## One immutable snapshot per generation

`Kernel` holds exactly one `_Snapshot` pointer: revision id, pinned SDK hash, the effective
`KernelConfig` (model, providers, policy, plugin entries), the admission, the host client, the tool
definitions/validators, the command catalog, and the kernel-owned tools. Every read of
`Kernel.host`, `Kernel.config`, `Kernel.revision_id`, `Kernel.command_catalog()` and
`Kernel.plugin_views()` comes from that one pointer, so configuration, catalog, tool schema,
context and dispatch always describe the same generation. Publication is a single assignment.

### Dispatch units

Each host dispatch (provider turn, tool call, command, view render, observer notification, context
contribution) runs inside `Kernel._dispatch(...)`: under a short lock it checks the fence, captures
the current snapshot and registers an active call; the call stays registered until the callback's
result is committed. No lock and no SQLite transaction is ever held across an RPC.

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
  approval): it only records `kernel.activation_requested` with the pinned target and returns. It
  never waits, never imports, and never switches anything inside the tool call.
- Reserved command `kernel.reload` (client-initiated, same activation service): refused in
  `read_only` mode and for busy sessions, and it owes no automatic continuation.

When an approval is required, the candidate is pinned *before* the user is asked
(`Kernel._prepare_target`): the approval payload carries `target_revision_id`, and after `allow`
the activation rebuilds that exact pinned artifact and private configuration, re-verifying the
pinned bytes. A later workspace edit cannot change what was approved; a mutated artifact fails
hard instead of activating something else.

## State migration and deactivation are pure copies

- `Plugin.migrate_state(payload, from_version, to_version) -> Mapping`, arity 3, no services, no
  leases, no store access: run in the candidate host for owners whose `state_schema_version`
  increased. The migrated copy is validated against the candidate manifest's optional
  `state_schema` before the commit; a downgrade (candidate version < stored version) is refused,
  and a missing transform fails the activation visibly.
- `Plugin.prepare_deactivate(payload, scope, scope_key, tip_sequence) -> Mapping`, arity 4, run in
  the *current* host for owners the candidate does not admit (disabled, removed, or missing). This
  is what turns "an owner was disabled" into an explicit lifecycle state instead of leaving opaque
  durable state claiming to be active. Session-scope rows receive that row's own session event tip;
  workspace-scope rows receive `None`; any other combination is refused.
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
the blocker list) or `kernel.activation_failed`, the candidate host is closed, and the previous
generation stays active. Nothing is cancelled implicitly.

After the commit: pending facts of the old generation were already flushed through the old host;
then the snapshot pointer is swapped under a short lock and the old host is closed, which revokes
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
restores the *persisted* revision: the revision payload from the database, the pinned artifacts
(re-hashed), and the credential-bearing snapshot from `<state>/private/<revision_id>.json`
immutable, mode 0600. Changed workspace source or configuration is never admitted implicitly —
that is what an explicit reload is for — and a missing snapshot is a hard error with recovery
guidance instead of a silent fallback.

## Observer eligibility across generations

Facts are delivered to exactly the owners the *source event's own revision* registered: live events
use that generation's registration, and older events use their revision's stored manifest
declarations. A newly subscribing owner therefore never receives old facts, an unchanged owner's
counters are never double-accounted, and receipts remain one per `(event, owner)` with no
generation column. An eligible owner that is not loaded aborts an activation (undelivered facts must
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
  an explicit resume, and that resume re-activates the pinned target (re-verifying its bytes),
  consumes the request exactly once and executes the new tool only after that resume. The kernel
  never spends provider tokens on a switch that did not commit, and it never auto-retries the
  request on cold boot.
- After the commit, before publication: the database is the single truth. A restart restores the
  committed target, performs no second migration, and the startup driver claims the durable resume
  intent once and finishes the continuation in the same session with no client resume, prompt or
  drain; the new tool executes in the fresh process.
- Rollback is only ever "activate a revision whose state schema version can read the stored rows".
  The kernel never restores an old state snapshot over newer facts.
