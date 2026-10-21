import { test, expect } from "@playwright/test";
import { spawn, type ChildProcess } from "child_process";
import { existsSync, mkdtempSync, writeFileSync } from "fs";
import { createServer } from "net";
import * as os from "os";
import * as path from "path";
import { fileURLToPath } from "url";

/**
 * Core client smoke, offline: the real `voidcode.core serve` process owns the
 * workspace/state/revision, serves the built SPA (`frontend/dist`) at "/", and the
 * browser drives one session plus one plugin command. No provider is configured, so
 * nothing here reaches a model.
 *
 * Requires `frontend/dist` — run `bun run build` first (`mise run frontend:e2e` does it).
 */

test.describe.configure({ timeout: 180_000 });

const rootDir = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../..",
);
const token = "core-e2e-token";

let proc: ChildProcess | undefined;
let baseUrl = "";
let bootLog = "";

function freePort(): Promise<number> {
  const { promise, resolve, reject } = Promise.withResolvers<number>();
  const probe = createServer();
  probe.on("error", reject);
  probe.listen(0, "127.0.0.1", () => {
    const address = probe.address();
    if (address === null || typeof address === "string") {
      probe.close();
      reject(new Error("could not allocate a port"));
      return;
    }
    probe.close(() => resolve(address.port));
  });
  return promise;
}

function delay(ms: number): Promise<void> {
  const { promise, resolve } = Promise.withResolvers<void>();
  setTimeout(resolve, ms);
  return promise;
}

/** Wait until the core transport answers, or report what the process printed instead. */
async function waitForCore(url: string): Promise<void> {
  const deadline = Date.now() + 120_000;
  while (Date.now() < deadline) {
    if (proc?.exitCode !== null && proc?.exitCode !== undefined) {
      throw new Error(`core server exited with ${proc.exitCode}\n${bootLog}`);
    }
    try {
      const resp = await fetch(`${url}/api/commands`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (resp.ok) return;
    } catch {
      // not listening yet
    }
    await delay(250);
  }
  throw new Error(`core transport never answered at ${url}\n${bootLog}`);
}

test.beforeAll(async () => {
  const dist = path.join(rootDir, "frontend", "dist", "index.html");
  if (!existsSync(dist)) {
    throw new Error(
      `missing web bundle at ${dist}: run \`bun run build\` in frontend/ first`,
    );
  }
  // The core refuses a state directory under the workspace its file tools can write.
  const workspace = mkdtempSync(
    path.join(os.tmpdir(), "voidcode-core-e2e-ws-"),
  );
  const stateDir = mkdtempSync(
    path.join(os.tmpdir(), "voidcode-core-e2e-state-"),
  );
  writeFileSync(
    path.join(workspace, "voidcode.json"),
    JSON.stringify({ schema_version: 1 }),
  );
  const port = await freePort();
  baseUrl = `http://127.0.0.1:${port}`;

  proc = spawn(
    "uv",
    [
      "run",
      "python",
      "-X",
      "utf8",
      "-m",
      "voidcode.core",
      "--workspace",
      workspace,
      "--state-dir",
      stateDir,
      "serve",
      "--port",
      String(port),
      "--bearer-token",
      token,
    ],
    {
      cwd: rootDir,
      env: { ...process.env, PYTHONUNBUFFERED: "1" },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  proc.stdout?.on("data", (chunk) => {
    bootLog += chunk.toString();
  });
  proc.stderr?.on("data", (chunk) => {
    bootLog += chunk.toString();
  });
  await waitForCore(baseUrl);
});

test.afterAll(() => {
  proc?.kill("SIGTERM");
  proc = undefined;
});

test("the core client creates a session and renders a command result", async ({
  page,
}) => {
  await page.goto(`${baseUrl}/?token=${token}`);

  // The SPA the core serves is the core client, not the deleted legacy app.
  await expect(page.getByTestId("core-app")).toBeVisible();

  // Bootstrap creates the first session for an empty core; the id is user-visible.
  const sessionTabs = page.locator('[data-testid^="session-tab-"]');
  await expect(sessionTabs).toHaveCount(1);

  // A second session is created on explicit request.
  await page.getByTestId("new-session-btn").click();
  await expect(sessionTabs).toHaveCount(2);

  // A plugin command round trip: /session_status is admitted by the session_info
  // plugin, and its committed result is rendered in the transcript.
  await page.getByTestId("prompt-input").fill("/session_status");
  await page.getByTestId("send-btn").click();
  await expect(
    page
      .locator('[data-testid^="event-"]')
      .filter({ hasText: "/session_status [ok]" }),
  ).toBeVisible();
});
