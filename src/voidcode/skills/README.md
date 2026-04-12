# `voidcode.skills`

Capability-layer home for skill manifests, discovery rules, metadata parsing, and reusable skill registry definitions.

## Owns

- skill manifest format and parsing rules
- discovery path conventions
- reusable skill metadata models
- pure registry helpers that do not require runtime session state

## Does not own

- runtime prompt assembly
- session-bound applied skill state
- runtime event emission
- request execution semantics

## Runtime boundary

`src/voidcode/runtime/skills.py` remains the runtime integration layer until manifest parsing and discovery helpers are extracted into this package. Runtime should keep ownership of effective configuration and session-facing behavior.

## Current status

This directory is a planned capability layer. The current implementation still lives in `src/voidcode/runtime/skills.py`.
