# Controlled Core Upgrade

One workspace runs exactly one core generation at a time, and a core upgrade replaces the
running build with a candidate installation under supervision: immutable images, a read-only
preflight before any downtime, a fenced drain at committed boundaries, one atomic adoption
transaction, and a health-checked election. This document is the authoritative contract for that
behavior. The client-visible half (CLI rows, health, reconnect rules) is also summarized in
`client-transport.md`; on any conflict this document governs the backend and `client-transport.md`
governs client rendering.

## Scope and non-goals

Supported, and proven by `tests/integration/test_core_upgrade.py` plus the unit suites named
below:

- Upgrading one installation to another installation of the same product line: generation changes
  (new image bytes, new SDK bytes, changed bundled plugins, or any combination).
- The upgrade preserves the session id, the append-only event prefix, pending and parked work that
  the policy allows, and the elected image across restarts. Completed side effects never replay.

Explicitly out of scope: upgrades from the pre-cutover runtime, imports of old configuration or
databases, downgrading facts, in-process code swapping (`importlib.reload`), a generic update
manager, plugin markets, network installers, agent-supplied executable paths, and arbitrary
historical version compatibility. The kernel→core rename is a same-line generation change:
every installation on either side of it is still the same product line, and its history
(events, state directory, image pins) is read and adopted as usual — see "Across the
rename" below.

## Runtime identity and immutable images

- An **image** is a copy of one installation's package tree plus the web bundle it serves, stored
  under `<state>/kernel-images/<digest>/root`. The digest is the shared framed tree digest
(`core/digests.py`): every file contributes its relative path, a separator, and a fixed-size
  per-file SHA-256, streamed; caches are excluded relative to the tree root. The digest always
  covers the *served* bundle bytes: a source checkout serves `frontend/dist`, so its stale packaged
  copy is ignored and the live tree hashes the same bytes as the image materialized from it.
- Materialization stages the installation's own bundle first (the sibling `frontend/dist` counts
  only in the real source layout; every other layout serves the `_web_dist` inside its own tree),
  then digests, then renames into place. Re-ingesting an existing digest re-verifies the stored
  bytes; a sibling directory next to an image is outside its attested identity and can never
  override the staged bundle.
- A supervised child launches as `[sys.executable, "-P", "-m", "voidcode", "serve", ...]`
  with `PYTHONPATH` replaced by the image root and `PYTHONHOME` dropped (the `voidcode` root
  facade resolves to the old kernel entry under an old image and to the core entry under a new
  one, so the supervisor can drive either side of the rename), and it verifies at startup that
  the tree it executes hashes to the pinned digest. A workspace or cwd `voidcode` package can
  never shadow the image. `serve --owner-pid` is accepted only from the embedded-TUI launcher,
  which passes the same pinned environment to the supervisor itself.
- The supervisor snapshots its own runtime at startup (`ensure_running_image`, no second copy
  when it already runs from an image), so recovery relaunches the exact bytes that were running.
- The revision payload pins the elected image as
  `image: {digest, core_version, launch_protocol}` — the single authoritative shape
  (`images.pin`). Every revision built afterwards (adoptions and plugin-only activations alike)
  carries the executing image's identity, taken from the running process, never from workspace
  files.

## Across the rename (one direction, stated once)

  `src/voidcode/` (`python -m voidcode`, console script
  `voidcode.cli:main`, `Core`/`CoreResult`/`open_core`/
  `create_core_app*`), the reserved names are `core.reload`/`core_reload`, health
  reports `core_version`, new revision pins record
  `image: {digest, core_version, launch_protocol}`, the supervisor and server print
  `core endpoint:`/`core bearer token:`/`core ready:`, the 503 drain message reads
  `core is draining…`, and the upgrade contract file is `core-upgrade.md`. There are
  no aliases: the old package, entry points, and names do not exist on a new build.
- What stays byte-identical so history and mixed-generation operation keep working:
  the `kernel.*` event types, `source`/`actor`/`owner` value `kernel`, the control
  routes `/api/kernel/drain|admit|adopt|promote`, the state directory `kernel-v1/`,
  the database `kernel-v1.sqlite3`, `kernel-images/`, the schema magic
  `voidcode-kernel-v1` and `kernel_identity` table, and the stored image/preflight
  `launch_protocol`. Old stored pins keep a dead `kernel_version` key that every
  reader ignores; nothing migrates it.
- Launcher compatibility: new builds launch children and one-shot reexecs through the
  root facade (`-m voidcode`), which resolves to the old `kernel` entry under an old
  image and to `core` under a new one, so the new supervisor can serve the old image
  it elects and drive the kept control routes against it. An old supervisor cannot
  launch a new image: its child argv hardcodes the old module, so a candidate preflight
  fails visibly with `preflight_failed` before anything drains, stops, or writes.
- Crossing procedure (stop, restart, upgrade): ① stop the old supervisor (SIGTERM;
  the control socket path carries no renamed strings, so the new CLI still finds it
  for `upgrade status`). ② start the new supervisor: it elects the old image by the
  recorded digest pin, launches it through the root facade, waits on the unchanged
  health fields (`generation`/`stage`/`image_digest`), and serves the old revision
  unchanged — ordinary work and reloads keep their old-image behavior under the new
  supervisor (the command is still spelled `kernel.reload` until the upgrade adopts the
  new generation, which advertises `core.reload`). ③ run `upgrade run --candidate <new> --session-id`
  from the new installation: candidate preflight, fenced drain over the kept control
  routes, the adoption transaction, and health-checked election proceed exactly as
  this contract describes.
- Plugin configuration carrying the deleted formatter hook (`plugins.formatter`) is
  refused visibly at admission (`plugin formatter requires an explicit path`), surfacing
  from preflight as `admission_failed` before downtime: edit `voidcode.json`
  (`formatter.enabled=false` or drop the entry), run `kernel.reload` on the still-running
  old image to re-admit without it, then retry the upgrade. Old
  formatter result notes in committed events keep rendering as before.

## Supported transitions

- **Schema profiles.** The adoption transaction moves the revision pointer; the schema identity
  does not change. Anything the candidate cannot read is refused in preflight before anything
  stops, with data untouched. There is no old-format importer: incompatible old state is refused
  with guidance, never silently converted.
- **Generation diff.** `adoption_required` compares the recorded generation with the candidate
  installation: the image digest, the SDK and launch protocols (equality gates; a bumped protocol
  refuses with `launch_protocol_mismatch`), and the composition of
  every enabled plugin. Adoption is required for an image change or when a state migration is
  owed (including the same-image case). A configured-but-enabled plugin that is absent from the
  candidate installation is a preflight error: to remove a plugin, disable it via `core.reload`
  on the old running core first (so its real `prepare_deactivate` runs), then upgrade.
- **The adoption transaction** (`SqliteStore.adopt_revision`) is one `BEGIN IMMEDIATE` commit:
  the acting session CAS, the blocker re-check, the new immutable revision row (the
  old row is never mutated), the workspace pointer, every session ref, every migrated or
  deactivated owner state copy, budget/overlay/projection cleanup for dropped owners, rebinding
  of pending/claimed continuations **and of every non-terminal checkpoint** in the workspace, and
  `kernel.revision_adopted` on the acting session. An adoption without an acting session is
  allowed only when the workspace has no sessions.

## Preflight: read-only checks plus the throwaway dry-run

`python -m voidcode.core preflight --workspace W --state-dir S` prints one JSON object and exits
`0` for a report or `1` with `{ok: false, reason, detail}` for a refusal. It runs from the
candidate image while the old core still owns the workspace, reads the database strictly
read-only (URI-escaped `mode=ro` plus `PRAGMA query_only`), and never creates, migrates, or writes
anything live — including no live snapshots, sessions, or continuations.

Read-only checks (each a named refusal): database identity and supported transition; adopted
revision presence and payload shape; private snapshot presence and digest; SDK/launch protocol
equality; Python/dependency environment equality; enabled-plugin presence in the candidate;
plugin state rows against their registered `StateSchema` (above the declared
version refuses as a downgrade, at the version validates, below reports `migration_owed`).
An empty plugin map is a legal generation (every plugin disabled): there is nothing to verify or
load beyond the snapshot digest. With the old core connected — the topology every supervised
preflight runs in — the database files are byte-identical before and after; on a quiet database
SQLite may finalize WAL bookkeeping bytes on a lone open, which moves already-committed frames
without changing logical content (row-level dumps are identical either way).

The dry-run then executes the candidate's plugin code in a throwaway runtime, because a generation
that cannot load or migrate must fail here and not after downtime: the target generation is
built with no store and no callbacks. The existing state-copy planner runs its real
`prepare_deactivate` transforms through it; the plan is reported
(`deactivated_owners`) and discarded. Admission, load, and transform failures
are `admission_failed`, `plugin_load_failed`, and `deactivation_failed`, each naming the owner.

## Drain: two modes, two phases, one pause rule

`POST /api/kernel/drain {timeout_seconds, require_quiescent, pause_at_batch_boundary,
pause_intent}` (supervisor control credential only):

- Fencing is immediate: fresh runs, resumes, ticks, and activations refuse; business HTTP routes
  answer `503 upgrade_pending`; only `cancel` stays allowed, because it can only reduce work.
- The drain waits for every admitted run to reach a committed boundary (session locks,
  re-snapshotted until stable, then the dispatch-call table), bounded by the timeout.
- With `pause_at_batch_boundary`, an active run commits `kernel.upgrade_paused` and a checkpoint
  carrying the pause intent at the same safe point where a staged activation would apply (the
  whole native batch reported and committed) — never mid-batch, never over an already-staged
  intent — and then returns. A run that never reaches a boundary inside the timeout fails the
  drain with blockers instead of being force-cancelled.
- Drain modes: a same-generation restart (`require_quiescent=false`) treats parked approvals and
  stored confirmations as durable boundaries that restore afterwards; a revision-changing upgrade
  (`require_quiescent=true`) refuses them before downtime (`approval_wait`,
  `pending_confirmation`), along with running sessions, claimed continuations, and unanswered
  activation requests, so an approval is never rebound to different tool code. Pending (unclaimed)
  continuations are retargeted by the adoption, not refused.
- Two phases: the `server_restart` frame goes out only after a drain SUCCEEDS, when the old child
  is about to stop — and the successful drain response waits (bounded) for every stream open at
  that moment to end with its frame, so no stop can race frame delivery. A drain that ends 409 — blockers, timeout, or work that parked after the
  early scan — leaves every stream connected with no frame, and the supervisor calls
  `POST /api/kernel/admit` promptly: admit clears both fences and resumes durable intents exactly
  once on the old image, returning immediately while resumption runs on the existing driver.

## Supervision, election, and recovery

- The supervisor binds the loopback listener once (`--port 0` picks an ephemeral one), generates
  the API bearer once, and passes the listening socket to each child (`--listen-fd`). Endpoint
  and token never move across a replacement. While no child accepts, connects queue in the listen
  backlog and are answered by the elected child.
- Children start fenced and stay fenced until promoted. A child that owes an adoption serves
  health plus the control surface with no core (`validating` → `validated`); `POST
  /api/kernel/adopt {"session_id": <id|null>}` builds the target generation, runs the real runtime
  load, commits the adoption, and only then constructs the core (`adopted`);
  `POST /api/kernel/promote` starts background work and clears the fence (`serving`). A child
  that owes no adoption constructs its core at startup and waits fenced for promotion.
- Control routes (`/api/kernel/drain|admit|adopt|promote`) require a separate unguessable control
  token generated per supervisor lifetime, passed only to the child, never printed or persisted;
  the API bearer gets `401 missing_control_credential` there. The supervisor's own CLI socket
  (`0600`) lives in a private per-user runtime directory (`0700`, `/tmp/voidcode-<uid>`), named by
  a digest of the canonical state directory because the durable state path plus its workspace
  identity is longer than the fixed AF_UNIX address limit; it is OS-user authority — the SDK/API
  client control surface does not expose image election — not a plugin sandbox: trusted Python
  running as the same OS user can connect it. Platforms without POSIX ownership/mode semantics
  refuse to serve the supervisor instead of weakening this boundary. No agent-facing tool accepts
  an executable identity.
- The durable decision is SQLite, never a journal file: the revision pointer plus the revision's
  image pin, resolved to `<state>/kernel-images/<digest>/root`. Anything before the adoption
  commit restarts the old image; anything after restarts the candidate image; a missing elected
  image refuses by name instead of starting another image against state it cannot read. The old
  image refuses post-adoption state by its own pin guard. A SIGKILL between the database commit
  and any journal write still recovers the candidate, because the election committed atomically
  with the revision.
- A child watches its supervisor by comparing `os.getppid()` against the passed
  `--supervisor-pid` (no signals, so PID reuse is harmless) and shuts down gracefully when it
  differs; the embedded supervisor watches the TUI pid the same way when `--owner-pid` is
  passed. Standalone `serve` never exits on shell-parent death.

## Continuations: exactly once, never replayed, never rebound

- A staged continuation (`resume_intent`, an activation request id or an upgrade pause id) is
  consumed by one CAS commit (`kernel.continuation_consumed` with the real `cause`) before it is
  driven, by whichever image legitimately owns the pointer: the old image after an abort, the
  candidate after election. A crash before the commit leaves the intent durable; a crash after it
  resumes the same run cursor.
- Completed tool calls are facts (`tool_intent`/`tool_completed` pairs in the provider window),
  not queue entries: nothing re-executes across the handoff, and pending continuations are
  rebound to the new revision in the adoption transaction rather than cancelled or duplicated.
- A serving child that dies is never silently replaced: the supervisor reports its exit code on
  stderr and exits nonzero, and the next launch cold-starts the elected image over unchanged
  state. Work the dead process had admitted but never committed a boundary for resumes only
  through an explicit client `resume`, which first appends `kernel.tool_interrupted` for each
  parked batch member and then continues from the durable cursor: an interrupted member is
  never replayed and never reported completed.
- A dead core's orphan `running` session is interrupted only inside a controlled adoption
  holding the exclusive workspace lease (a live core makes adoption refuse, so the row
  can have no live owner): the adoption appends `kernel.run_interrupted` at the last
  committed boundary, keeps the cursor untouched, and the session stays explicitly
  resumable. A plain start against a schema-changed source refuses instead of
  reinterpreting stored state; only the adoption transaction migrates it.
- The event prefix is immutable; the upgrade only appends (`kernel.revision_adopted`,
  `kernel.upgrade_paused`, `kernel.continuation_consumed`).

## Client surface

- Health: `GET /api/health` (in the OpenAPI schema) reports the stable identity clients and the
  supervisor poll — status, fence, stage, pid, workspace, revision, SDK/launch protocols, core
  version, image identity, supervisor pid, and the active upgrade. It answers even with no core
  constructed.
- Restart: a followed stream whose drain succeeded ends with
  `{"kind":"server_restart","cursor":<last sequence this stream delivered>}` after flushing its
  final backlog; the cursor never names an undelivered event, so reconnecting with
  `after_sequence=<cursor>` replays exactly the remainder. Clients keep their own applied cursor
  and resume from it; the frame cursor is corroboration. Lifespan shutdown closes the core under
  in-flight streams, so a follow loop that can no longer read still ends with the restart frame
  (never an error frame) when a drain already succeeded; anything committed after the last fetch
  has a higher sequence and replays on reconnect. No prompt is ever resent.
- CLI: `serve` runs the supervisor; `upgrade --candidate PATH [--session-id ID] [--timeout S]`
  and `upgrade status` print one JSON object each; `preflight` prints the preflight report; the
  bare command without `--server` spawns a supervisor child and behaves exactly like remote mode.
  Ordinary one-shot commands execute inside the elected image (re-executing into it when the
  invoking installation is not it, snapshotting first on a fresh state), so an old install never
  runs stale bytes and never needs a refusal the user must decode. `show` stays a config-free,
  schema-tolerant rescue read.
