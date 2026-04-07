# Stream Transport Contract

Source issue: #17

## Purpose

Define the MVP transport expectations for delivering runtime events and final output to CLI, TUI, and web clients.

## Status

The current code exposes ordered events and output through both in-process runtime iteration and a local HTTP/SSE transport. Broader transport work such as WebSocket delivery is still planned.

The current CLI is still a replay/print consumer, while the web shell uses the minimal local HTTP/SSE transport for live session streaming.

## Transport responsibilities

The transport layer must deliver:

- ordered runtime events
- final output
- enough session identity to associate the stream with persistence and resume

It must not:

- bypass runtime governance
- invent private client-only state that is not recoverable

## MVP delivery semantics

- streams are session-scoped
- event ordering follows `EventEnvelope.sequence`
- clients must be able to render partial progress before final output exists
- persisted replay must preserve the same observable ordering model as live delivery

## Client expectations

### CLI
- may consume a completed response and print events in order

### TUI
- should consume ordered events as an activity timeline for an active turn
- should be able to switch from live stream to persisted replay without semantic drift

### Web client
- should consume the same event semantics as the TUI
- should render event progression, tool activity, approvals, and final output from runtime-provided data

## Recommended transport abstraction

The runtime exposes a transport-neutral event stream contract that is currently bound to:

- in-process iteration for local/runtime consumers
- local HTTP/SSE delivery for the web shell

It can later also be bound to:

- WebSocket delivery

This document defines behavior, not the final long-term wire protocol choice.

## Invariants

- live delivery and replay share the same event vocabulary
- clients can show progress without parsing human-oriented text output
- final output does not replace the need for ordered event visibility

## Non-goals

- selecting one final wire protocol today
- post-MVP multi-agent multiplexing semantics
- token/cost telemetry transport requirements

## Acceptance checks

- the contract is sufficient to implement one live stream consumer and one replay consumer
- transport choices can change later without changing the event schema itself
- runtime persistence remains the source of truth for replay behavior
