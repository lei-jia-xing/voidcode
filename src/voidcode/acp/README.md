# `voidcode.acp`

Capability-layer home for ACP request/response contracts, config schemas, and reusable adapter-facing models when ACP semantics become stable enough to stand on their own.

## Owns

- ACP envelope and state model definitions that are not runtime-coupled
- reusable ACP configuration schema helpers
- stable protocol contracts for ACP integrations

## Does not own

- connection lifecycle management
- runtime-managed availability state
- session persistence and resume behavior
- runtime event emission and recovery flow

## Runtime boundary

`src/voidcode/runtime/acp.py` remains the runtime-managed control plane. ACP should only move into this package when its pure contracts and schemas can be separated cleanly from runtime lifecycle ownership.

## Current status

This directory is a placeholder for a future capability layer. The current implementation still lives in `src/voidcode/runtime/acp.py`.
