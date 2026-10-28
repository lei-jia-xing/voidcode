# providers

Provider adapters, dispatch policy, and read-only provider/model information panels.

This package is a thin adapter: `create_plugin` forwards to the shared `voidcode.capabilities.providers` factory, which registers one adapter per shipped-table provider name.

## Surface

- Command `/providers` (no arguments): configured providers joined with their shipped table row (label, wire, shipped-model count) plus the declared-but-unconfigured ids.
- Command `/model` (optional `provider` id): with an id, that provider's shipped catalog models; without, this session's last committed dispatch plus configured providers. An unreadable dispatch payload is reported, never invented.
- Views `providers` and `model`: markdown panels rendering the same reports.
- Provider registrations: one adapter per shipped-table provider name; the core selects one registered name per turn by string only (it holds no credential concept) and this plugin resolves the native adapter, endpoint, credential, and usage pricing behind it.

## Configuration

Takes the product-injected `providers` section: product bootstrap resolves configured entries plus environment keys (`resolve_providers`) and hands every factory the map; the core only carries it through. Panels join configured names against the plugin's own shipped table and retain no credential values. Model selection is configuration plus hot reload — neither panel writes state. See [configuration](../../../../docs/configuration.md) for global config.

## Behavior and limits

- Handler- and config-readonly: neither command nor view writes plugin state, provider configuration, or staged events. Dispatch history comes from the calling session's committed transcript dispatch records. (These plugin commands still record normal core `COMMAND_COMPLETED` receipts on success or intentional error.)
- `close` closes every registered provider.
