# `voidcode.provider`

Capability-layer home for provider/model resolution contracts, provider registries, fallback semantics, and reusable provider configuration helpers.

## Owns

- provider and model reference schemas
- resolved provider config models
- provider registry and fallback resolution helpers
- provider-specific config validation that is independent of runtime session state

## Does not own

- graph execution orchestration
- runtime retry loops and provider attempt state
- session metadata persistence
- runtime error/event routing

## Runtime boundary

Runtime continues to own effective provider config resolution in-session, provider attempt tracking, and fallback execution flow. This package is the intended home for the pure provider-control-plane primitives that runtime consumes.

## Current status

The active implementation is still split across `src/voidcode/runtime/model_provider.py`, `src/voidcode/runtime/provider_errors.py`, and `src/voidcode/runtime/service.py`.
