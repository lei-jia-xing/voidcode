import "./test-local-storage";
import { Profiler, type ReactElement } from "react";
import { render, act, cleanup } from "@testing-library/react";
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { QueryClientProvider } from "@tanstack/react-query";
import App from "./App";
import { useAppStore } from "./store";
import { queryClient } from "./lib/queries";
import type { RuntimeStatusSnapshot } from "./lib/runtime/types";
import "./i18n";

// The shell reads an explicit `useShallow` slice of the store — now client state
// and the streamed-run projection only, because the server payloads it paints
// come from the query cache. This file pins that contract behaviourally with
// render counting: a write to store state the slice does not carry must not
// commit the shell, a write to state it does carry must. A write to a *query*
// commits it through the cache instead (see the query hooks' own suites).
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
    getReview: vi.fn(async () => ({
      root: "/workspace",
      git: { state: "git_ready" },
      changed_files: [],
      tree: [],
    })),
    getSettings: vi.fn(async () => ({})),
    getSessionReplay: vi.fn(),
    getChildSessionContext: vi.fn(),
  },
}));

describe("App store slice", () => {
  let commits = 0;

  beforeEach(async () => {
    commits = 0;
    // The shell is given the process-wide client (the one the store reads
    // through), so each test starts from an empty cache.
    queryClient.clear();
  });

  // Settle the shell's mount work before any measurement so its own commits (the
  // language change, the boot queries, the catalog reconciliation) are never
  // attributed to the store writes under test. One flush is not enough: those
  // commits land several turns after render.
  async function settleShell() {
    for (let turn = 0; turn < 5; turn += 1) {
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 0));
      });
    }
  }

  afterEach(() => {
    cleanup();
  });

  function renderShell(): ReactElement {
    return (
      <QueryClientProvider client={queryClient}>
        <Profiler
          id="shell"
          onRender={() => {
            commits += 1;
          }}
        >
          <App />
        </Profiler>
      </QueryClientProvider>
    );
  }

  it("only commits for writes its explicit slice carries", async () => {
    render(renderShell());
    await settleShell();

    // Fields the slice does not carry: the review mode (painted by the review
    // panel only when it asks), the cancel flag (read by the cancel action, not
    // painted), and the replay token (lifecycle bookkeeping).
    const skipped: Array<Array<[string, unknown]>> = [
      [["reviewMode", "files"]],
      [["cancelRequested", true]],
      [["replayRequestId", 7]],
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
      ["runStatus", "running"],
      ["currentSessionOutput", "streamed answer"],
      ["agentPreset", "explore"],
    ] as Array<[string, unknown]>) {
      const before = commits;
      await act(async () => {
        useAppStore.setState({ [key]: value } as never);
      });
      expect(commits, `write to ${key} must commit`).toBeGreaterThan(before);
    }
  });
});
