# providers

Provider adapters, dispatch policy, and read-only provider/model information panels.

This package is a thin adapter: `create_plugin` forwards to the shared `voidcode.capabilities.providers` factory, which registers one adapter per shipped-table provider name.

## Surface

- Command `/providers` (no arguments): configured providers joined with their shipped table row (label, wire, shipped-model count) plus the declared-but-unconfigured ids.
- Command `/model` (optional `provider` id): without an id, this session's last committed dispatch plus configured providers. With an id, list its shipped models or, for a configured OpenAI-compatible endpoint with no shipped catalog, fetch `/models`. `/model openrouter` uses this live path. An unknown provider id is refused; a known provider with no shipped catalog and no configuration is refused rather than reported as an empty catalog. A shipped catalog is listed without configuration. An unreadable dispatch or failed listing is reported, never invented.
- Views `providers` and `model`: markdown panels rendering the same reports.
- Provider registrations: one adapter per shipped-table provider name; the core selects one registered name per turn by string only (it holds no credential concept) and this plugin resolves the native adapter, endpoint, credential, and usage pricing behind it.
- Provider errors cross one boundary redaction: every configured credential value is removed from the message and structured details before the error reaches the core, which persists `str(exc)`.

## Configuration

Takes the product-injected `providers` section: bootstrap discovers supported environment credentials and merges explicit configuration once, then records the private map. Built-in endpoints need no `base_url`; explicit fields win. An explicit nested `auth` block is authoritative over discovered flat credentials. There are no configuration fields that name an environment variable for later resolution: credentials are captured at admission and restored from the recorded snapshot. Public facts and results contain provider identities and model IDs, never credentials. Model selection and credential changes use configuration plus `/core reload`; restarts keep the recorded values. See [configuration](../../../../docs/configuration.md).

## Behavior and limits

- Handler- and config-readonly: neither command nor view writes plugin state, provider configuration, or staged events. Dispatch history comes from the calling session's committed transcript dispatch records. (These plugin commands still record normal core `COMMAND_COMPLETED` receipts on success or intentional error.)
- Live model listing reuses the provider's SDK client, authentication, and TLS settings. It sends one GET with an SDK timeout of 10 seconds or lower, with no retries or model turn; malformed responses and HTTP failures stay errors. Views and startup never fetch remote catalogs.
- The normal turn path streams and is single-attempt: a failure after dispatch is reported, never retried or rerouted to another provider/model. Runtime retries apply only to non-streaming provider implementations.
- OpenAI Chat Completions keeps assistant tool-call turns and tool messages intact. Because the tool role cannot carry images, tool-result images are appended after each tool batch as one labelled user feedback message identifying the tool call id/name; an image already emitted in that payload is not repeated. User message images keep the normal `image_url` content path.
- `close` closes every registered provider.
