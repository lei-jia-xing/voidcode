# Artifact references

The core stores immutable, content-addressed bytes in the workspace's state
directory. Plugins use SDK services to store/read them and pass references in
tool results. A producer does not call another plugin to publish an artifact.

```python
artifact = services.spill_artifact(content, media_type="text/plain; charset=utf-8")
result = ToolSuccess(
    "my_tool",
    TextOutput("Output saved", bounds=OutputBounds(reference=artifact)),
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

Readers call the byte service with a digest:

```python
page = services.read_artifact(artifact.sha256, offset=0, limit=4096)
```

`ArtifactPage` carries that same reference as `.artifact`, plus `offset`, `data`
and `eof`. Metadata is not restated using a second set of field names.

Only the `read` tool translates `artifact://` input into a digest and renders
bounded text/image output. Storage and byte paging remain available through the
SDK independently of whether the files plugin is enabled.

The bytes exist before a referencing result commits. Identical bytes share one
content address; the first stored media-type claim remains authoritative and
subsequent spills return that stored type. References are shared within the
workspace store, rather than owned by an individual producing plugin/session.

Service calls require a live invocation lease. Read-only leases may spill output
because this writes internal artifacts; hook leases may read but cannot spill.
Revoked leases may neither read nor spill. Byte pages and stored payloads are
bounded by the core store.
