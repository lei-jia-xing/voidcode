# Artifact references

The core stores immutable, content-addressed bytes in the workspace's state
directory. Plugins use SDK services to store/read them and pass references in
tool results. A producer does not call another plugin to publish an artifact.

```python
artifact = services.spill_artifact_stream(chunks, media_type="text/plain; charset=utf-8")
result = ToolSuccess(
    "my_tool",
    EmptyOutput("Output saved", bounds=OutputBounds(reference=artifact)),
)
```

`ArtifactReference` contains exactly `sha256`, `bytes` and `media_type`.
Its `.uri` property derives `artifact://<sha256>`; URI is not an additional stored
field. `as_payload()` exports the same three fields when a plugin needs a JSON
body. Business context belongs in the tool's own body or `OutputBounds.source`,
not in artifact metadata.

`OutputBounds.reference` accepts an `ArtifactReference`, an ordinary resource URI
string such as `transcript://<session>`, or `None`. The codec restores artifact
objects from their JSON representation; generic URI references remain strings.
There is no separate output-reference wrapper or URI registration step.

`TextOutput.text` is the producer-selected model page. A recovery reference on
that output never tells the host to restart at byte zero. A machine `ToolBody`
accompanying `TextOutput` is available through the host-owned canonical
`result_reference`, not duplicated beside the selected text. Even a small body
must have that complete URI before the host omits it. `EmptyOutput` with a
structured body keeps the body as the model data; `EmptyOutput` with a typed
textual artifact reference opts into bounded content paging and retains its
`presentation` as a separate summary. Core treats `text/*`, JSON/XML,
`application/javascript`, `application/x-javascript`, `application/*+json`,
and `application/*+xml` artifact media types as textual. Omitting machine
metadata is not evidence that the selected read page was clipped.

Readers call the byte service with a digest:

```python
page = services.read_artifact(artifact.sha256, offset=0, limit=4096)
```

`ArtifactPage` carries that same reference as `.artifact`, plus `offset`, `data`
and `eof`. Metadata is not restated using a second set of field names.

Pages use byte offsets and contain at most 256 KiB (default 64 KiB).
Offsets/limits must be exact integers; offsets are nonnegative and limits are
positive. A valid offset at or beyond EOF returns empty bytes with `eof=True`.
Digests are lowercase 64-character SHA-256 hex. Missing objects raise
`FileNotFoundError`; malformed requests and storage failures are distinct.

The same read operation is public on `Core` and `SessionClient`; remote clients
use authenticated `artifact.read` scoped to the workspace store. Its wire DTO
contains `artifact`, `offset`, base64-encoded `data`, and `eof`. Clients decode
the page back to bytes; arbitrary binary data never uses UTF-8 replacement.
This byte API does not imply a writable backing path or generic virtual
filesystem. Viewer rendering and image-terminal support are separate consumers.

`artifact://<sha256>` input is served by the files plugin's registered resource
reader: `read` routes the URI by scheme and the reader renders bounded
text/image output through the shared stored-artifact pager. Other namespaces
(e.g. `web://<sha256>` snapshots) may render the same stored bytes under their
own scheme. Storage and byte paging remain available through the SDK
independently of whether the files plugin is enabled.

The bytes exist before a referencing result commits. Identical bytes share one
content address; the first stored media-type claim remains authoritative and
subsequent spills return that stored type. References are shared within the
workspace store, rather than owned by an individual producing plugin/session.

Service calls require a live invocation lease. Read-only leases may spill output
because this writes internal artifacts; hook leases may read but cannot spill.
Revoked leases may neither read nor spill. Byte pages and stored payloads are
bounded by the core store.

`spill_artifact_stream(Iterable[bytes], media_type=...)` consumes chunks under
the current lease, checks cancellation/revocation, and publishes only after EOF.
Invalid chunks, producer failures, cancellation, revocation, or the store's
append bound discard the incomplete spill. `spill_artifact(bytes | str)` remains
the convenience ingress. Stored originals, model projection allocations, and UI
folding are separate bounds; a model preview never replaces the original.
