import { test, expect, type Page } from "@playwright/test";
import { spawn, ChildProcess } from "child_process";
import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import { fileURLToPath } from "url";

let proc: ChildProcess;
let launcherUrl: string;

let stdoutData = "";
let stderrData = "";

type JsonValue =
  | null
  | boolean
  | number
  | string
  | JsonValue[]
  | { [key: string]: JsonValue };

function jsonResponse(payload: JsonValue) {
  return {
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(payload),
  };
}

function sseBody(chunks: JsonValue[]) {
  return chunks.map((chunk) => `data: ${JSON.stringify(chunk)}\n`).join("\n");
}

function uvExecutable() {
  return "uv";
}
async function installMockRuntime(page: Page) {
  const workspaceSnapshot = {
    current: {
      path: "/workspace",
      label: "workspace",
      available: true,
      current: true,
      last_opened_at: 1,
    },
    recent: [
      {
        path: "/workspace",
        label: "workspace",
        available: true,
        current: true,
        last_opened_at: 1,
      },
    ],
    candidates: [
      {
        path: path.join(os.tmpdir(), "voidcode-demo"),
        label: "voidcode-demo",
        available: true,
        current: false,
      },
    ],
  };
  const statusSnapshot = {
    git: { state: "git_ready", root: "/workspace", error: null },
    lsp: { state: "running", error: null, details: {} },
    mcp: {
      state: "stopped",
      error: null,
      details: {
        retry_available: true,
        servers: [{ server: "local", status: "stopped", stage: "idle" }],
      },
    },
    acp: {
      state: "running",
      error: null,
      details: { status: "connected", last_request_type: "handshake" },
    },
  };
  const reviewSnapshot = {
    root: "/workspace",
    git: { state: "git_ready", root: "/workspace", error: null },
    changed_files: [{ path: "src/app.ts", change_type: "modified" }],
    tree: [{ kind: "file", name: "app.ts", path: "src/app.ts", changed: true }],
  };
  const approvalSession = {
    session: { id: "browser-session" },
    status: "waiting",
    turn: 1,
    metadata: {},
  };
  const completedSession = {
    session: { id: "browser-session" },
    status: "completed",
    turn: 1,
    metadata: {},
  };
  const requestEvent = {
    session_id: "browser-session",
    sequence: 1,
    event_type: "runtime.request_received",
    source: "runtime",
    payload: { prompt: "browser QA tool surfaces" },
  };
  const readEvent = {
    session_id: "browser-session",
    sequence: 2,
    event_type: "runtime.tool_completed",
    source: "runtime",
    payload: {
      tool: "read_file",
      tool_call_id: "read-1",
      status: "ok",
      arguments: { path: "README.md" },
      tool_status: {
        invocation_id: "read-1",
        tool_name: "read_file",
        status: "completed",
        display: {
          kind: "context",
          title: "Read",
          summary: "Read README.md",
          args: ["README.md"],
        },
      },
    },
  };
  const grepEvent = {
    session_id: "browser-session",
    sequence: 3,
    event_type: "runtime.tool_completed",
    source: "runtime",
    payload: {
      tool: "grep",
      tool_call_id: "grep-1",
      status: "ok",
      arguments: { pattern: "TODO", path: "." },
      tool_status: {
        invocation_id: "grep-1",
        tool_name: "grep",
        status: "completed",
        display: {
          kind: "context",
          title: "Search",
          summary: "Search TODO",
          args: ["TODO", "."],
        },
      },
    },
  };
  const shellEvent = {
    session_id: "browser-session",
    sequence: 4,
    event_type: "runtime.tool_completed",
    source: "runtime",
    payload: {
      tool: "shell_exec",
      tool_call_id: "shell-1",
      status: "ok",
      arguments: { command: "bun run --cwd frontend lint" },
      data: {
        command: "bun run --cwd frontend lint",
        exit_code: 0,
        stdout: "lint passed",
        stderr: "",
      },
      tool_status: {
        invocation_id: "shell-1",
        tool_name: "shell_exec",
        status: "completed",
        display: {
          kind: "shell",
          title: "Shell",
          summary: "Run frontend lint",
          args: ["bun run --cwd frontend lint"],
          copyable: {
            command: "bun run --cwd frontend lint",
            output: "lint passed",
          },
        },
      },
    },
  };
  const approvalEvent = {
    session_id: "browser-session",
    sequence: 5,
    event_type: "runtime.approval_requested",
    source: "runtime",
    payload: {
      request_id: "approval-1",
      tool: "write_file",
      target_summary: "write note.txt",
    },
  };
  const responseEvent = {
    session_id: "browser-session",
    sequence: 6,
    event_type: "graph.response_ready",
    source: "graph",
    payload: { output: "Browser QA completed." },
  };

  await page.addInitScript(() => {
    if (!window.sessionStorage.getItem("__voidcode_e2e_storage_cleared")) {
      window.localStorage.clear();
      window.sessionStorage.setItem("__voidcode_e2e_storage_cleared", "true");
    }
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: {
        writeText: async (value: string) => {
          const target = window as unknown as { __copied?: string[] };
          if (!target.__copied) target.__copied = [];
          target.__copied.push(value);
        },
      },
    });
  });
  await page.route("**/api/workspaces", async (route) =>
    route.fulfill(jsonResponse(workspaceSnapshot)),
  );
  await page.route("**/api/workspaces/open", async (route) =>
    route.fulfill(jsonResponse(workspaceSnapshot)),
  );
  await page.route("**/api/sessions", async (route) =>
    route.fulfill(
      jsonResponse([
        {
          session: { id: "browser-session" },
          status: "waiting",
          turn: 1,
          prompt: "browser QA tool surfaces",
          updated_at: 1,
        },
      ]),
    ),
  );
  await page.route("**/api/sessions/browser-session", async (route) =>
    route.fulfill(
      jsonResponse({
        session: approvalSession,
        events: [requestEvent, readEvent, grepEvent, shellEvent, approvalEvent],
        output: null,
      }),
    ),
  );
  await page.route("**/api/sessions/browser-session/debug", async (route) =>
    route.fulfill(
      jsonResponse({
        session: approvalSession,
        prompt: "browser QA tool surfaces",
        persisted_status: "waiting",
        current_status: "waiting",
        active: false,
        resumable: true,
        replayable: true,
        terminal: false,
        pending_question: null,
        failure: null,
        last_tool: "shell_exec",
        suggested_operator_action: "approve",
        operator_guidance: "Resolve approval.",
      }),
    ),
  );
  await page.route("**/api/sessions/browser-session/tasks", async (route) =>
    route.fulfill(jsonResponse([])),
  );
  await page.route("**/api/providers", async (route) =>
    route.fulfill(
      jsonResponse([
        {
          name: "deepseek",
          label: "DeepSeek",
          configured: true,
          current: true,
        },
      ]),
    ),
  );
  await page.route("**/api/providers/deepseek/models", async (route) =>
    route.fulfill(
      jsonResponse({
        provider: "deepseek",
        configured: true,
        models: ["deepseek-v4-pro", "deepseek-v4-flash"],
        source: null,
        last_refresh_status: "ok",
        last_error: null,
        discovery_mode: "configured_endpoint",
        model_metadata: {
          "deepseek-v4-pro": {
            context_window: 1000000,
            max_output_tokens: 384000,
            supports_reasoning_effort: true,
            default_reasoning_effort: "medium",
          },
          "deepseek-v4-flash": {
            context_window: 1000000,
            max_output_tokens: 384000,
            supports_reasoning_effort: true,
            default_reasoning_effort: "medium",
          },
        },
      }),
    ),
  );
  await page.route("**/api/providers/deepseek/validate", async (route) =>
    route.fulfill(
      jsonResponse({
        provider: "deepseek",
        configured: true,
        ok: true,
        status: "ok",
        message: "Provider credentials valid.",
      }),
    ),
  );
  await page.route("**/api/agents", async (route) =>
    route.fulfill(
      jsonResponse([
        { id: "leader", label: "Leader", description: null, selectable: true },
      ]),
    ),
  );
  await page.route("**/api/status", async (route) =>
    route.fulfill(jsonResponse(statusSnapshot)),
  );
  await page.route("**/api/status/mcp/retry", async (route) =>
    route.fulfill(jsonResponse(statusSnapshot)),
  );
  await page.route("**/api/review", async (route) =>
    route.fulfill(jsonResponse(reviewSnapshot)),
  );
  await page.route("**/api/review/diff/src/app.ts", async (route) => {
    expect(route.request().url()).toContain("/api/review/diff/src/app.ts");
    expect(route.request().url()).not.toContain("src%2Fapp.ts");
    await route.fulfill(
      jsonResponse({
        root: "/workspace",
        path: "src/app.ts",
        state: "changed",
        diff: "--- a/src/app.ts\n+++ b/src/app.ts\n@@ -1 +1 @@\n-old\n+new",
      }),
    );
  });
  await page.route("**/api/settings", async (route) =>
    route.fulfill(
      jsonResponse({
        provider: "deepseek",
        model: "deepseek/deepseek-v4-pro",
        provider_api_key_present: true,
      }),
    ),
  );
  await page.route("**/api/notifications", async (route) =>
    route.fulfill(jsonResponse([])),
  );
  await page.route("**/api/tasks", async (route) =>
    route.fulfill(jsonResponse([])),
  );
  await page.route("**/api/runtime/run/stream", async (route) =>
    route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      body: sseBody([
        {
          kind: "event",
          session: approvalSession,
          event: requestEvent,
          output: null,
        },
        {
          kind: "event",
          session: approvalSession,
          event: readEvent,
          output: null,
        },
        {
          kind: "event",
          session: approvalSession,
          event: grepEvent,
          output: null,
        },
        {
          kind: "event",
          session: approvalSession,
          event: shellEvent,
          output: null,
        },
        {
          kind: "event",
          session: approvalSession,
          event: approvalEvent,
          output: null,
        },
      ]),
    }),
  );
  await page.route(
    "**/api/sessions/browser-session/approval",
    async (route) => {
      const body = route.request().postDataJSON() as {
        request_id?: string;
        decision?: string;
      };
      expect(body).toMatchObject({
        request_id: "approval-1",
        decision: "allow",
      });
      await route.fulfill(
        jsonResponse({
          session: completedSession,
          events: [
            requestEvent,
            readEvent,
            grepEvent,
            shellEvent,
            approvalEvent,
            responseEvent,
          ],
          output: "Browser QA completed.",
        }),
      );
    },
  );
}

test.describe("VoidCode Web Launcher", () => {
  test.beforeAll(async () => {
    const rootDir = path.resolve(
      path.dirname(fileURLToPath(import.meta.url)),
      "../..",
    );
    const port = Math.floor(Math.random() * 50000) + 10000;

    proc = spawn(
      uvExecutable(),
      [
        "run",
        "voidcode",
        "web",
        "--no-open",
        "--port",
        port.toString(),
        "--workspace",
        rootDir,
      ],
      {
        cwd: rootDir,
        env: { ...process.env, PYTHONUNBUFFERED: "1" },
      },
    );

    stdoutData = "";
    stderrData = "";
    launcherUrl = await new Promise<string>((resolve, reject) => {
      let foundUrl = "";
      let uvicornReady = false;

      proc.stdout?.on("data", (data) => {
        stdoutData += data.toString();
        if (!foundUrl) {
          const match = stdoutData.match(
            /Local server running at:\s+(http:\/\/[^\s]+)/,
          );
          if (match) {
            foundUrl = match[1];
            if (uvicornReady) resolve(foundUrl);
          }
        }
      });

      proc.stderr?.on("data", (data) => {
        stderrData += data.toString();
        if (!uvicornReady) {
          if (stderrData.includes("Uvicorn running on")) {
            uvicornReady = true;
            if (foundUrl) resolve(foundUrl);
          }
        }
      });

      proc.on("close", (code) => {
        if (!foundUrl || !uvicornReady)
          reject(
            new Error(
              `voidcode closed with code ${code}. Output: ${stdoutData} | Err: ${stderrData}`,
            ),
          );
      });
    });
  });

  test.afterAll(() => {
    if (proc) proc.kill();
  });

  test("should fallback to SPA on unknown routes", async ({ page }) => {
    const response = await page.goto(`${launcherUrl}/some-non-existent-route`);
    expect(response?.status()).toBe(200);
    await expect(page).toHaveTitle(/VoidCode/i);
    await expect(page.locator("textarea").first()).toBeVisible({
      timeout: 20000,
    });
  });

  test("should run a task using env-backed DeepSeek key without browser-side API key entry", async ({
    page,
  }) => {
    test.skip(
      !process.env.DEEPSEEK_API_KEY,
      "Skipping live smoke test because DEEPSEEK_API_KEY is not set in environment.",
    );

    await page.goto(launcherUrl);
    await expect(page).toHaveTitle(/VoidCode/i);

    const settingsModal = page.locator('div[role="dialog"]');
    await expect(settingsModal).not.toBeVisible();

    const input = page.locator("textarea");
    await expect(input).toBeVisible({ timeout: 20000 });
    await input.fill("read README.md");
    await input.press("Enter");

    await expect(page.getByText("Agent Busy")).toBeVisible({ timeout: 10000 });
    await expect(page.getByText("Agent Idle")).toBeVisible({ timeout: 60000 });

    await expect(page.getByText(/^Error:/)).not.toBeVisible();

    await expect(page.locator(".markdown-body").first()).toBeVisible();
  });

  test("should answer a runtime question from the browser", async ({
    page,
  }) => {
    await page.route("**/api/runtime/run/stream", async (route) => {
      const session = {
        session: { id: "question-session" },
        status: "waiting",
        turn: 1,
        metadata: {},
      };
      const requestEvent = {
        session_id: "question-session",
        sequence: 1,
        event_type: "runtime.request_received",
        source: "runtime",
        payload: { prompt: "ask for direction" },
      };
      const questionEvent = {
        session_id: "question-session",
        sequence: 2,
        event_type: "runtime.question_requested",
        source: "runtime",
        payload: {
          request_id: "question-1",
          tool: "question",
          question_count: 1,
          questions: [
            {
              header: "Direction",
              question: "Which path should VoidCode take?",
              multiple: false,
              options: [
                { label: "simple", description: "Use the simple path" },
                { label: "detailed", description: "Use the detailed path" },
              ],
            },
          ],
        },
      };
      await route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: [
          `data: ${JSON.stringify({ kind: "event", session, event: requestEvent, output: null })}`,
          "",
          `data: ${JSON.stringify({ kind: "event", session, event: questionEvent, output: null })}`,
          "",
        ].join("\n"),
      });
    });

    await page.route(
      "**/api/sessions/question-session/question",
      async (route) => {
        const body = route.request().postDataJSON() as { request_id?: string };
        expect(body.request_id).toBe("question-1");
        expect(body).toMatchObject({
          responses: [{ header: "Direction", answers: ["simple"] }],
        });
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            session: {
              session: { id: "question-session" },
              status: "completed",
              turn: 1,
              metadata: {},
            },
            events: [
              {
                session_id: "question-session",
                sequence: 1,
                event_type: "runtime.request_received",
                source: "runtime",
                payload: { prompt: "ask for direction" },
              },
              {
                session_id: "question-session",
                sequence: 2,
                event_type: "runtime.question_answered",
                source: "runtime",
                payload: { request_id: "question-1" },
              },
              {
                session_id: "question-session",
                sequence: 3,
                event_type: "graph.response_ready",
                source: "graph",
                payload: { output: "question answered" },
              },
            ],
            output: "question answered",
          }),
        });
      },
    );

    await page.route("**/api/sessions", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([
          {
            session: { id: "question-session" },
            status: "waiting",
            turn: 1,
            prompt: "ask for direction",
            updated_at: 1,
          },
        ]),
      });
    });

    await page.route(
      "**/api/sessions/question-session/debug",
      async (route) => {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            session: {
              session: { id: "question-session" },
              status: "waiting",
              turn: 1,
              metadata: {},
            },
            prompt: "ask for direction",
            persisted_status: "waiting",
            current_status: "waiting",
            active: false,
            resumable: true,
            replayable: true,
            terminal: false,
            pending_question: {
              request_id: "question-1",
              tool_name: "question",
              question_count: 1,
              headers: ["Direction"],
            },
            failure: null,
            last_tool: null,
            suggested_operator_action: "answer_question",
            operator_guidance: "Answer the pending question.",
          }),
        });
      },
    );

    await page.route(
      "**/api/sessions/question-session/tasks",
      async (route) => {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: "[]",
        });
      },
    );

    await page.goto(launcherUrl);
    const input = page.locator("textarea");
    await expect(input).toBeVisible({ timeout: 20000 });
    await input.fill("ask for direction");
    await input.press("Enter");

    await expect(page.getByText("Question Required")).toBeVisible({
      timeout: 10000,
    });
    await page.getByLabel("simple").check();
    await page.getByRole("button", { name: "Submit Answer" }).click();

    await expect(page.getByText("question answered")).toBeVisible({
      timeout: 10000,
    });
  });

  test("should cover browser UI controls and monochrome tool layout", async ({
    page,
  }) => {
    test.setTimeout(60000);

    await installMockRuntime(page);
    const rootDir = path.resolve(
      path.dirname(fileURLToPath(import.meta.url)),
      "../..",
    );
    const evidenceDir = path.join(rootDir, ".sisyphus/evidence");
    fs.mkdirSync(evidenceDir, { recursive: true });

    await page.goto(launcherUrl);
    await expect(page.getByRole("button", { name: "Model" })).toHaveText(
      /DeepSeek/,
    );
    await expect(page.getByText("workspace", { exact: true })).toBeVisible();

    const resizeHandle = page.getByRole("separator", {
      name: "Resize session sidebar",
    });
    await resizeHandle.focus();
    await page.keyboard.press("End");
    await expect(page.locator("aside").first()).toHaveCSS(
      "--session-sidebar-width",
      "448px",
    );
    await page.reload();
    await expect(page.locator("aside").first()).toHaveCSS(
      "--session-sidebar-width",
      "448px",
    );

    await page.getByRole("button", { name: "Toggle file tree" }).click();
    await expect(
      page.getByRole("complementary", { name: "File Tree" }),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Toggle file tree" }),
    ).toHaveAttribute("aria-expanded", "true");
    await expect(page.getByRole("button", { name: "Changes" })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Files" })).toHaveCount(0);
    await page.getByRole("button", { name: "app.ts" }).click();
    await expect(
      page.getByRole("button", { name: "Toggle file tree" }),
    ).toHaveAttribute("aria-expanded", "true");
    await expect(
      page.getByRole("button", { name: "Toggle code review" }),
    ).toHaveAttribute("aria-expanded", "true");
    await expect(
      page.getByRole("complementary", { name: "Code Review" }),
    ).toBeVisible();
    await expect(page.getByText("--- a/src/app.ts")).toBeVisible();

    await page.getByRole("button", { name: "Toggle code review" }).click();
    await expect(
      page.getByRole("button", { name: "Toggle code review" }),
    ).toHaveAttribute("aria-expanded", "false");
    await expect(
      page.getByRole("button", { name: "Toggle file tree" }),
    ).toHaveAttribute("aria-expanded", "true");
    await page.getByRole("button", { name: "Toggle code review" }).click();
    await expect(
      page.getByRole("button", { name: "Toggle code review" }),
    ).toHaveAttribute("aria-expanded", "true");
    await page.getByRole("button", { name: "Refresh" }).first().click();
    await expect(
      page.getByRole("button", { name: "M src/app.ts" }),
    ).toBeVisible();
    await page.screenshot({
      path: path.join(evidenceDir, "task-11-layout-buttons.png"),
      fullPage: true,
    });
    await page.getByRole("button", { name: "Close code review panel" }).click();
    await page.getByRole("button", { name: "Close file tree panel" }).click();

    const input = page.locator("textarea");
    await input.fill("browser QA tool surfaces");
    await input.press("Enter");

    await expect(
      page.getByRole("button", { name: /show details for project lookups/i }),
    ).toBeVisible({ timeout: 10000 });
    await expect(page.getByText("2 lookups")).toBeVisible();
    await page
      .getByRole("button", { name: /show details for project lookups/i })
      .click();
    await expect(page.getByText("Read README.md")).toBeVisible();
    await expect(page.getByText("Search TODO")).toBeVisible();

    const shellToggle = page.getByRole("button", {
      name: /show details for shell/i,
    });
    await expect(shellToggle).toBeVisible();
    await expect(page.getByText("Run frontend lint")).toBeVisible();
    await expect(page.getByText("lint passed")).not.toBeVisible();
    await expect(shellToggle).not.toHaveClass(/border/);
    await shellToggle.click();
    await expect(page.locator('[data-terminal-block="shell"]')).toHaveCount(1);
    await expect(page.getByText("$ bun run --cwd frontend lint")).toBeVisible();
    await expect(page.getByText("lint passed")).toBeVisible();
    await page.getByRole("button", { name: /copy command/i }).click();
    await expect
      .poll(() =>
        page.evaluate(
          () => (window as unknown as { __copied?: string[] }).__copied ?? [],
        ),
      )
      .toContain("bun run --cwd frontend lint");
    await page.screenshot({
      path: path.join(evidenceDir, "task-11-tool-browser.png"),
      fullPage: true,
    });

    await expect(page.getByText("Approval Required")).toBeVisible();
    await page.getByRole("button", { name: "Allow" }).click();
    await expect(page.getByText("Browser QA completed.")).toBeVisible();
    await expect(
      page
        .getByText("Browser QA completed.")
        .locator('xpath=ancestor::*[contains(@class, "markdown-body")][1]'),
    ).toHaveCount(1);

    const statusToggle = page.getByRole("button", {
      name: "Toggle runtime status",
    });
    await statusToggle.click();
    const statusDetailId = await statusToggle.getAttribute("aria-controls");
    if (!statusDetailId)
      throw new Error("Runtime status toggle should control a detail popover");
    const statusDetail = page.locator(`[id="${statusDetailId}"]`);
    await expect(
      statusDetail.getByText("Server", { exact: true }),
    ).toBeVisible();
    await expect(statusDetail.getByText("LSP", { exact: true })).toBeVisible();
    await expect(statusDetail.getByText("MCP", { exact: true })).toBeVisible();
    await expect(statusDetail.getByText(/transport: connected/)).toBeVisible();
    await expect(
      statusDetail.getByText(/last request: handshake/),
    ).toBeVisible();
    await page.getByRole("button", { name: "Retry MCP" }).click();

    await page.getByRole("button", { name: "Settings" }).click();
    await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();
    await expect(page.getByText("Configured providers")).toBeVisible();
    await page.locator("form").getByRole("button", { name: "Close" }).click();

    await page.getByRole("button", { name: "Open Project" }).first().click();
    await expect(
      page.getByRole("heading", { name: "Open Project" }),
    ).toBeVisible();
    await page
      .getByPlaceholder("Search recent or nearby projects")
      .fill("demo");
    await expect(
      page.getByText("voidcode-demo", { exact: true }),
    ).toBeVisible();
    await page.getByRole("button", { name: "Close" }).nth(1).click();

    await page.keyboard.press("Tab");
    await expect(page.locator(":focus")).toBeVisible();
  });

  test("should show the project-first empty state in the launcher shell", async ({
    page,
  }) => {
    await page.addInitScript(() => window.localStorage.clear());
    await page.route("**/api/workspaces", async (route) =>
      route.fulfill(
        jsonResponse({ current: null, recent: [], candidates: [] }),
      ),
    );
    await page.route("**/api/providers", async (route) =>
      route.fulfill(jsonResponse([])),
    );
    await page.route("**/api/agents", async (route) =>
      route.fulfill(jsonResponse([])),
    );
    await page.route("**/api/status", async (route) =>
      route.fulfill(
        jsonResponse({
          git: { state: "not_git_repo", root: null, error: null },
          lsp: { state: "stopped", error: null, details: {} },
          mcp: { state: "stopped", error: null, details: {} },
          acp: { state: "stopped", error: null, details: {} },
        }),
      ),
    );
    await page.route("**/api/review", async (route) =>
      route.fulfill(
        jsonResponse({
          root: null,
          git: { state: "not_git_repo", root: null, error: null },
          changed_files: [],
          tree: [],
        }),
      ),
    );
    await page.route("**/api/settings", async (route) =>
      route.fulfill(
        jsonResponse({
          provider: null,
          model: null,
          provider_api_key_present: false,
        }),
      ),
    );
    await page.route("**/api/sessions", async (route) =>
      route.fulfill(jsonResponse([])),
    );
    await page.route("**/api/notifications", async (route) =>
      route.fulfill(jsonResponse([])),
    );
    await page.route("**/api/tasks", async (route) =>
      route.fulfill(jsonResponse([])),
    );

    await page.goto(launcherUrl);
    await expect(page.getByText("Open a project to get started")).toBeVisible();
    await expect(
      page.getByText(
        "Choose a workspace first before using chat, review, or composer.",
      ),
    ).toBeVisible();
    await expect(page.locator("textarea")).toHaveCount(0);
  });
});
