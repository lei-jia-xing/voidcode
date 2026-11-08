import { test, expect, type Page } from "@playwright/test";
import { spawn, type ChildProcess } from "child_process";
import { mkdtempSync, writeFileSync } from "fs";
import { createServer } from "net";
import * as os from "os";
import * as path from "path";
import { fileURLToPath } from "url";
/**
 * Monitor smoke on the real served core: the hidden shortcut action opens a
 * live roster through `command.input` only, navigates without touching the
 * prompt draft, and closes back to the composer. No provider is configured,
 * so the roster is the explicit empty state and nothing reaches a model.
 */

test.setTimeout(120000);
const root = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../..",
);
const python = path.join(
  root,
  ".venv",
  process.platform === "win32" ? "Scripts/python.exe" : "bin/python",
);
const token = "monitor-ui-test-token";
let proc: ChildProcess | undefined;
let base = "";
let logs = "";
let workspace = "";

async function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const listener = createServer();
    listener.on("error", reject);
    listener.listen(0, "127.0.0.1", () => {
      const address = listener.address();
      if (!address || typeof address === "string")
        return reject(new Error("No free port"));
      listener.close(() => resolve(address.port));
    });
  });
}

/** Raw RPC helper for scene setup: no UI, exact request/response or throw. */
async function rpc(
  page: Page,
  calls: Array<{ id: string; method: string; params: object }>,
): Promise<Record<string, Record<string, unknown>>> {
  return page.evaluate(async (batch) => {
    const socket = new WebSocket(
      `${window.location.origin.replace("http", "ws")}/api/rpc`,
      "voidcode.rpc.v1",
    );
    const token = new URLSearchParams(window.location.search).get("token");
    const send = (id: string, method: string, params: object) =>
      new Promise<Record<string, unknown>>((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error("rpc timeout")), 15000);
        const onMessage = (event: MessageEvent) => {
          const message = JSON.parse(String(event.data)) as {
            id?: string;
            result?: Record<string, unknown>;
            error?: { message?: string };
          };
          if (message.id !== id) return;
          socket.removeEventListener("message", onMessage);
          clearTimeout(timer);
          if (message.error) reject(new Error(String(message.error.message)));
          else resolve(message.result ?? {});
        };
        socket.addEventListener("message", onMessage);
        socket.send(JSON.stringify({ jsonrpc: "2.0", id, method, params }));
      });
    await new Promise<void>((resolve, reject) => {
      socket.addEventListener("open", () => resolve(), { once: true });
      socket.addEventListener("error", () => reject(new Error("ws open")), {
        once: true,
      });
    });
    await send("auth", "connection.authenticate", { token });
    const out: Record<string, Record<string, unknown>> = {};
    for (const call of batch)
      out[call.id] = await send(call.id, call.method, call.params);
    socket.close();
    return out;
  }, calls);
}

/** Starts an isolated live-scene server; caller must release then teardown. */
async function startLiveScene(tag: string): Promise<{
  liveBase: string;
  liveToken: string;
  rootId: string;
  childId: string;
  grandchildId: string;
  siblingId: string;
  release: () => Promise<string>;
  teardown: () => Promise<void>;
}> {
  const liveWs = mkdtempSync(
    path.join(os.tmpdir(), `voidcode-monitor-${tag}-ws-`),
  );
  const liveState = mkdtempSync(
    path.join(os.tmpdir(), `voidcode-monitor-${tag}-st-`),
  );
  writeFileSync(
    path.join(liveWs, "voidcode.json"),
    JSON.stringify({ schema_version: 1 }),
  );
  const liveBase = `http://127.0.0.1:${await freePort()}`;
  const liveToken = `monitor-${tag}-token`;
  const env = {
    ...process.env,
    PYTHONPATH: path.join(root, "src"),
    PYTHONUNBUFFERED: "1",
  };
  const liveProc = spawn(
    python,
    [
      "-m",
      "voidcode",
      "--workspace",
      liveWs,
      "--state-dir",
      liveState,
      "serve",
      "--port",
      new URL(liveBase).port,
      "--bearer-token",
      liveToken,
    ],
    { cwd: root, env, stdio: ["ignore", "pipe", "pipe"] },
  );
  let liveLogs = "";
  liveProc.stdout?.on("data", (chunk) => {
    liveLogs += String(chunk);
  });
  liveProc.stderr?.on("data", (chunk) => {
    liveLogs += String(chunk);
  });
  const helper = spawn(
    python,
    [
      path.join(root, "frontend", "e2e", "live-scene.helper.py"),
      liveWs,
      liveBase,
      liveToken,
    ],
    { cwd: root, env, stdio: ["pipe", "pipe", "pipe"] },
  );
  let helperOut = "";
  helper.stdout?.on("data", (chunk) => {
    helperOut += String(chunk);
  });
  helper.stderr?.on("data", (chunk) => {
    helperOut += String(chunk);
  });
  const waitExit = (
    proc: ChildProcess,
    signal: NodeJS.Signals | null,
    timeoutMs: number,
  ): Promise<void> => {
    if (proc.exitCode !== null || proc.signalCode !== null)
      return Promise.resolve();
    const { promise, resolve, reject } = Promise.withResolvers<void>();
    const timer = setTimeout(() => {
      proc.kill("SIGKILL");
      reject(new Error(`Process did not exit within ${timeoutMs}ms`));
    }, timeoutMs);
    proc.once("exit", () => {
      clearTimeout(timer);
      resolve();
    });
    if (signal !== null) proc.kill(signal);
    return promise;
  };
  const release = async (): Promise<string> => {
    helper.stdin?.write("RELEASE\n");
    await waitExit(helper, null, 45000);
    if (helper.exitCode !== 0)
      throw new Error(`scene helper exited ${helper.exitCode}:\n${helperOut}`);
    return helperOut;
  };
  const teardown = async (): Promise<void> => {
    if (helper.exitCode === null) helper.kill("SIGKILL");
    await waitExit(liveProc, "SIGTERM", 10000);
  };
  try {
    const deadline = Date.now() + 90000;
    while (Date.now() < deadline) {
      if (helper.exitCode !== null)
        throw new Error(`scene helper exited early:\n${helperOut}`);
      if (liveProc.exitCode !== null) throw new Error(liveLogs);
      if (helperOut.includes("SCENE-READY")) break;
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    if (!helperOut.includes("SCENE-READY"))
      throw new Error(`scene never ready:\n${helperOut}\n${liveLogs}`);
    const rootId = /SCENE-ROOT (\S+)/.exec(helperOut)?.[1] ?? "";
    const nodes = /SCENE-NODES child=(\S+) grandchild=(\S+) sibling=(\S+)/.exec(
      helperOut,
    );
    if (!rootId || !nodes) throw new Error(`bad scene lines:\n${helperOut}`);
    const [, childId, grandchildId, siblingId] = nodes;
    return {
      liveBase,
      liveToken,
      rootId,
      childId,
      grandchildId,
      siblingId,
      release,
      teardown,
    };
  } catch (error) {
    await teardown();
    throw error;
  }
}
test.beforeAll(async () => {
  workspace = mkdtempSync(path.join(os.tmpdir(), "voidcode-monitor-ui-ws-"));
  const state = mkdtempSync(path.join(os.tmpdir(), "voidcode-monitor-ui-st-"));
  writeFileSync(
    path.join(workspace, "voidcode.json"),
    JSON.stringify({ schema_version: 1 }),
  );
  base = `http://127.0.0.1:${await freePort()}`;
  const env = {
    ...process.env,
    PYTHONPATH: path.join(root, "src"),
    PYTHONUNBUFFERED: "1",
  };
  proc = spawn(
    python,
    [
      "-m",
      "voidcode",
      "--workspace",
      workspace,
      "--state-dir",
      state,
      "serve",
      "--port",
      new URL(base).port,
      "--bearer-token",
      token,
    ],
    { cwd: root, env, stdio: ["ignore", "pipe", "pipe"] },
  );
  proc.stdout?.on("data", (chunk) => {
    logs += String(chunk);
  });
  proc.stderr?.on("data", (chunk) => {
    logs += String(chunk);
  });
  const deadline = Date.now() + 30000;
  while (Date.now() < deadline) {
    if (proc.exitCode !== null) throw new Error(logs);
    try {
      const response = await fetch(`${base}/api/health`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (response.ok) return;
    } catch {
      /* The local server is still starting. */
    }
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  throw new Error(`Core did not start: ${logs}`);
});

test.afterAll(async () => {
  if (!proc || proc.exitCode !== null || proc.signalCode !== null) return;
  const process = proc;
  await new Promise<void>((resolve) => {
    const timer = setTimeout(() => {
      process.kill("SIGKILL");
      resolve();
    }, 10000);
    process.once("exit", () => {
      clearTimeout(timer);
      resolve();
    });
    process.kill("SIGTERM");
  });
});

test("the monitor action browses read-only and restores the composer", async ({
  page,
}) => {
  const inputs: Array<{ name?: string; args?: Record<string, unknown> }> = [];
  const invokes: string[] = [];
  page.on("websocket", (socket) =>
    socket.on("framesent", (event) => {
      const message = JSON.parse(String(event.payload)) as {
        method?: string;
        params?: { name?: string; arguments?: Record<string, unknown> };
      };
      if (message.method === "command.input" && message.params?.name) {
        inputs.push({
          name: message.params.name,
          args: message.params.arguments,
        });
      }
      if (message.method === "command.invoke" && message.params?.name) {
        invokes.push(message.params.name);
      }
    }),
  );
  await page.goto(`${base}/?token=${token}`);
  await expect(page.locator('[data-testid^="session-tab-"]')).toHaveCount(1);

  // Hidden action: no slash suggestion, but an accessible button by description.
  await page.getByTestId("prompt-input").fill("/subagents");
  await expect(page.getByTestId("slash-suggestions")).toHaveCount(0);
  const action = page.getByTestId("shortcut-action-subagents.monitor");
  await expect(action).toBeVisible();
  await expect(action).toHaveAttribute("type", "button");

  // The draft survives opening: no fake slash submit, no prompt clear.
  await page.getByTestId("prompt-input").fill("draft stays");
  await action.click();
  const panel = page.getByTestId("command-presentation");
  await expect(panel).toBeVisible();
  await expect(panel).toContainText("Running subagents");
  await expect(page.getByTestId("choices-empty")).toBeVisible();
  await expect(page.getByTestId("prompt-input")).toHaveValue("draft stays");

  // Read-only open: exactly the roster input, no invoke at all.
  await expect
    .poll(() => inputs.filter((c) => c.name === "subagents.monitor").length)
    .toBeGreaterThanOrEqual(1);
  expect(inputs[0]).toEqual({ name: "subagents.monitor", args: {} });
  expect(invokes).toHaveLength(0);

  // Filter is a real controlled input; Escape clears it without closing.
  const filter = page.getByTestId("choices-filter");
  await filter.fill("zzz-no-match");
  await expect(page.getByTestId("choices-empty")).toBeVisible();
  await filter.press("Escape");
  await expect(filter).toHaveValue("");
  await expect(panel).toBeVisible();

  // Close returns to the composer with the draft intact.
  await page.getByTestId("command-view-close").click();
  await expect(panel).toHaveCount(0);
  await expect(page.getByTestId("prompt-input")).toHaveValue("draft stays");
  await expect(page.getByTestId("prompt-input")).toBeFocused();

  // Reload never resurrects the panel from history.
  await page.reload();
  await expect(page.locator('[data-testid^="session-tab-"]')).toHaveCount(1);
  await expect(panel).toHaveCount(0);
});

test("disabling subagents removes the monitor action and open panel", async ({
  page,
}) => {
  await page.goto(`${base}/?token=${token}`);
  await expect(page.locator('[data-testid^="session-tab-"]')).toHaveCount(1);
  const action = page.getByTestId("shortcut-action-subagents.monitor");
  await expect(action).toBeVisible();
  await action.click();
  const panel = page.getByTestId("command-presentation");
  await expect(panel).toContainText("Running subagents");

  // Rewrite the served config with subagents off, then reload through RPC.
  // The catalog refresh must drop the button and the open panel, with no
  // silent re-read of the removed action.
  writeFileSync(
    path.join(workspace, "voidcode.json"),
    JSON.stringify({
      schema_version: 1,
      plugins: { subagents: { enabled: false } },
    }),
  );
  await page.evaluate(async () => {
    const socket = new WebSocket(
      `${window.location.origin.replace("http", "ws")}/api/rpc`,
      "voidcode.rpc.v1",
    );
    const token = new URLSearchParams(window.location.search).get("token");
    const send = (id: string, method: string, params: object) =>
      new Promise<Record<string, unknown>>((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error("rpc timeout")), 15000);
        const onMessage = (event: MessageEvent) => {
          const message = JSON.parse(String(event.data)) as {
            id?: string;
            result?: Record<string, unknown>;
            error?: { message?: string };
          };
          if (message.id !== id) return;
          socket.removeEventListener("message", onMessage);
          clearTimeout(timer);
          if (message.error) reject(new Error(String(message.error.message)));
          else resolve(message.result ?? {});
        };
        socket.addEventListener("message", onMessage);
        socket.send(JSON.stringify({ jsonrpc: "2.0", id, method, params }));
      });
    await new Promise<void>((resolve, reject) => {
      socket.addEventListener("open", () => resolve(), { once: true });
      socket.addEventListener("error", () => reject(new Error("ws open")), {
        once: true,
      });
    });
    await send("auth", "connection.authenticate", { token });
    const catalog = await send("cat", "command.catalog", {});
    const session = (await send("list", "session.list", { limit: 1 })) as {
      sessions: Array<{ session_id: string }>;
    };
    const result = (await send("reload", "command.invoke", {
      session_id: session.sessions[0].session_id,
      name: "core.reload",
      arguments: {},
      expected_revision: catalog.revision_id,
    })) as { status?: string };
    socket.close();
    if (result.status !== "ok") throw new Error(`reload ${result.status}`);
  });
  await expect(action).toHaveCount(0);
  await expect(panel).toHaveCount(0);
});

test("a gated live scene browses, denies, allows, and restores the draft", async ({
  browser,
}) => {
  const scene = await startLiveScene("live");
  const { liveBase, liveToken, rootId, childId, grandchildId, siblingId } =
    scene;
  try {
    const context = await browser.newContext();
    const live = await context.newPage();
    try {
      await live.goto(`${liveBase}/?token=${liveToken}`);
      await live.getByTestId(`session-tab-${rootId}`).click();
      await live.getByTestId("prompt-input").fill("live draft here");
      await live.getByTestId("shortcut-action-subagents.monitor").click();
      const panel = live.getByTestId("command-presentation");
      await expect(panel).toContainText("Running subagents");
      await expect(live.getByTestId(`choice-option-${childId}`)).toContainText(
        "LIVE-CHILD-label",
      );
      await expect(
        live.getByTestId(`choice-option-${grandchildId}`),
      ).toContainText("LIVE-GRAND-label");
      await expect(
        live.getByTestId(`choice-option-${siblingId}`),
      ).toContainText("LIVE-SIB-label");
      // Detail of the cross-plugin grandchild, then back to the roster.
      await live.getByTestId(`choice-option-${grandchildId}`).click();
      await expect(panel).toContainText(grandchildId);
      await expect(panel).toContainText("Back to roster");
      await panel.getByRole("button", { name: "Back to roster" }).click();
      await expect(live.getByTestId(`choice-option-${childId}`)).toBeVisible();
      // Deny first: the confirmation names the child scope; denied work keeps
      // running and the detail stays live.
      await live.getByTestId(`choice-option-${childId}`).click();
      await expect(panel).toContainText("Cancel subtree");
      await panel.getByRole("button", { name: "Cancel subtree" }).click();
      const confirmation = live.getByTestId("pending-confirmation-banner");
      await expect(confirmation).toContainText("1 descendant(s)");
      await expect(confirmation).toContainText(
        "Root and siblings are unaffected",
      );
      await live.getByTestId("confirm-deny-btn").click();
      await expect(confirmation).toHaveCount(0);
      await expect(panel).toContainText(childId);
      await expect(panel).toContainText("Cancel subtree");
      const denied = await rpc(live, [
        { id: "s1", method: "session.get", params: { session_id: childId } },
        {
          id: "s2",
          method: "session.get",
          params: { session_id: grandchildId },
        },
        { id: "s3", method: "session.get", params: { session_id: siblingId } },
      ]);
      expect(denied.s1.status).toBe("running");
      expect(denied.s2.status).toBe("running");
      expect(denied.s3.status).toBe("running");
      // Allow from the same detail: the durable completion receipt renders as
      // a transcript row with requested (not stopped) wording + target short id.
      await panel.getByRole("button", { name: "Cancel subtree" }).click();
      await expect(confirmation).toBeVisible();
      await live.getByTestId("confirm-allow-btn").click();
      await expect(confirmation).toHaveCount(0);
      const receipt = live
        .locator('[data-testid^="event-"]')
        .filter({ hasText: "Cancellation requested" });
      await expect(receipt).toBeVisible();
      await expect(receipt).toContainText(childId.slice(0, 8));
      // Close restores the exact draft and focus.
      await live.getByTestId("command-view-close").click();
      await expect(panel).toHaveCount(0);
      await expect(live.getByTestId("prompt-input")).toHaveValue(
        "live draft here",
      );
    } finally {
      await context.close();
    }
    const helperOut = await scene.release();
    if (!helperOut.includes("SCENE-RELEASED"))
      throw new Error(`scene never released:\n${helperOut}`);
    // After release the cancelled subtree settles interrupted while root and
    // sibling complete; the helper already asserted the root thread returned.
    const settledContext = await browser.newContext();
    const settledPage = await settledContext.newPage();
    try {
      await settledPage.goto(`${liveBase}/?token=${liveToken}`);
      const settled = await rpc(settledPage, [
        { id: "c1", method: "session.get", params: { session_id: childId } },
        {
          id: "c2",
          method: "session.get",
          params: { session_id: grandchildId },
        },
        { id: "c3", method: "session.get", params: { session_id: siblingId } },
        { id: "c4", method: "session.get", params: { session_id: rootId } },
      ]);
      expect(settled.c1.status).toBe("interrupted");
      expect(settled.c2.status).toBe("interrupted");
      expect(settled.c3.status).toBe("completed");
      expect(settled.c4.status).toBe("completed");
    } finally {
      await settledContext.close();
    }
  } finally {
    await scene.teardown();
  }
});
