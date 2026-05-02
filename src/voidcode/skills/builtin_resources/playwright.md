# Playwright Browser Verification

Use this skill when a task needs browser-based verification, screenshots, navigation checks, form testing, responsive layout checks, or end-to-end UI validation.

This is a VoidCode-local browser verification guide. It is inspired by common Playwright workflow patterns, but it is not a Claude plugin drop-in and does not assume plugin-specific directories, helper scripts, or automatic browser startup.

## Capability Model

- Treat Playwright as a configured capability. VoidCode exposes only a descriptor for the Playwright MCP server; execution requires the runtime/session to have an available configured Playwright MCP server or another browser tool surface.
- Do not claim that browser automation is available until the configured capability is actually present.
- Prefer visible, user-observable browser checks when diagnosing UI behavior, and capture screenshots or exact page state when useful.
- Keep browser work bounded to the requested application and avoid unrelated browsing.

## Verification Flow

1. **Identify the target**: determine the app URL, route, viewport, credentials or fixtures, and expected user flow.
2. **Confirm app availability**: verify the dev server or target site is reachable before interacting with it.
3. **Exercise the critical path**: navigate, click, type, submit forms, inspect visible state, and validate accessible labels or error messages where relevant.
4. **Check visual behavior**: use screenshots for layout, spacing, overflow, responsive breakpoints, loading states, modals, and menus.
5. **Report evidence**: include the browser actions taken, observed result, screenshots or selectors when available, and any limitations from missing configured capabilities.

## Good Uses

- Validate that a frontend change renders and behaves correctly.
- Reproduce a UI bug with concrete browser steps.
- Confirm responsive behavior across representative viewport sizes.
- Check login, form validation, navigation, modal, menu, or loading-state flows.
- Gather screenshots for review when a visual change is part of the task.

## Guardrails

- Do not mutate application data beyond what the requested test flow requires.
- Do not store secrets in scripts, screenshots, logs, or session output.
- Do not assume Playwright MCP is running globally; it remains descriptor/config-gated.
- If no browser capability is configured, state that limitation and fall back to code-level or build/test verification.
