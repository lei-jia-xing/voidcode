import { test, expect } from "@playwright/test";
import { spawn, execFileSync, type ChildProcess } from "child_process";
import { mkdtempSync, mkdirSync, readFileSync, writeFileSync } from "fs";
import { createServer } from "net";
import * as os from "os";
import * as path from "path";
import { fileURLToPath } from "url";

test.setTimeout(90000);
const root = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../..",
);
const python = path.join(
  root,
  ".venv",
  process.platform === "win32" ? "Scripts/python.exe" : "bin/python",
);
const token = "command-browsers-test-token";
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

test.beforeAll(async () => {
  workspace = mkdtempSync(
    path.join(os.tmpdir(), "voidcode-command-browser-ws-"),
  );
  const state = mkdtempSync(
    path.join(os.tmpdir(), "voidcode-command-browser-state-"),
  );
  const env = {
    ...process.env,
    PYTHONPATH: path.join(root, "src"),
    PYTHONUNBUFFERED: "1",
  };
  const plugins = JSON.parse(
    execFileSync(
      python,
      [
        "-c",
        "import json; from voidcode.bootstrap import bundled_sources; print(json.dumps({name: {'enabled': False} for name in bundled_sources()}))",
      ],
      { cwd: root, env },
    ).toString(),
  );
  plugins.todo = { enabled: true };
  plugins.skills = { enabled: true, config: { enabled: true } };
  plugins.delegation = { enabled: true };
  for (const name of [
    "cron",
    "review",
    "goal",
    "session_info",
    "effectiveness",
    "mcp",
    "lsp",
  ])
    plugins[name] = { enabled: true };
  writeFileSync(
    path.join(workspace, "voidcode.json"),
    JSON.stringify({ schema_version: 1, plugins }),
  );
  const skill = path.join(workspace, ".voidcode/skills/guide");
  mkdirSync(skill, { recursive: true });
  writeFileSync(
    path.join(skill, "SKILL.md"),
    "---\nname: guide\ndescription: Browser guide\n---\n\n" +
      "Read the workspace carefully.\n".repeat(400),
  );
  base = `http://127.0.0.1:${await freePort()}`;
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

test("keyboard discovery, todo editing, passive replay and paged skills work in the real browser", async ({
  page,
}, info) => {
  const calls: Array<{
    method: string;
    params: { name?: string; arguments?: Record<string, unknown> };
  }> = [];
  page.on("websocket", (socket) =>
    socket.on("framesent", (event) => {
      const message = JSON.parse(String(event.payload));
      if (message.method) calls.push(message);
    }),
  );
  await page.goto(`${base}/?token=${token}`);
  await expect(page.locator('[data-testid^="session-tab-"]')).toHaveCount(1);
  const input = page.getByTestId("prompt-input");
  await input.fill("/ag");
  await expect(
    page.getByTestId("slash-suggestions").getByRole("button"),
  ).toHaveCount(1);
  await expect(page.getByTestId("slash-suggestions")).not.toContainText(
    "/agents show",
  );
  await input.press("Tab");
  await expect(input).toHaveValue("/agents ");
  await expect(page.getByTestId("slash-suggestions")).toContainText(
    "/agents show",
  );
  await input.press("Escape");
  await expect(page.getByTestId("slash-suggestions")).toHaveCount(0);
  await expect(input).toHaveValue("/agents ");
  await input.press("Backspace");
  await input.press("Enter");
  await page.getByTestId("choice-option-explore").click();
  await expect(page.getByTestId("command-presentation")).toContainText(
    "Agent: explore",
  );
  await input.fill("/todo");
  await input.press("Enter");
  await page
    .getByTestId("command-presentation")
    .getByRole("button", { name: "Add tasks", exact: true })
    .click();
  await expect(page.getByTestId("form-input-items")).toBeVisible();
  expect(
    calls.filter(
      (call) =>
        call.method === "command.invoke" && call.params.name === "todo.add",
    ),
  ).toHaveLength(0);
  await expect(page.getByTestId("form-input-expected_revision")).toHaveCount(0);
  await page.getByTestId("form-input-items").fill("Web first\nWeb second");
  await page
    .getByTestId("command-presentation")
    .getByTestId("form-submit-btn")
    .click();
  const receipt = page
    .locator('[data-testid^="event-"]')
    .filter({ hasText: "/todo add [ok]" });
  await expect.poll(() => receipt.innerText()).toContain("applied.\n");
  await page
    .getByTestId("command-presentation")
    .getByRole("button", { name: "Edit: Web first", exact: true })
    .click();
  await page.getByTestId("form-input-op").selectOption("done");
  await page
    .getByTestId("command-presentation")
    .getByTestId("form-submit-btn")
    .click();
  await expect(page.getByTestId("command-presentation")).toContainText(
    "[completed] Web first",
  );
  await expect(page.getByTestId("command-presentation")).toContainText(
    "[in_progress] Web second",
  );
  await page.screenshot({ path: info.outputPath("todo-editor.png") });
  await page.reload();
  await expect(page.locator('[data-testid^="session-tab-"]')).toHaveCount(1);
  await expect(
    page
      .locator('[data-testid^="event-"]')
      .filter({ hasText: "Web first" })
      .first(),
  ).toBeVisible();
  await expect(page.getByTestId("command-presentation")).toHaveCount(0);
  await input.fill("/skills");
  await input.press("Enter");
  await page.getByTestId("choice-option-guide").click();
  await page
    .getByTestId("command-presentation")
    .getByRole("button", { name: "Next page", exact: true })
    .click();
  await expect(page.getByTestId("command-presentation")).toContainText(
    "characters 4000",
  );
  expect(
    calls
      .filter(
        (call) =>
          call.method === "command.invoke" &&
          call.params.name === "skills.show",
      )
      .at(-1)?.params.arguments?.offset,
  ).toBe(4000);
  await input.fill("/todo ");
  await expect(page.getByTestId("slash-suggestions")).not.toContainText(
    "todo update",
  );
  await input.press("ArrowDown");
  await input.press("ArrowUp");
  await input.press("Escape");
  await expect(input).toHaveValue("/todo ");
  await input.fill("/todo e");
  await input.press("Home");
  await input.press("Tab");
  await expect(input).toHaveValue("/todo e");
  const configPath = path.join(workspace, "voidcode.json");
  const config = JSON.parse(readFileSync(configPath, "utf8"));
  config.plugins.skills.enabled = false;
  writeFileSync(configPath, JSON.stringify(config));
  await input.fill("/core reload");
  await input.press("Enter");
  await expect(
    page
      .locator('[data-testid^="event-"]')
      .filter({ hasText: "Core revision adopted:" }),
  ).toHaveCount(1);
  await input.fill("/sk");
  await expect(page.getByTestId("slash-suggestions")).toHaveCount(0);
  await expect(page.getByTestId("command-presentation")).toHaveCount(0);
  const previousSkillCalls = calls.filter(
    (call) =>
      call.method === "command.invoke" &&
      call.params.name?.startsWith("skills"),
  ).length;
  await input.fill("/skills");
  await input.press("Enter");
  await expect(
    page.getByText("Unknown command: /skills", { exact: true }),
  ).toBeVisible();
  expect(
    calls.filter(
      (call) =>
        call.method === "command.invoke" &&
        call.params.name?.startsWith("skills"),
    ),
  ).toHaveLength(previousSkillCalls);
});

test("guided review, schedule cancellation and diagnostic panels work without a provider", async ({
  page,
}) => {
  const calls: Array<{ method: string; params: { name?: string } }> = [];
  page.on("websocket", (socket) =>
    socket.on("framesent", (event) => {
      const message = JSON.parse(String(event.payload));
      if (message.method) calls.push(message);
    }),
  );
  await page.goto(`${base}/?token=${token}`);
  const input = page.getByTestId("prompt-input");
  const panel = page.getByTestId("command-presentation");
  async function command(text: string) {
    await input.fill(text);
    await input.press("Enter");
  }
  await expect(page.locator('[data-testid^="session-tab-"]')).toHaveCount(1);
  await input.fill("/review");
  await expect(page.getByTestId("slash-suggestions")).toContainText("/review");
  await command("/review");
  await expect(page.getByTestId("form-input-target")).toHaveValue(
    "uncommitted",
  );
  expect(
    calls.filter(
      (call) =>
        call.method === "command.invoke" && call.params.name === "review",
    ),
  ).toHaveLength(0);
  await page.getByTestId("form-input-target").selectOption("base_branch");
  await page.getByTestId("form-input-focus").fill("Check error handling");
  await page
    .getByTestId("command-presentation")
    .getByTestId("form-submit-btn")
    .click();
  await expect(page.locator("body")).toContainText("requires a branch name");
  await expect(page.getByTestId("form-input-focus")).toHaveValue(
    "Check error handling",
  );
  await expect(page.getByTestId("form-input-target")).toHaveValue(
    "base_branch",
  );
  await command("/loop");
  await page.getByTestId("form-input-input").fill("1h Browser schedule");
  await page
    .getByTestId("command-presentation")
    .getByTestId("form-submit-btn")
    .click();
  await panel
    .getByRole("button", { name: "Browse schedules", exact: true })
    .click();
  await expect(panel).toContainText("Browser schedule");
  await panel
    .getByRole("button", { name: "Cancel: Browser schedule", exact: true })
    .click();
  await expect(panel).toContainText("No scheduled prompts");
  await expect(
    panel.getByRole("button", { name: "Create repeating prompt", exact: true }),
  ).toBeVisible();
  const adoptions = page
    .locator('[data-testid^="event-"]')
    .filter({ hasText: "Core revision adopted:" });
  const previousAdoptions = await adoptions.count();
  await command("/core reload");
  await expect(adoptions).toHaveCount(previousAdoptions + 1);
  await command("/usage");
  await expect(panel.getByTestId("fields-view")).toContainText(
    "Usage coverage",
  );
  await expect(panel).toContainText("Active budget");
  await command("/context");
  await expect(panel).toContainText("No committed provider dispatch");
  await command("/effectiveness");
  await expect(panel).toContainText("No persisted tool calls");
  await panel
    .getByRole("button", { name: "Refresh metrics", exact: true })
    .click();
  await expect(panel).toContainText("Session effectiveness");
  await command("/mcp");
  await expect(panel).toContainText("No MCP servers are configured");
  await expect(panel).toContainText("/core reload");
  await command("/lsp");
  await expect(panel).toContainText("configuration");
  await command("/goal");
  await expect(panel.getByTestId("form-input-objective")).toBeVisible();
  expect(
    calls.filter(
      (call) =>
        call.method === "command.invoke" && call.params.name === "goal.set",
    ),
  ).toHaveLength(0);
  await page.reload();
  await expect(page.locator('[data-testid^="session-tab-"]')).toHaveCount(1);
  await expect(panel).toHaveCount(0);
});
