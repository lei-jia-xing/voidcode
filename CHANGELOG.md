# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project aims to follow [Semantic Versioning](https://semver.org/).


## [0.1.1] - 2026-05-12



### Added


- **runtime:** harden harness runtime adoption (#487)

- **runtime:** finalize harness policy safeguards (#488)



### Build


- **release:** add git-cliff changelog generation



### Fixed


- **build:** include frontend sources in source distributions

- **runtime:** preserve full reasoning output (#489)



## [0.1.0] - 2026-05-10



### Added


- add in-process runtime streaming transport (#17) (#28)

- add runtime permission engine (#29)

- add web runtime transport foundation (#31)

- replace mocked frontend session state with runtime data (#32)

- render runtime timeline and activity panels (#33)

- add real write_file approval slice (#36)

- add real shell_exec approval slice (#37)

- add real grep tool slice (#38)

- add HTTP approval resolution endpoint (#39)

- add runtime config surface (#40)

- add extension infrastructure foundation (#41)

- close the final CLI streaming blockers (#42)

- render frontend final output for read-only runs (#46)

- close hooks/config MVP semantics (#48)

- add chat-first Textual TUI client (#49)

- add tui minimal implement (#52)

- abut web to backend (#53)

- close MVP client loops (#54)

- add execution engine selection foundation (#63)

- add provider-model runtime abstraction (#64)

- add provider-backed single-agent execution engine (#65)

- implement runtime-managed skill execution semantics (#73)

- **tools:** expand tool registry, normalize tool paths, and make shell_exec cross-platform (#72)

- **runtime:** manage LSP servers inside the runtime (#74)

- **runtime:** land ACP as a runtime-managed control plane (#75)

- **tui:** align the TUI with runtime sessions and events (#77)

- **runtime:** add context window management for single-agent runs (#78)

- **runtime:** add provider fallback handling for single-agent runs (#79)

- **runtime:** make execution engine step budget configurable (#80)

- **runtime:** add persisted resume checkpoints for approval resume (#81)

- **hook:** add runtime-owned formatter hook presets (#89) (#92)

- **runtime:** harden provider config resolution and persistence (#90) (#93)

- **lsp:** add preset and workspace-root capability layer (#102)

- **runtime:** add developer diagnostics for lsp and fallback paths (#103)

- **runtime:** add runtime-managed MCP tool plumbing (#105)

- add ast-grep structural search and replace substrate (#123)

- **runtime:** add context window capacity metadata

- **runtime:** add persisted tui preference config layers

- **tui:** add persisted preference commands and pickers

- improve formatter presets and expand built-in catalog (#127)

- **web:** improve session usability in the app shell (#137)

- **edit:** align formatter-aware edit results (#135)

- **runtime:** inject applied skills into provider-backed execution (#145)

- **runtime:** add background task substrate (#143)

- **runtime:** reserve async lifecycle hook surfaces (#144)

- **doctor:** add runtime capability doctor for external tool readiness (#138)

- add runtime session results and notifications (#146)

- **lsp:** expand builtin preset catalog (#149)

- **runtime:** add parent-child session lineage (#148)

- **lsp:** derive workspace defaults for common projects (#156)

- **runtime:** apply minimal leader agent preset slice (#152) (#157)

- **agent:** add declaration layer for leader config (#159)

- **agent:** add multi-role declaration skeletons (#160)

- add session continuity memory slice (#162)

- minimal runtime skill execution model (#161)

- **runtime:** enforce executable agent preset boundary (#171)

- **runtime:** enforce agent tool boundaries (#172)

- **runtime:** execute lifecycle hook surfaces (#173)

- **runtime:** accept provider credentials from environment (#176)

- **runtime:** query background tasks by parent session (#181)

- **runtime:** emit background task waiting approval event (#182)

- **runtime:** enforce runtime tool timeouts (#180) (#183)

- **runtime:** enforce stable runtime request metadata schema (#186)

- **runtime:** inject agent-facing tool guidance via sidecar files and complete second-wave docs (#192)

- **runtime:** recover delegated leader task visibility (#193)

- **runtime:** emit MCP server failure events (#195)

- deliver chat-first web shell with runtime settings support (#198)

- **runtime:** align agent prompts and remove leader_mode (#199)

- **runtime:** add question flow and runtime-backed agent tools (#201)

- **runtime:** add tool execution start observability (#202)

- **runtime:** cut over to delegated execution architecture (#207)

- **provider:** harden provider resolution and discovery semantics (#216)

- **agent:** harden builtin preset prompt semantics (#218)

- **mcp:** harden runtime sessions on official SDK (#220)

- **skills:** harden local skill subsystem contracts (#222)

- **runtime:** harden runtime with extracted collaborators (#205) (#221)

- introduce modular command system (#228)

- **runtime:** add session debug snapshot surface (#230)

- ship workspace-scoped web MVP (#231)

- **web:** add voidcode web launcher with OpenCode-aligned contracts (#233)

- **provider:** add token metadata for context compaction (#247)

- **runtime:** add agents config map (#250)

- **runtime:** add agent refs and acp status (#251)

- **web:** reflect configured provider models (#252)

- **runtime:** add token-budget tool retention (#253)

- **runtime:** add context window policy config (#260)

- **agent:** add model-aware manifest metadata (#262)

- **provider:** add provider capability inspection (#263)

- **cli:** add schema-backed config workflow (#264)

- **acp:** add stdio runtime facade (#280)

- **agent:** productize top-level planning agent (#282)

- **cli:** polish CLI reference client UX (#283)

- surface provider readiness diagnostics (#281)

- **provider:** harden model metadata for routing (#291)

- **runtime:** add background task concurrency controls (#292)

- **runtime:** route models by agent category (#293)

- **runtime:** make skills catalog-first and model-loadable by default (#295)

- **runtime:** add delegated subagent execution baseline (#297)

- **cli:** polish delegated task operator UX (#303)

- surface MCP health and config visibility (#305)

- **web:** complete runtime integration parity (#307)

- add first-task readiness diagnostics (#306)

- **runtime:** add context-pressure event, config thresholds, and non-fatal hook surface (#308)

- **web:** improve runtime tool visibility and reasoning controls (#309)

- **runtime:** harden SQLite storage operations (#315)

- **runtime:** productize context memory compaction (#316)

- **runtime:** complete background task lifecycle semantics (#317)

- **runtime:** add conversation undo (#327)

- **runtime:** add reasoning effort config (#331)

- **runtime:** make todo state runtime-owned (#332)

- **runtime:** add active run interruption (#329)

- **web:** redesign tool activity UI (#319)

- **runtime:** add portable session bundles (#339)

- **runtime:** add provider context inspector (#340)

- **runtime:** add provider failure recovery checkpoints (#341)

- **runtime:** add reasoning parts and thinking controls (#343)

- rework tool compaction and OpenCode-Go feedback (#342)

- **runtime:** runtime-owned external directory permissions with tool integration Body (#333)

- **runtime:** simplify delegated category taxonomy (#351)

- **runtime:** add background task observability (#350)

- **runtime:** add provider context diagnostic policy (#364)

- **runtime:** add tmp-first tool output artifacts (#363)

- **runtime:** stream shell exec progress (#370)

- **hook:** add builtin hook preset catalog (#379)

- **runtime:** add production-ready model-assisted continuity distillation with deterministic fallback (#377)

- **runtime:** materialize hook preset guidance (#386)

- **runtime:** expose hook preset snapshots (#387)

- **agent:** harden builtin role boundaries (#389)

- **command:** add minimal builtin prompt commands (#391)

- **runtime:** define agent capability bindings (#400)

- **runtime:** add pattern permission rules (#404)

- **cli:** add pending question answers (#406)

- **runtime:** add local custom tools (#407)

- **agent:** support local custom agent manifests (#408)

- **runtime:** add workflow preset harness (#409)

- **frontend:** expose background task output (#410)

- **cli:** add human-readable run trace (#418)

- **web:** improve runtime token status UI (#419)

- **runtime:** productionize storage and delegated retry (#445)

- **runtime:** add workflow handoff and batched tool calls (#446)

- add runtime-owned continuation loops (#447)

- **runtime:** add intensive loop verification state (#449)

- **runtime:** add context continuity safeguards (#450)

- **runtime:** add structured hook diagnostics (#452)

- **command:** add init slash command (#454)

- **runtime:** add context transform hooks (#455)

- **runtime:** add context transform registry (#456)

- **runtime:** add scoped transform policy (#457)

- **runtime:** add request transform narrowing (#458)

- **runtime:** add transform ordering diagnostics (#459)

- **runtime:** add transform failure policy (#460)

- **runtime:** add typed extension observability (#463)

- **context:** add readme context and write guards (#464)

- **tools:** add tool governance workflow guards (#465)

- **harness:** lightweight agent harness upgrades — phase 1 (plan/act, lazy skills, disciplined todos) (#467)

- **runtime:** phase 2 prompt assembly and context tier productization (#468)

- **runtime:** compact recent-tier context under pressure (#469)

- add tmux-backed interactive shell tool (#470)

- add background process tools and harden interactive shell (#471)

- **frontend:** improve runtime visibility and child session navigation (#474)

- **runtime:** add delegated idle reminders and process guardrails (#475)

- **runtime:** add workflow mode harness integration (#480)

- **runtime:** add workspace memory capability (#481)

- polish frontend runtime UX and delegated sessions (#485)



### CI


- add opencode-review

- add opencode-triage

- fix permission for review

- correctly set ai related ci

- add qwen3.6 to review

- change qwen 3.6 to gpt5.4

- open gpt review

- remove review ci

- keep release builds on supported Python (#190)

- cancel bot's permission to pr

- remove useless opencode review

- remove issue triage

- publish release builds to PyPI via trusted publishing



### Changed


- use lib to transfer hand-write html exact

- use lsprotocol to pydantic

- adopt rapidfuzz and unidiff for tool tooling (#109)

- **mcp:** stabilize runtime boundary and extract config/schema/types (#117)

- **skills:** extract capability layer from runtime (#128)

- **acp:** extract validated ACP contracts (#147)

- **lsp:** make builtin server names the primary config path

- consolidate boundary-layer parsing with Pydantic models (#203)

- **frontend:** align web shell controls with OpenCode-style hierarchy (#232)

- **runtime:** tighten runtime config surface (#294)

- **runtime:** enforce assembled context as sole provider boundary (#296)

- **runtime:** extract tool scoping policy (#378)

- **provider:** move config projection into adapters (#421)

- **runtime:** extract permission context resolver (#441)

- **runtime:** move background task lifecycle state (#448)

- **runtime:** extract pure helpers from service.py into 14 domain modules



### Documentation


- add some plan

- remove unexist doc

- add runtime contract documents

- add runtime config and transport contracts

- align truth-source references across project docs

- clarify MVP and frontend documentation

- align approval flow contract with runtime event envelope schema (#26)

- complete runtime config contract (#27)

- define TUI MVP interaction model (#30)

- remove unexist doc

- add MVP demo verification guide (#20) (#35)

- localize repository docs and templates in Chinese (#43)

- sync web transport state with issue #23 (#45)

- add post-MVP technical design

- claude code's advise for arch

- update doc to current state

- define retention and checkpoint invalidation semantics (#86)

- update todo

- define capability-layer ownership boundaries (#94)

- define modules reponsibility

- sync docs to code

- sync #82 completion status

- add runtime-owned scheduler design spec (#101)

- add memory reference

- sync MCP integration status and clarify LangGraph scope (#114)

- add plan for agent tool

- add mcp-related doc

- **runtime:** refresh roadmap and current state

- **mvp:** mark issue 84 follow-up complete

- remove useless doc

- add tui preferences design

- update roadmap

- agent related doc update

- add background task delegation contract

- sync repo state and roadmap references

- **runtime:** align hook and config contracts

- localize agent tooling adoption plan

- **runtime:** include execution engine in config examples

- add reasoning effort decision draft

- **runtime:** recommend builtin-name LSP server config

- **current-state:** align MVP status with current runtime

- **roadmap:** refresh active backlog references

- **architecture:** sync ACP and LSP maturity notes

- sync docs with code

- make tool calls agent-consumable (#168)

- contracts update

- align transport baseline and contract status

- add agent-facing tools guide (#177)

- AGENTS.md update

- transfer top docs into en_US

- tighten doc

- add CLI and Web failure recovery runbook (#229)

- **runtime:** define deterministic engine lifecycle (#259)

- align product status with current code

- mark prompt command MVP item complete

- **runtime:** plan service decomposition (#436)



### Fixed


- emit failed terminal stream chunk

- **runtime:** fall back when persisted checkpoints are unreadable (#85)

- **apply_patch:** normalize mode-only patches and improve git error handling (#104)

- **lsp:** address shutdown cleanup and UNC URI follow-ups (#106)

- add apply_patch fuzz-style tests and normalize diff headers (#125)

- **runtime:** make queued background task cancellation atomic

- **runtime:** recheck cancellation before background dispatch

- **lsp:** match canonical Dockerfile names

- **lsp:** guard workspace-scoped lifecycle reuse (#151)

- **runtime:** complete ACP managed lifecycle slice (#155)

- **runtime:** preserve resume fallback null content

- **provider:** inject runtime continuity summary into prompts (#189)

- ruff formatte

- **runtime:** harden hook execution semantics (#217)

- **graph:** harden provider graph terminal handling (#219)

- **lsp:** harden request handling and failure bounds (#223)

- **runtime:** enforce token retention bounds

- **web:** prevent launcher e2e browser popups (#261)

- stabilize provider-backed single-agent runs (#266)

- preserve tool output fidelity (#348)

- **provider:** keep redaction sentinels out of tool arguments (#362)

- **runtime:** return approval denials as tool feedback (#369)

- **runtime:** retry transient provider failures (#373)

- **runtime:** align failure signals and tool feedback (#376)

- **runtime:** respect gitignore in review tree (#388)

- **agent:** reduce leader QA false confidence (#392)

- **runtime:** avoid shell read probes as external writes (#393)

- **tools:** clarify tool argument validation feedback (#416)

- fix refresh replay and simplify runtime provider surfaces (#420)

- **runtime:** use production-grade timeout defaults (#422)

- **frontend:** improve runtime provider settings and subagent navigation (#423)

- **tools:** improve edit mismatch diagnostics (#434)

- fix web/provider/runtime approval flow regressions (#439)

- improve cross-platform compatibility (#440)

- improve default MCP and LSP agent ergonomics (#443)

- keep MCP disabled unless configured (#444)

- **runtime:** harden long-task stability (#451)

- **runtime:** update config schema url

- **cli:** align config schema url expectations

- **runtime:** close transform block bypass for off diagnostics (#462)

- **tools:** remove shell interactivity classifier (#466)

- **tools:** harden background process cleanup (#472)

- **runtime:** stabilize delegated task terminal truth (#473)

- auto-assign web launcher ports and backfill waiting-child reminders (#476)

- **build:** package web assets into Python release artifacts (#478)

- **provider:** add opencode zen integration and readiness checks (#477)

- **build:** honor verifier timeout for silent web launchers (#479)

- **runtime:** align server startup, state DB recovery, and harden git status snapshot decoding on Windows (#482)

- **runtime:** default external directory access to allow (#483)

- **hooks:** enable runtime hooks by default (#484)

- harden delegated child session frontend flow (#486)



### Testing


- add frontend component test harness (#34)

- group graph unit coverage

- group tool unit coverage

- group runtime unit coverage

- group interface unit coverage

- group package import checks

- group project metadata checks

- add unit path helper module

- mark unit test domain packages

- centralize CLI smoke subprocess paths

- remove graph unit sys.path setup

- remove project unit path boilerplate

- remove runtime import path boilerplate

- remove runtime config path boilerplate

- remove runtime service path boilerplate

- remove tool unit sys.path setup

- capture the remaining skill execution gap (#91)

- add apply_patch test

- **grep:** add Hypothesis fuzz coverage (#131)

- **multi_edit:** add Hypothesis fuzz coverage (#132)

- **runtime:** add fuzz coverage for runtime-backed agent tools (#214)

- parallelize python test tiers (#265)

- add marker-driven backend test lanes (#344)

- **runtime:** cover background task reconciliation idempotency (#349)

- **runtime:** add provider context parity coverage (#361)

- **agent:** add manifest boundary invariant coverage (#384)

- add issue #425 contract parity coverage (#437)

- **runtime:** cover session restart persistence (#438)



### chore


- pin python version to 3.14

- amend coding-standards & gitignore

- remove uneccessary file

- add agent.md

- add team config

- readme update

- untrack .coverage

- pin uv & bun version

- stop ai ci in a period

- stop ai review

- update ignore file

- disabled review ci

- do not publish to pypi

- downgrade python to 3.13 & docs update

- tighten langgraph pyright boundary

- add codeOWNERS to review

- remove test entry in pr template

- add codex config file to ignore

- ignore .voidcode.json

- add litterred

- add linter & formmatter for frontend

- remove pyright related

- **deps:** bump actions/github-script from 7 to 9 (#395)

- add codeowner

- remove dead code

- remove dangling list tool references

- prep voidcode 0.1.0 release (#453)

- change to alpha version



### config


- migrate from mypy to basedpyright
