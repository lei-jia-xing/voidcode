import "./test-local-storage";
import { Profiler, type ReactElement } from "react";
import { render, act, cleanup } from "@testing-library/react";
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import App from "./App";
import { useAppStore } from "./store";
import type { RuntimeStatusSnapshot } from "./lib/runtime/types";
import "./i18n";

// The shell reads a single explicit `useShallow` slice of the store. This file
// pins that contract behaviourally with render counting: a write to state the
// slice does not carry must not commit the shell, a write to state it does
// carry must.
const statusSnapshot: RuntimeStatusSnapshot = {
  git: { state: "git_ready", root: "/workspace", error: null },
  lsp: { state: "stopped", error: null, details: {} },
  mcp: { state: "stopped", error: null, details: {} },
  acp: { state: "unconfigured", error: null, details: {} },
  background_tasks: {
    active_worker_slots: 0,
    queued_count: 0,
    running_count: 0,
    terminal_count: 0,
    default_concurrency: 1,
    provider_concurrency: {},
    model_concurrency: {},
    status_counts: {},
  },
};

vi.mock("./lib/runtime/client", () => ({
  RuntimeClient: {
    listWorkspaces: vi.fn(async () => ({
      current: {
        available: true,
        current: true,
        label: "workspace",
        last_opened_at: null,
        path: "/workspace",
      },
      recent: [],
      candidates: [],
    })),
    listSessions: vi.fn(async () => []),
    listProviders: vi.fn(async () => []),
    listAgents: vi.fn(async () => []),
    listSkills: vi.fn(async () => []),
    listCommands: vi.fn(async () => []),
    listNotifications: vi.fn(async () => []),
    listBackgroundTasks: vi.fn(async () => []),
    listSessionBackgroundTasks: vi.fn(async () => []),
    getStatus: vi.fn(async () => statusSnapshot),
    getSettings: vi.fn(async () => ({})),
    getSessionReplay: vi.fn(),
    getChildSessionContext: vi.fn(),
  },
}));

describe("App store slice", () => {
  let commits = 0;

  beforeEach(async () => {
    commits = 0;
    // Settle the shell's mount effects before any measurement so their own
    // commits are not attributed to the store writes under test.
    await act(async () => {});
  });

  afterEach(() => {
    cleanup();
  });

  function renderShell(): ReactElement {
    return (
      <Profiler
        id="shell"
        onRender={() => {
          commits += 1;
        }}
      >
        <App />
      </Profiler>
    );
  }

  it("only commits for writes its explicit slice carries", async () => {
    render(renderShell());
    await act(async () => {});

    // Fields the 97-field slice does not carry: agent/skill/command catalog
    // status, review mode, and the local run bookkeeping.
    const skipped: Array<Array<[string, unknown]>> = [
      [["skills", []]],
      [["skillsStatus", "loading"]],
      [["agentsStatus", "loading"]],
      [["commandsStatus", "loading"]],
      [["reviewMode", "files"]],
      [["cancelRequested", true]],
    ];
    for (const writes of skipped) {
      const before = commits;
      await act(async () => {
        for (const [key, value] of writes) {
          useAppStore.setState({ [key]: value } as never);
        }
      });
      expect(commits, `write to ${String(writes[0][0])} must not commit`).toBe(
        before,
      );
    }

    // Fields the slice carries because the shell paints them.
    for (const [key, value] of [
      ["settingsStatus", "loading"],
      ["providerValidationResults", { deepseek: { ok: true } }],
    ] as Array<[string, unknown]>) {
      const before = commits;
      await act(async () => {
        useAppStore.setState({ [key]: value } as never);
      });
      expect(commits, `write to ${key} must commit`).toBeGreaterThan(before);
    }
  });
});
