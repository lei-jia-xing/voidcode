import { test, expect } from "@playwright/test";
import { spawn, type ChildProcess } from "child_process";
import { mkdtempSync, writeFileSync } from "fs";
import { createServer } from "net";
import * as os from "os";
import * as path from "path";
import { fileURLToPath } from "url";
import { gotoAuthed } from "./auth";

/** Restart the genuine core while the browser is attached; no model or live network. */
test.setTimeout(120000);
const root = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../..",
);
const token = "rpc-reconnect-test-token";

async function port(): Promise<number> {
  return new Promise((resolve, reject) => {
    const server = createServer();
    server.on("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      if (!address || typeof address === "string") {
        reject(new Error("No listening port"));
        return;
      }
      server.close(() => resolve(address.port));
    });
  });
}

async function stop(process: ChildProcess): Promise<void> {
  if (process.exitCode !== null || process.signalCode !== null) return;
  await new Promise<void>((resolve, reject) => {
    const timer = setTimeout(() => {
      process.kill("SIGKILL");
      reject(new Error("Core failed to stop"));
    }, 15000);
    process.once("exit", () => {
      clearTimeout(timer);
      resolve();
    });
    process.kill("SIGTERM");
  });
}

test("RPC reconnect restores events once and accepts a new command", async ({
  page,
}) => {
  const workspace = mkdtempSync(path.join(os.tmpdir(), "voidcode-rpc-ws-"));
  const state = mkdtempSync(path.join(os.tmpdir(), "voidcode-rpc-state-"));
  writeFileSync(
    path.join(workspace, "voidcode.toml"),
    "schema_version = 1\n",
  );
  const selectedPort = await port();
  const base = `http://127.0.0.1:${selectedPort}`;
  let logs = "";
  const launch = async (): Promise<ChildProcess> => {
    const process = spawn(
      path.join(root, ".venv/bin/python"),
      [
        "-m",
        "voidcode",
        "--workspace",
        workspace,
        "--state-dir",
        state,
        "serve",
        "--port",
        String(selectedPort),
        "--bearer-token",
        token,
      ],
      { cwd: root, stdio: ["ignore", "pipe", "pipe"] },
    );
    process.stdout?.on("data", (chunk) => {
      logs += String(chunk);
    });
    process.stderr?.on("data", (chunk) => {
      logs += String(chunk);
    });
    const deadline = Date.now() + 60000;
    let lastError = "";
    while (Date.now() < deadline) {
      if (process.exitCode !== null) throw new Error(logs);
      try {
        const response = await fetch(`${base}/api/health`, {
          headers: { Authorization: `Bearer ${token}` },
        });
        if (response.ok) return process;
        lastError = `${response.status}: ${await response.text()}`;
      } catch (error) {
        lastError = String(error);
      }
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
    await stop(process);
    throw new Error(`Core failed to start: ${lastError} ${logs}`);
  };
  let replayHeads = 0;
  page.on("websocket", (socket) => {
    socket.on("framereceived", (event) => {
      const message = JSON.parse(String(event.payload));
      if (
        message.method === "session.frame" &&
        Array.isArray(message.params?.frame?.events)
      )
        replayHeads++;
    });
  });
  let process = await launch();
  try {
    await gotoAuthed(page, base, token);
    const commands = page
      .locator('[data-testid^="event-"]')
      .filter({ hasText: "/plan status [ok]" });
    await expect(page.locator('[data-testid^="session-tab-"]')).toHaveCount(1);
    await page.getByTestId("prompt-input").fill("/plan status");
    await page.getByTestId("send-btn").click();
    await expect(commands).toHaveCount(1);
    const firstId = await commands.first().getAttribute("data-testid");
    const beforeRestart = replayHeads;
    await stop(process);
    process = await launch();
    // Observe a replay head on the existing page, not a reload or a second client.
    await expect
      .poll(() => replayHeads, { timeout: 15000 })
      .toBeGreaterThan(beforeRestart);
    await page.getByTestId("prompt-input").fill("/plan status");
    await page.getByTestId("send-btn").click();
    await expect(commands).toHaveCount(2, { timeout: 15000 });
    expect(firstId).not.toBeNull();
    await expect(page.getByTestId(firstId!)).toHaveCount(1);
  } finally {
    await stop(process);
  }
});
