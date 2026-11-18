# SDK data contracts: resource discovery/enumeration/search + streaming/raw process capture

Status: ACCEPTED DESIGN, UNIMPLEMENTED (recorded 2026-11-18). Source: sdk-data turn-1 design + turn-2 review corrections (both parent-accepted). Review corrections integrated in A§1 (grep truncation; detail rule). Risks + open questions below are verbatim from the design and still open.

## Shared machinery (defined once; referenced by A§1 and B§1)

Both contracts reuse one paging/identity vocabulary; defined once here, referenced by (A)§1 and (B)§1.

**Paging/identity vocabulary.** offset:int 1-based line/row cursor + byte_offset:int|None 0-based byte cursor, with next_offset/next_byte_offset continuations; None continuation = end. Source: ResourceReadRequest src/voidcode/sdk/resources.py:67-99; render_stored_artifact_page src/voidcode/capabilities/resource_pages.py:117-155 (next_byte_offset :128) and :157-240 (next_offset :207); proc byte cursors src/voidcode/capabilities/exec/process_reader.py:182-183,227-228. Rule: continuations are only ever the returned cursor on the same URI; cursors must advance monotonically (stuck-link precedent process_reader.py:159-162).

**Identity.** ArtifactReference{sha256,bytes,media_type} + derived uri artifact://<sha256> src/voidcode/sdk/services.py:480-494. Mutable roots (agent://, proc://) hand out immutable digest URIs for continuation (agent_reader.py:73,101); (B) raw artifacts are consumed through the existing artifact pager, no new pager.

**Honesty conventions.** Three-way split reused everywhere: unknown-scheme raises ResourceUnavailable (resources.py:63-64, plugins.py:1035-1036), unknown-resource returns None, malformed-provider-data raises ResourceContractError (resources.py:53-60, plugins.py:1062-1063). Verbatim cause strings, never inference (ChildOutcome.output_error/history_error precedent, sdk/TODO.md P1). Counts claimed only when known (proc total_count :87 vs artifact line pager claiming none).

**Bounds reuse.** No new MAX: in-process plugin-answer wire check reuses MAX_QUERY_RESULT_BYTES 256KiB (core/plugins.py:120,1015); model-projection bound stays TOOL_OUTPUT_BYTES 50KiB (sdk/limits.py:11) via MAX_RESOURCE_PAGE_BYTES (resource_pages.py:22); capture/store bounds stay MAX_CAPTURE_BYTES / MAX_ARTIFACT_BYTES 256MiB (core/processes.py:35, core/artifacts.py:26).

# Contract A — Resource capability discovery / enumeration (+ search shape)

## A(1) Exact proposed surface

**Module.** src/voidcode/sdk/resources.py (extend; stdlib-only like today: re/dataclasses/pathlib/typing + .json_values/.tools), plus 4 methods on PluginServices protocol in src/voidcode/sdk/services.py.

```python
@dataclass(frozen=True, slots=True)
class ResourceSchemeCaps:
    scheme: str                      # lowercase, _READER_SCHEME_PATTERN
    enumerable: bool
    searchable: bool
    line_paging: bool                # honors offset/limit line cursors
    byte_paging: bool                # honors byte_offset/byte_limit
    inventory_uri: str | None = None # e.g. 'proc://' when enumerable
    detail: str = ''  # REQUIRED non-empty, unconditionally (accepted review correction below)

@dataclass(frozen=True, slots=True)
class ResourceEnumRequest:
    uri: str; offset: int = 1; limit: int | None = None
    @property
    def scheme(self) -> str: ...     # same lowercasing as ResourceReadRequest.scheme

@dataclass(frozen=True, slots=True)
class ResourceEnumEntry:
    uri: str; label: str | None = None

@dataclass(frozen=True, slots=True)
class ResourceEnumPage:
    entries: tuple[ResourceEnumEntry, ...]
    next_offset: int | None          # None = end; else must exceed request offset
    total_count: int | None = None   # None = unknown, never guessed

@dataclass(frozen=True, slots=True)
class ResourceSearchRequest:
    uri: str                         # scope root, e.g. 'proc://'
    pattern: str                     # non-empty
    regex: bool = False
    ignore_case: bool = False
    offset: int = 1                  # match cursor
    limit: int | None = None
    @property
    def scheme(self) -> str: ...

@dataclass(frozen=True, slots=True)
class ResourceSearchMatch:
    uri: str; text: str              # one bounded match line
    line: int | None = None          # set iff scope is line-addressed
    byte_offset: int | None = None   # set iff scope is byte-addressed

@dataclass(frozen=True, slots=True)
class ResourceSearchPage:
    matches: tuple[ResourceSearchMatch, ...]
    next_offset: int | None
    truncated: bool = False

class ResourceUnsupported(ValueError):
    '''A known scheme does not offer the requested capability.'''
```

**Accepted review correction — `detail` enforcement (integrated).** "Mixed" is dropped from the enforced rule; `detail` is required unconditionally. No boolean combination detects intra-scheme splits: `proc://` is line `True` + byte `True`, yet root pages by line and leaves by byte (`process_reader.py:57-58` vs `:248-249`), so any `line_paging != byte_paging`-style condition has a hole. The enforceable rule, verbatim:

```python
def __post_init__(self) -> None:
    if not isinstance(self.scheme, str) or _READER_SCHEME_PATTERN.fullmatch(self.scheme) is None:
        raise ValueError(f"invalid resource scheme: {self.scheme!r}")
    for name in ("enumerable", "searchable", "line_paging", "byte_paging"):
        if type(getattr(self, name)) is not bool:
            raise ValueError(f"resource scheme capabilities {name} must be a boolean")
    if self.inventory_uri is not None and (
        not isinstance(self.inventory_uri, str) or not self.inventory_uri.startswith(f"{self.scheme}://")
    ):
        raise ValueError("resource capabilities inventory_uri must be None or a URI in this scheme")
    if not isinstance(self.detail, str) or not self.detail.strip():
        raise ValueError("resource scheme capabilities require a non-empty detail note")
```

Precedents: scheme pattern `resources.py:49` + reader check `:211`; `type(...) is not bool` check `:41`; non-empty-message rules (e.g. `services.py:45-46`). Unconditional `detail` additionally guarantees `ResourceUnsupported` messages always carry content. `inventory_uri` stays optional even when `enumerable` is `True` (a multi-root scheme names its roots in `detail`); the proc root-vs-leaf split becomes a content requirement on proc's `detail` string, covered by review/tests rather than boolean logic.

ResourceReader gains three defaulted fields (existing ResourceReader(scheme, handler) calls unchanged):
```python
    capabilities: ResourceSchemeCaps | None = None   # None = undeclared (cf. ToolDefinition.output_schema None=undeclared)
    enumerate: Callable[[ResourceEnumRequest, ResourceReadCaller, ResourceReaderServices], ResourceEnumPage | None] | None = None
    search: Callable[[ResourceSearchRequest, ResourceReadCaller, ResourceReaderServices], ResourceSearchPage | None] | None = None
```
PluginServices gains:
```python
    def resource_schemes(self) -> tuple[str, ...]: ...                              # admitted schemes, admission order
    def resource_capabilities(self, scheme: str) -> ResourceSchemeCaps | None: ...  # None = registered-but-undeclared
    def enumerate_resources(self, request: ResourceEnumRequest) -> ResourceEnumPage | None: ...  # None = no such scope
    def search_resources(self, request: ResourceSearchRequest) -> ResourceSearchPage | None: ...
```

**Declared caps per scheme.** tool:// enum F ('catalog listing not exposed to readers; read tool://<name>') search F, line T, byte F (rejects byte args resource_readers.py:39); artifact:// enum F ('content-addressed, no inventory') search F ('no store scan') line T byte T; transcript:// enum F ('no session roster') search F, line F + byte F with detail 'page with ?after_sequence=&offset= cursors; limit honored ≤200' (reader ignores request.offset, honors limit resource_readers.py:93-156); agent:// enum F ('root requires an id; no roster route' — subagents/README.md:19) search F, line T byte T (shared pager agent_reader.py:103-112); proc:// enum T inventory_uri='proc://' search F ('live log scan not offered') line T byte T detail 'root pages by line, proc://<id>[/stderr] pages by byte' (process_reader.py:57-58,248-249); web:// enum F search F line T byte T; skill:// enum F ('use skills command for roster') search F line T byte F (skills_plugin.py:440-441). All searchable=False initially — search shape specified, zero providers.

**Consumers.** read: dispatch unchanged (read_tool.py:341-360); only addition is pre-dispatch refusal when caps DECLARED and byte_offset given but byte_paging False (else handler keeps exact errors). grep: '://' in path (same discriminator as read_tool.py:341) -> capabilities lookup -> searchable? search_resources(pattern,regex,ignore_case,limit=args.limit,offset=1) single page, map to GrepResultMatch(file=match.uri,...) inside existing GrepResultBody (grep_tool.py:91-113); line=None (byte-only scope) -> honest ToolFailure, never line 0. glob: '://' in path -> enumerable? enumerate_resources single page (limit=args.limit) -> fnmatch full-URI filter -> existing GlobResultBody.matches URIs verbatim (glob_tool.py:65-84; path=scope URI preserved). Unknown scheme -> ValueError('unsupported resource URI') mirroring read_tool.py:354-355; known-but-unsupported/undeclared -> model-visible ToolFailure naming scheme+reason mirroring _uri_failure (read_tool.py:136-142,359). Script helpers: no new helper — eval/tool.* call the same tools (eval is future per plugins/TODO.md:30-34). Clients: NO SessionClient method (client RPC has only read_artifact client.py:586; resource reads stay lease-scoped tool invocations, direct RPC would bypass visibility).

**Accepted review correction — grep truncation surfacing (integrated).** No field added; the slot already exists. `GrepResultBody.truncated: bool` is at `src/voidcode/capabilities/files/grep_tool.py:99`, serialized as both `"truncated"` and `"partial"` (`:110-111`), and file-mode already surfaces it via `OutputBounds(truncated=truncated, partial=truncated)` (`:366`) with `match_count` (`:360-361`), setting it when the match walk is cut (`:309-320`, `:351`). Exact URI-branch rule, mirroring `:360-366`:

```python
truncated = page.truncated or page.next_offset is not None
body = GrepResultBody(..., match_count=len(page.matches), truncated=truncated, ...)
output = TextOutput(summary, bounds=OutputBounds(truncated=truncated, partial=truncated))
```

The `or` is required because grep takes exactly one page (`GrepArgs` has no cursor, `:41-52`), so either "more exists" signal means this result is incomplete; on inconsistent providers the `or` errs toward claiming partial, never complete. It cannot be silent because `:110-111` + `:366` are the existing model-visible completeness channel — leaving them `False` on a cut page would present a partial match set as the full result, the same silent-partial dishonesty the contract forbids. (Glob parallel, unchanged shape: `truncated = page.next_offset is not None`, mirroring `glob_tool.py:229,244`.)

## A(2) Semantics

**Lifecycle.** Discovery (schemes/caps) reads the live generation map under lease_lock per call — no snapshots, no staleness; provider disabled / generation retired (plugins.py:1746+) immediately yields ResourceUnavailable. enumerate/search dispatch is a read_resource sibling (plugins.py:1019-1064): provider-owner sub-lease with caller session/run/revision, readonly=True, internal_work=False (:1038-1047), ResourceReaderServicesView only (services.py:1493-1535), revoked before return (:1058-1059, :744-758 incl. discard_owner).

**Ownership.** Core owns the map entry and dispatch; plugin owns caps truth + handler behavior; caller owns cursors.

**Error taxonomy.** ResourceUnavailable (exists): unknown scheme on all four entry points. ResourceUnsupported (new, plain ValueError w/ 'scheme X does not support <cap>: <detail>' incl. undeclared case): known scheme lacking capability — never silent empty. ResourceContractError (exists, extended): malformed enum/search page (wrong type, non-tuple entries, next_offset not advancing, negative counts). None: unknown scope/resource in a known scheme (mirrors read None). ValueError: malformed URI/cursor from handler (agent_reader.py:44-46, process_reader.py:33-49 precedents). CancelledError: abort via caller.abort_signal (process_reader.py:26-28, resource_pages.py:57-59). PermissionError: revoked lease (services.py:66-68). FileNotFoundError vs ValueError/OSError: storage errors propagate, never None (read_artifact contract services.py:1190-1195; resource_pages.py:76-77).

**Cancellation/restart.** Handlers check caller.abort_signal; Core sets cancel via cancel_event_for (plugins.py:782-789). No persistence, no restart semantics: enum/search are pure reads of live state; cursors are not durable (re-read after reload may yield different rows for mutable roots — same as proc inventory today); immutable continuations use artifact:// digests.

## A(3) Layer placement

**SDK.** All shapes + ResourceUnsupported + ResourceReader slots + 4 Protocol methods. No core imports (tools.py:1-4 self-containment rule; resources.py today imports only stdlib + .json_values/.tools).

**Core.** Admission: widen _resource_readers entry (plugins.py:356,484-489) to one record (handler, owner, caps, enumerate, search) — SAME dict, no second registry; duplicate-scheme refusal unchanged; add caps.scheme==scheme check + enumerate/search arity-3 checks (mirror :488). Dispatch: enumerate_resources/search_resources siblings of read_resource with identical sub-lease discipline; enum/search page wire-size check against MAX_QUERY_RESULT_BYTES exactly like execute_query (plugins.py:120,1015). Discovery: no sub-lease (registration metadata only), active-lease check only. services.py: add 'resource_schemes','resource_capabilities' to _HOOK_METHODS (:78-94, alongside tool_definition :92 — catalog metadata is hook-observable); enumerate/search call _check_hook like read_resource (:1281) so hooks are refused (they would run handler code).

**Plugin/capability.** Caps values, enumerate/search handlers, read/grep/glob consumption. Core implements NO scheme (no product builtins).

## A(4) Non-goals

- No writable resources / VFS (plugins/TODO.md:39); mutation stays in tools.

- No cross-scheme federated search, no SQL/query language, no OMP table copying (plugins/TODO.md:38).

- No SessionClient RPC for resources (lease/visibility bypass).

- No transcript:// cursor restructuring (caps describe actual ?after_sequence behavior).

- No agent:// roster (needs parent/descendant permission design, plugins/TODO.md:45).

- No caps caching; no codec/wire changes (discovery + pages are in-process only; grep/glob results cross the wire in existing bodies).

- No enum pattern/prefix pushdown (glob pages one page + full-URI fnmatch; server-side filtering deferred).

## A(5) Compatibility

ResourceReader defaulted fields: all existing constructions (PROC_READER process_reader.py:276, AGENT_READER agent_reader.py:144, create_resource_readers resource_readers.py:159-165, memo test) unchanged and behavior-identical. Codec untouched (no wire types added). read file/URI paths byte-identical when caps undeclared (no pre-validation without declared caps). grep/glob file behavior identical (new branch only when '://' present; grep walk grep_tool.py:179, glob _find_files glob_tool.py:120-130 untouched).

## A(6) Acceptance sketch

- tests/unit/sdk/test_resource_caps.py (new): caps validation (detail required unconditionally per accepted correction; inventory_uri scheme match; bad scheme refused); ResourceReader defaults None; request cursor validation; scheme lowercasing.

- tests/unit/core/test_resource_discovery.py (new, real-runtime harness mirroring test_resource_readers.py:61-90): schemes list admission-ordered; caps round-trip; undeclared memo caps -> None; unknown scheme -> ResourceUnavailable on all four entries; files-only runtime -> agent/proc schemes unavailable (disabled-provider removal); hook lease allows discovery, refuses enumerate/search.

- tests/unit/plugins/test_proc_enumerate.py (new): enumerate proc:// equals existing inventory-root rows (process_reader.py:52-91); cursor monotonic incl. end-None; total_count present; unknown process scope -> None; agent enumerate -> ResourceUnsupported naming reason.

- tests/unit/plugins/test_files.py or grep/glob suite (extend): grep/glob URI scope on non-searchable/non-enumerable -> ToolFailure text names scheme+reason, zero matches (never silent empty list); glob proc://* returns proc:// URIs with path=scope preserved.

- tests/unit/core/test_resource_readers.py (extend): malformed enum page (non-advancing next_offset) -> ResourceContractError; oversized page refused via reused bound.

## A(7) Risks + open questions (verbatim, still open)

- RISK: proc mixed paging (root=line, leaves=byte) flattened into per-scheme booleans + detail string — discovery consumers cannot branch per-URI; accepted: exact validation stays in handler (process_reader.py:57-58,248-249), caps are advisory for routing. Per-URI caps deferred.

- RISK: resource_schemes discloses admitted plugin set; judged low (tool names already catalog-visible via tool_definition) but needs reviewer confirmation for hidden-plugin postures.

- RISK: glob over a large enumerable inventory fetches one page then filters — recall bounded by glob MAX_RESULT_LIMIT 5000 (glob_tool.py:29,43; consumer's own bound, not fiat) with truncated flag; server-side pattern pushdown open.

- OPEN: transcript:// ignoring request.offset while caps say line F — should read normalize or transcript adopt offset? Caps describe actual; behavior change explicitly out of scope.

- OPEN: future artifact:// search needs a store-scan budget (disk/CPU risk owner unclear); no provider designed — grep refusal text must invite re-check after caps change (no versioning on caps).

- OPEN: sibling interaction-contracts agent may define overlapping cursor/paging vocabulary — reconcile naming (next_offset/next_byte_offset) at review.

# Contract B — Streaming / raw process-output capture

## B(1) Exact proposed surface

**Module.** src/voidcode/sdk/services.py (shape + params only); Core internals in src/voidcode/core/processes.py + thin RuntimeServices plumbing in src/voidcode/core/services.py; shell opt-in in src/voidcode/capabilities/exec/shell_exec.py.

```python
@dataclass(frozen=True, slots=True)
class RawStreamCapture:
    stream: Literal['stdout', 'stderr']
    bytes_captured: int              # raw bytes durably stored in artifact
    bytes_dropped: int               # raw bytes past the capture bound or after store failure; never stored
    artifact: ArtifactReference | None
    capture_error: str | None = None # verbatim store/append/finish cause; exactly-one-of with artifact
    def __post_init__(self) -> None: # non-neg int counts; (artifact is None) == (capture_error is None) -> ValueError
    @property
    def truncated(self) -> bool: ... # bytes_dropped > 0
    def as_payload(self) -> dict[str, object]: ...  # {stream,bytes_captured,bytes_dropped,truncated,artifact|None,capture_error}
```
ProcessResult (services.py:557-576) gains ONE defaulted field: `raw: tuple[RawStreamCapture, ...] = ()` — empty = text mode (today); exactly two entries (stdout, stderr) when raw requested. `run_process`/`run_query_process` gain keyword-only `raw_capture: bool = False, raw_media_type: str = 'application/octet-stream'` (defaults preserve byte-identical behavior). ShellExecArgs gains `raw_capture: bool = False`; ShellExecResultBody gains `streams: tuple[RawStreamCapture, ...] = ()` serialized only when non-empty (schema rule shell_exec.py:91-93); SHELL_EXEC_RESULT_SCHEMA gains optional 'streams' array, required keys unchanged (:95).

**Request path.** Consumer requests raw via run_process(..., raw_capture=True) / run_query_process(..., raw_capture=True) / shell_exec(raw_capture=true). 'Streaming' = Core appends raw chunks to per-stream artifact spills incrementally during the run (flat ~8KiB memory, no 128MiB buffer/join/decode/re-encode chain from processes.py:49-72 + shell_exec.py:128-130); observational live progress stays the existing on_output hook (processes.py:1024-1029, shell_exec.py:133-155). No new live handle: long-lived byte streaming is already spawn_process + read_process_output (raw bytes pages services.py:539-554).

**Per-stream capture/truncation state.** captured = artifact bytes (byte-identical raw, CRLF preserved — text() folding at processes.py:72 bypassed); dropped = over-bound + post-store-failure bytes, run continues (truncation-consistent with append-drop processes.py:58-69); replaced = N/A by construction — raw path never decodes, so no replacement count is carried (the only decoder, _StreamCapture.text(), is bypassed); any text shown from a raw artifact is decoded explicitly at presentation time (strict-or-hex precedent process_reader.py:150-163). Cancel/timeout: partial captured bytes ARE published (parity with text drain processes.py:384-386 + shell spill :185-186); stop cause rides existing cancelled/timed_out flags + exit_code None (processes.py:398) — flags mean 'artifact is a prefix'. Reader/pipe errors: discard spills, raise ValueError as today (:394).

**Storage failures.** Append/finish failure (ArtifactError/OSError/disk-full): record verbatim cause in capture_error, artifact None, run CONTINUES dropping+counting bytes (same as bound truncation), exit code preserved. Follows ChildOutcome.output_error verbatim-field precedent, not raise. exactly-one-of invariant makes silent partials unrepresentable.

## B(2) Semantics

**Lifecycle.** run() validates raw_media_type via normalize_media_type (artifacts.py:48-53) BEFORE spawn (fail fast, no process on bad type); opens per-stream spills (owner=lease.token, cf. services.py:445) eagerly so empty streams publish honestly-empty artifacts (shell precedent shell_exec.py:128-129, dedup-free artifacts.py:121-123); reader threads append-or-count per chunk under the SAME limit (min(max_output_bytes, MAX_CAPTURE_BYTES) processes.py:289); at exit/stop: join readers in drain budgets (:384-386), finish() spills, build entries. Lease revoke after return cannot touch published artifacts (store-owned); in-flight spill discarded on raise paths (mirror services.py:459-461).

**Ownership.** Core owns process/group/bytes/bounds (unchanged); lease owns spill until publish; caller owns refs after return. No writable backing path leaves Core — consumers page via read_artifact/artifact:// only.

**Error taxonomy.** ValueError (all existing, unchanged): bad argv/timeout/max_output_bytes (:285-288), spawn OSError (:320), reader errors (:394), bad media type via ArtifactError⊂ValueError (artifacts.py:36). PermissionError (unchanged): run_process refuses readonly+hook (services.py:168-169); run_query_process refuses hook only (:200) — raw under readonly lease is consistent because readonly may spill (services.py:1169). Per-stream capture_error field (new, NOT an exception): verbatim store failure; artifact None; never confused with truncation (truncated=dropped>0 regardless of cause). No new exception types. In raw mode ProcessResult.stdout/stderr are '' and VOID — documented rule: when raw non-empty, text fields carry no meaning (disambiguated by raw presence, never misread as empty output).

**Cancellation/restart.** Unchanged governance: cancel_event wiring (services.py:180 <- plugins.py:782-789), 20Hz watcher (plugins.py:814-826), _CANCEL_RESPONSE_SECONDS/_READER_GRACE_SECONDS drains (:44-46), group kill + exit None when stopped (:398), flags. One-shot runs have no restart (as today); raw artifacts persist content-addressed (never GC'd artifacts.py:6-7) so refs survive reload. No events emitted (append-only untouched).

## B(3) Layer placement

**SDK.** RawStreamCapture + ProcessResult.raw + run_* params. Pure data (stdlib typing/dataclasses + ArtifactReference sibling); no core imports.

**Core.** ProcessRegistry: new _RawStreamCapture sink (limit, spill, captured/dropped/error; append()/finish()) sharing _read_stream's append protocol (processes.py:1010-1029; on_output stays observational, sink failures still swallowed :1025-1029); run() gains Core-INTERNAL kwargs (store, spill_owner, raw, raw_media_type) — not SDK surface; stop/drain/publish ordering inside existing flow (:351-405). RuntimeServices: validate media type, pass require_artifacts() (plugins.py:688-691) + lease.token; stance gates unchanged.

**Plugin/capability.** shell_exec branches BEFORE spill: raw mode takes refs from result.raw (never spills void ''), body.streams set, presentation names byte counts + raw URIs ('Captured raw stdout (N bytes): ...'); text mode untouched. Schema gains optional keys only (consumer-side validation precedent result_schema.py). No product builtins in Core (no shell knowledge).

## B(4) Non-goals

- No live-stream handle for one-shot runs (spawn+read_process_output already byte-honest).

- No text-mode replacement counting (honest U+FFFD-origin accounting needs a decode-comparison design; text path stays legacy-labeled: 'decoded UTF-8 (invalid bytes replaced)' services.py:561).

- No default-capture changes (same _StreamCapture path, same bytes).

- No new store/capture bounds (reuse MAX_CAPTURE_BYTES + MAX_ARTIFACT_BYTES).

- No on_output backpressure (observational by design :1028).

- No client/RPC streaming; no codec changes (ProcessResult never crosses wire — codec.py covers call/definition/message/provider/result only); shell body crosses wire via additive optional schema keys.

- No per-stream media types (single raw_media_type; deferred).

## B(5) Compatibility

Default calls (raw_capture=False) never open the store: identical _StreamCapture path, identical text()/flags, raw=() — byte-identical incl. CRLF fold. Signatures gain keyword-only defaulted params. Shell text mode byte-identical (same spill of same text, same schema required keys). Shell raw mode INTENTIONALLY changes artifact bytes to byte-identical raw (even CRLF outputs differ from text mode — documented, tested). SHELL_EXEC_RESULT_SCHEMA additive-optional only; old payloads validate; legacy results without 'streams' decode to text-mode.

## B(6) Acceptance sketch

- tests/unit/core/test_process_raw_capture.py (new, real registry): binary output (invalid UTF-8 + NUL + CRLF) round-trips byte-identical via read_artifact; over-bound run drops+counts, process continues to natural exit, bytes_captured+dropped == produced; store-failure injection (full/discard) -> capture_error verbatim + artifact None + exit preserved + other stream intact; cancel/timeout mid-run -> flags set + published artifact is exact prefix (artifact bytes == first bytes_captured raw bytes) + counts consistent; raw_media_type invalid -> ArtifactError before spawn (no process); text+raw dual-run equality on pure-UTF8-LF output except raw preserves CRLF.

- tests/unit/plugins/test_exec_shell.py (extend): shell raw_capture=true on printf binary -> octet-stream artifacts byte-identical; presentation names counts; text default unchanged incl. CRLF fold + text/plain mime.

- tests/unit/plugins/test_shell_exec_schema.py (extend): raw branches validate (streams array present, base keys intact); tampered streams rejected; failure bodies never validate (existing 6-way pattern).

- tests/unit/sdk/test_process_capture_shapes.py (new): RawStreamCapture exactly-one-of invariant; counts validation; ProcessResult.raw len/order validation; as_payload round-trip.

## B(7) Risks + open questions (verbatim, still open)

- RISK: ArtifactStore._spills has no lock (artifacts.py:81-130); concurrent raw runs append to disjoint spills/files (uuid keys) — likely safe but needs reviewer sign-off for threaded spawns.

- RISK: default max_output_bytes 262144 (services.py:1129) truncates raw artifacts surprisingly for direct callers; shell passes 128MiB (:182). Mitigation: document loudly; no silent raise of the default (bound has a memory-risk owner).

- RISK: cancel-vs-finish race — finish() must complete before run returns; revoke-after-return is safe (store-owned) but mid-drain revoke paths need a test.

- OPEN: text-mode replacement accounting (captured vs replaced for legacy path) still unowned — needs honest original-vs-substitute U+FFFD distinction, not a second decode.

- OPEN: single media type for both streams; per-stream typing (e.g. stdout image bytes) deferred to a real consumer.

- OPEN: incremental spill writes add syscalls per 8KiB chunk during run — perf acceptable (matches _READ_CHUNK_BYTES :39) but unmeasured; no batching added without evidence.
