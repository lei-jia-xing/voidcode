import { test, expect } from "@playwright/test";
import { spawn, execFileSync, type ChildProcess } from "child_process";
import { existsSync, mkdtempSync, writeFileSync, cpSync, rmSync } from "fs";
import { createServer } from "net";
import * as os from "os";
import * as path from "path";
import { fileURLToPath } from "url";

/**
 * Supervised live-handoff acceptance, offline: the real supervisor owns the
 * listener/token, the browser attaches to the served T8+ bundle, then a REAL
 * `upgrade run` (comment-marker candidate copy — never the live root) hands
 * off mid-attachment. Asserts the permanent client shape: stable endpoint +
 * token, restart resume exact (no duplicate settled rows, no prompt resend),
 * generation identity compare, catalog refetch. No provider: nothing reaches
 * a model. Requires `frontend/dist` (`mise run frontend:e2e` builds it).
 */

test.describe.configure({ timeout: 300_000 });

const rootDir = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../..",
);
const token = "kernel-handoff-token";

let proc: ChildProcess | undefined;
let baseUrl = "";
let bootLog = "";
let workspace = "";
let stateDir = "";

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

async function api(
  method: string,
  apiPath: string,
  body?: unknown,
): Promise<{ status: number; json: Record<string, unknown> }> {
  const resp = await fetch(`${baseUrl}${apiPath}`, {
    method,
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const json = (await resp.json()) as Record<string, unknown>;
  return { status: resp.status, json };
}

/** Wait until the supervised child reports serving, or show what it printed. */
async function waitForServing(): Promise<number> {
  const deadline = Date.now() + 120_000;
  while (Date.now() < deadline) {
    if (proc?.exitCode !== null && proc?.exitCode !== undefined) {
      throw new Error(`supervisor exited with ${proc.exitCode}\n${bootLog}`);
    }
    try {
      const { json } = await api("GET", "/api/health");
      if (json["stage"] === "serving") return json["generation"] as number;
    } catch {
      // not listening yet
    }
    await delay(250);
  }
  throw new Error(`supervised kernel never served at ${baseUrl}\n${bootLog}`);
}

test.beforeAll(async () => {
  const dist = path.join(rootDir, "frontend", "dist", "index.html");
  if (!existsSync(dist)) {
    throw new Error(
      `missing web bundle at ${dist}: run \`bun run build\` in frontend/ first`,
    );
  }
  workspace = mkdtempSync(path.join(os.tmpdir(), "voidcode-handoff-ws-"));
  stateDir = mkdtempSync(path.join(os.tmpdir(), "voidcode-handoff-state-"));
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
      "voidcode.kernel",
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
  await waitForServing();
});

test.afterAll(() => {
  proc?.kill("SIGTERM");
  proc = undefined;
});

test("a live upgrade keeps endpoint/token/session with exact resume", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (err) => errors.push(String(err)));
  await page.goto(`${baseUrl}/?token=${token}`);
  await expect(page.getByTestId("kernel-app")).toBeVisible();

  // Bootstrap creates the first session; record its id for the upgrade below.
  const tabs = page.locator('[data-testid^="session-tab-"]');
  await expect(tabs).toHaveCount(1);
  const tabId = (await tabs.first().getAttribute("data-testid")) as string;
  const sessionId = tabId.replace("session-tab-", "");

  // Settle one command before the handoff so the prefix is non-trivial.
  await page.getByTestId("prompt-input").fill("/session_status");
  await page.getByTestId("send-btn").click();
  const rows = page.locator('[data-testid^="event-"]');
  await expect(
    rows.filter({ hasText: "/session_status [ok]" }).last(),
  ).toBeVisible();
  const before = await rows.count();
  expect(before).toBeGreaterThan(0);
  const { json: healthBefore } = await api("GET", "/api/health");
  const genBefore = healthBefore["generation"] as number;

  // A comment-marker candidate copy: a genuinely different image digest, made
  // from the current tree, never the live root itself.
  const candidate = mkdtempSync(
    path.join(os.tmpdir(), "voidcode-handoff-cand-"),
  );
  cpSync(
    path.join(rootDir, "src", "voidcode"),
    path.join(candidate, "src", "voidcode"),
    {
      recursive: true,
      filter: (src) => !src.includes("__pycache__"),
    },
  );
  writeFileSync(
    path.join(candidate, "src", "voidcode", "kernel", "transport", "models.py"),
    "\n# handoff-e2e candidate marker\n",
    { flag: "a" },
  );
  let raw: string;
  try {
    raw = execFileSync(
      "uv",
      [
        "run",
        "python",
        "-X",
        "utf8",
        "-m",
        "voidcode.kernel",
        "--workspace",
        workspace,
        "--state-dir",
        stateDir,
        "upgrade",
        "run",
        "--candidate",
        candidate,
        "--session-id",
        sessionId,
      ],
      { cwd: rootDir, timeout: 240_000, encoding: "utf-8" },
    );
  } finally {
    rmSync(candidate, { recursive: true, force: true });
  }
  const result = JSON.parse(raw) as { status: string };
  expect(result.status).toBe("upgraded");

  // Generation identity compare: the SAME endpoint now serves a NEW child.
  const { json: healthAfter } = await api("GET", "/api/health");
  expect(healthAfter["stage"]).toBe("serving");
  expect(healthAfter["fenced"]).toBe(false);
  expect(healthAfter["generation"] as number).toBeGreaterThan(genBefore);

  // The attached UI reconciles without reload: the adoption row streams in
  // (proving the new generation arrived AND the catalog was refetched — only
  // then is dispatch safe), every settled testid stays unique, and a
  // post-upgrade command works.
  await expect(rows.filter({ hasText: "revision adopted" }).last()).toBeVisible(
    { timeout: 30000 },
  );
  const ids = await rows.evaluateAll((els) =>
    els.map((e) => e.getAttribute("data-testid")),
  );
  expect(new Set(ids).size).toBe(ids.length);

  await page.getByTestId("prompt-input").fill("/session_status");
  await page.getByTestId("send-btn").click();
  await expect(rows).toHaveCount(ids.length + 2, { timeout: 15000 });

  // No prompt resend: without a provider every /run fails loudly, and no
  // extra session or phantom row appeared — still exactly one session tab.
  await expect(tabs).toHaveCount(1);
  expect(errors).toEqual([]);
});
