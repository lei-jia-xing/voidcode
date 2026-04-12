# `voidcode.lsp`

Capability-layer home for LSP presets, language support definitions, config schemas, and registry logic.

## Owns

- language-to-server mappings
- default LSP presets and reusable server definitions
- pure config normalization and validation helpers
- LSP-specific capability contracts that do not depend on runtime session state

## Does not own

- process lifecycle and stdio management
- request routing from runtime entrypoints
- runtime event emission
- session persistence or resume state

## Runtime boundary

`src/voidcode/runtime/lsp.py` remains the runtime integration layer. It should depend on `voidcode.lsp` for reusable definitions and schemas, while continuing to own runtime-managed lifecycle, events, and effective session truth.

## Current status

This directory is a planned capability layer. The current implementation still lives in `src/voidcode/runtime/lsp.py` and `src/voidcode/tools/lsp.py`.
