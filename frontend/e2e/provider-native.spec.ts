import { expect, test } from "@playwright/test";
import type { RuntimeSessionResult, StoredSessionSummary } from "../src/lib/runtime/types";
import { createHash } from "crypto";
import { spawn, type ChildProcess } from "child_process";
import { createServer, type IncomingMessage, type Server, type ServerResponse } from "http";
import { mkdtemp, mkdir, readFile, writeFile } from "fs/promises";
import * as os from "os";
import path from "path";
import { fileURLToPath } from "url";

interface ProviderReply {
  message: Record<string, unknown>;
  finish: string;
}

const originalContent = "PROVIDER_NATIVE_BROWSER_READ\n";
const updatedContent = "PROVIDER_NATIVE_BROWSER_WRITE\n";
const completedPrompt =
  "Read sample.txt, replace it with PROVIDER_NATIVE_BROWSER_WRITE, then confirm the change.";
const failedPrompt = "Summarize sample.txt.";
const reasoning = "Inspect sample.txt before summarizing it.";
const expectedHash = createHash("sha256").update(originalContent).digest("hex");
const replySequence: ProviderReply[] = [
  {
    message: {
      role: "assistant",
      content: null,
      reasoning_content: reasoning,
      tool_calls: [
        {
          index: 0,
          id: "call-browser-read",
          type: "function",
          function: {
            name: "read",
            arguments: JSON.stringify({ path: "sample.txt" }),
          },
        },
      ],
    },
    finish: "tool_calls",
  },
  {
    message: {
      role: "assistant",
      content: null,
      reasoning_content: reasoning,
      tool_calls: [
        {
          index: 0,
          id: "call-browser-write",
          type: "function",
          function: {
            name: "write",
            arguments: JSON.stringify({
              path: "sample.txt",
              content: updatedContent,
              expectedHash,
            }),
          },
        },
      ],
    },
    finish: "tool_calls",
  },
  {
    message: {
      role: "assistant",
      content: null,
      reasoning_content: reasoning,
      tool_calls: [
        {
          index: 0,
          id: "call-browser-read",
          type: "function",
          function: {
            name: "read",
            arguments: JSON.stringify({ path: "sample.txt" }),
          },
        },
      ],
    },
    finish: "tool_calls",
  },
  {
    message: {
      role: "assistant",
      content: "Updated sample.txt after approval.",
    },
    finish: "stop",
  },
];
let replies: ProviderReply[] = [...replySequence];

let runtime: ChildProcess;
let runtimeUrl: string;
let provider: Server;
let workspace: string;
const providerRequests: Record<string, unknown>[] = [];
let browserErrors: string[] = [];

function completion(reply: ProviderReply) {
  return {
    id: "chatcmpl-browser-loopback",
    object: "chat.completion",
    created: 1,
    model: "deepseek-v4-pro",
    choices: [
      {
        index: 0,
        message: reply.message,
        finish_reason: reply.finish,
      },
    ],
  };
}

function respond(request: IncomingMessage, response: ServerResponse) {
  const chunks: Buffer[] = [];
  request.on("data", (chunk: Buffer) => chunks.push(chunk));
  request.on("end", () => {
    if (request.url !== "/v1/chat/completions") {
      response.writeHead(404).end();
      return;
    }
    const payload = JSON.parse(Buffer.concat(chunks).toString("utf8")) as Record<
      string,
      unknown
    >;
    providerRequests.push(payload);
    const reply = replies.shift();
    if (!reply) {
      response.writeHead(500).end("unexpected provider request");
      return;
    }
    if (payload.stream === true) {
      const value = completion(reply);
      const choice = value.choices[0];
      response.writeHead(200, {
        "Cache-Control": "no-cache",
        Connection: "keep-alive",
        "Content-Type": "text/event-stream",
      });
      response.end(
        `data: ${JSON.stringify({ ...value, choices: [{ ...choice, message: undefined, delta: reply.message, finish_reason: null }] })}\n\n` +
          `data: ${JSON.stringify({ ...value, choices: [{ index: 0, delta: {}, finish_reason: reply.finish }] })}\n\n` +
          "data: [DONE]\n\n",
      );
      return;
    }
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(JSON.stringify(completion(reply)));
  });
}

test.describe("provider-native browser runtime", () => {
  test.beforeEach(() => {
    replies = [...replySequence];
    providerRequests.length = 0;
  });

  test.beforeAll(async () => {
    test.setTimeout(120_000);
    const root = path.resolve(
      path.dirname(fileURLToPath(import.meta.url)),
      "../..",
    );
    const temporaryRoot = await mkdtemp(
      path.join(os.tmpdir(), "voidcode-browser-provider-"),
    );
    workspace = path.join(temporaryRoot, "workspace");
    const home = path.join(temporaryRoot, "home");
    const configHome = path.join(temporaryRoot, "config");
    const cacheHome = path.join(temporaryRoot, "cache");
    const dataHome = path.join(temporaryRoot, "data");
    const stateHome = path.join(temporaryRoot, "state");
    await Promise.all(
      [workspace, home, configHome, cacheHome, dataHome, stateHome].map(
        (directory) => mkdir(directory, { recursive: true }),
      ),
    );
    await writeFile(path.join(workspace, "sample.txt"), originalContent, "utf8");

    provider = createServer(respond);
    await new Promise<void>((resolve, reject) => {
      provider.once("error", reject);
      provider.listen(0, "127.0.0.1", resolve);
    });
    const address = provider.address();
    if (!address || typeof address === "string") {
      throw new Error("loopback provider did not bind a TCP port");
    }
    await writeFile(
      path.join(workspace, ".voidcode.json"),
      JSON.stringify({
        approval_mode: "ask",
        model: "deepseek/deepseek-v4-pro",
        providers: {
          deepseek: {
            base_url: `http://127.0.0.1:${address.port}/v1`,
            timeout_seconds: 5,
          },
        },
      }),
      "utf8",
    );

    const port = Math.floor(Math.random() * 50_000) + 10_000;
    runtime = spawn(
      "uv",
      [
        "run",
        "voidcode",
        "web",
        "--no-open",
        "--port",
        port.toString(),
        "--workspace",
        workspace,
      ],
      {
        cwd: root,
        env: {
          ...process.env,
          DEEPSEEK_API_KEY: "loopback-key",
          HOME: home,
          LOCALAPPDATA: dataHome,
          PYTHONUNBUFFERED: "1",
          USERPROFILE: home,
          VOIDCODE_DB_PATH: path.join(temporaryRoot, "voidcode.sqlite3"),
          XDG_CACHE_HOME: cacheHome,
          XDG_CONFIG_HOME: configHome,
          XDG_DATA_HOME: dataHome,
          XDG_STATE_HOME: stateHome,
        },
      },
    );
    let stdout = "";
    let stderr = "";
    runtimeUrl = await new Promise<string>((resolve, reject) => {
      let localUrl = "";
      let uvicornReady = false;
      const timeout = setTimeout(
        () => reject(new Error(`web server did not start: ${stdout}\n${stderr}`)),
        90_000,
      );
      const maybeReady = () => {
        if (localUrl && uvicornReady) {
          clearTimeout(timeout);
          resolve(localUrl);
        }
      };
      runtime.stdout?.on("data", (chunk: Buffer) => {
        stdout += chunk.toString();
        const match = stdout.match(/Local server running at:\s+(http:\/\/[^\s]+)/);
        if (match) localUrl = match[1];
        maybeReady();
      });
      runtime.stderr?.on("data", (chunk: Buffer) => {
        stderr += chunk.toString();
        if (stderr.includes("Uvicorn running on")) uvicornReady = true;
        maybeReady();
      });
      runtime.once("error", (error) => {
        clearTimeout(timeout);
        reject(error);
      });
      runtime.once("close", (code) => {
        if (!localUrl || !uvicornReady) {
          clearTimeout(timeout);
          reject(new Error(`web server exited with ${code}: ${stdout}\n${stderr}`));
        }
      });
    });
  });

  test.afterAll(async () => {
    runtime?.kill();
    if (provider?.listening) {
      await new Promise<void>((resolve) => provider.close(() => resolve()));
    }
  });

  test.afterEach(async ({ page }, testInfo) => {
    if (testInfo.status !== testInfo.expectedStatus) {
      await page.screenshot({
        path: testInfo.outputPath("provider-native.png"),
        fullPage: true,
      });
      await testInfo.attach("browser-errors", {
        body: JSON.stringify(browserErrors, null, 2),
        contentType: "application/json",
      });
    }
  });

  test("reads, approves a guarded write, and completes in the browser", async ({
    page,
  }) => {
    test.setTimeout(120_000);
    browserErrors = [];
    page.on("pageerror", (error) => browserErrors.push(error.message));
    page.on("console", (message) => {
      if (message.type() === "error") browserErrors.push(message.text());
    });

    await page.goto(runtimeUrl);
    const composer = page.locator("textarea").first();
    await expect(composer).toBeVisible({ timeout: 30_000 });
    await composer.fill(completedPrompt);
    await composer.press("Enter");

    await expect(
      page.getByText("Approval Required", { exact: true }),
    ).toBeVisible({ timeout: 60_000 });
    await expect(page.locator('[aria-label="Waiting"]')).toBeVisible();
    const readCards = page.locator('[data-tool-row="read"]');
    await expect(readCards).toHaveCount(1);
    expect(await readFile(path.join(workspace, "sample.txt"), "utf8")).toBe(
      originalContent,
    );
    const approvalResponse = page.waitForResponse(
      (response) =>
        new URL(response.url()).pathname.endsWith("/approval") &&
        response.request().method() === "POST",
    );
    await page.getByRole("button", { name: "Allow", exact: true }).click();
    const resolvedApprovalResponse = await approvalResponse;
    expect(resolvedApprovalResponse.ok()).toBe(true);
    const resolvedApproval = (await resolvedApprovalResponse.json()) as {
      session: { status: string };
      output: string | null;
    };
    expect(resolvedApproval.session.status).toBe("completed");
    expect(resolvedApproval.output).toContain(
      "Updated sample.txt after approval.",
    );
    await expect
      .poll(() => readFile(path.join(workspace, "sample.txt"), "utf8"))
      .toBe(updatedContent);
    await expect(
      page.getByText("Approval Required", { exact: true }),
    ).toHaveCount(0);

    await expect(
      page.getByText("Updated sample.txt after approval.", { exact: true }),
    ).toBeVisible({ timeout: 60_000 });
    await expect(page.locator('[aria-label="Completed"]')).toBeVisible();
    await expect(readCards).toHaveCount(2);
    await readCards.nth(0).locator("button[aria-expanded]").click();
    await readCards.nth(1).locator("button[aria-expanded]").click();
    await expect(readCards.nth(0)).toContainText(originalContent.trim());
    await expect(readCards.nth(1)).toContainText(updatedContent.trim());
    await expect(page.locator('[aria-label="Waiting"]')).toHaveCount(0);
    await expect(
      page.getByText(
        "Run stream ended before the runtime reported a final session status; outcome not confirmed.",
        { exact: true },
      ),
    ).toHaveCount(0);
    expect(await readFile(path.join(workspace, "sample.txt"), "utf8")).toBe(
      updatedContent,
    );
    const sessionsResponse = await page.request.get(`${runtimeUrl}/api/sessions`);
    expect(sessionsResponse.ok()).toBe(true);
    const sessions = (await sessionsResponse.json()) as StoredSessionSummary[];
    const completedSession = sessions.find(
      (item) => item.prompt === completedPrompt,
    );
    if (!completedSession) throw new Error("completed run was not persisted");
    expect(completedSession.status).toBe("completed");
    const completedResultResponse = await page.request.get(
      `${runtimeUrl}/api/sessions/${encodeURIComponent(completedSession.session.id)}/result`,
    );
    expect(completedResultResponse.ok()).toBe(true);
    const completedResult =
      (await completedResultResponse.json()) as RuntimeSessionResult;
    expect(completedResult.session.status).toBe("completed");
    expect(completedResult.status).toBe("completed");
    expect(completedResult.output).toContain(
      "Updated sample.txt after approval.",
    );
    await test.info().attach("completed-terminal-result", {
      body: JSON.stringify({
        session_id: completedSession.session.id,
        session_status: completedResult.session.status,
        result_status: completedResult.status,
        output: completedResult.output,
        visible_dom: await page.locator("body").innerText(),
      }),
      contentType: "application/json",
    });

    expect(providerRequests.length).toBeGreaterThan(0);
    expect(providerRequests.every((request) => request.stream === true)).toBe(
      true,
    );
    const finalMessages = providerRequests
      .map((request) => request.messages)
      .filter(
        (messages): messages is Record<string, unknown>[] =>
          Array.isArray(messages),
      )
      .reverse()
      .find(
        (messages) =>
          messages.filter(
            (message) =>
              message.role === "assistant" &&
              Array.isArray(message.tool_calls) &&
              message.tool_calls.some(
                (call) =>
                  typeof call === "object" &&
                  call !== null &&
                  (call as Record<string, unknown>).id === "call-browser-read",
              ),
          ).length === 2 &&
          messages.filter(
            (message) =>
              message.role === "tool" &&
              message.tool_call_id === "call-browser-read",
          ).length === 2 &&
          messages.some(
            (message) =>
              message.role === "assistant" &&
              Array.isArray(message.tool_calls) &&
              message.tool_calls.some(
                (call) =>
                  typeof call === "object" &&
                  call !== null &&
                  (call as Record<string, unknown>).id === "call-browser-write",
              ),
          ) &&
          messages.some(
            (message) =>
              message.role === "tool" &&
              message.tool_call_id === "call-browser-write",
          ),
      );
    if (!finalMessages) {
      throw new Error("provider received no completed replayed read/write/read history");
    }
    const assistantFor = (id: string) =>
      finalMessages.find(
        (message) =>
          message.role === "assistant" &&
          Array.isArray(message.tool_calls) &&
          message.tool_calls.some(
            (call) =>
              typeof call === "object" &&
              call !== null &&
              (call as Record<string, unknown>).id === id,
          ),
      );
    const callIds = (message: Record<string, unknown>) =>
      Array.isArray(message.tool_calls)
        ? message.tool_calls.flatMap((call) =>
            typeof call === "object" &&
            call !== null &&
            typeof (call as Record<string, unknown>).id === "string"
              ? [(call as Record<string, unknown>).id]
              : [],
          )
        : [];
    const readCall = assistantFor("call-browser-read");
    const writeCall = assistantFor("call-browser-write");
    const readCalls = finalMessages.filter(
      (message) =>
        message.role === "assistant" &&
        Array.isArray(message.tool_calls) &&
        message.tool_calls.some(
          (call) =>
            typeof call === "object" &&
            call !== null &&
            (call as Record<string, unknown>).id === "call-browser-read",
        ),
    );
    if (!readCall || !writeCall || readCalls.length !== 2) {
      throw new Error("provider replay omitted an original native call occurrence");
    }
    expect(readCalls[0]).not.toBe(readCalls[1]);
    expect(readCall).not.toBe(writeCall);
    expect(callIds(readCalls[0])).toEqual(["call-browser-read"]);
    expect(callIds(writeCall)).toEqual(["call-browser-write"]);
    expect(callIds(readCalls[1])).toEqual(["call-browser-read"]);
    expect(readCalls.map((message) => message.reasoning_content)).toEqual([
      reasoning,
      reasoning,
    ]);
    expect(writeCall.reasoning_content).toBe(reasoning);
    const readResults = finalMessages.filter(
      (message) =>
        message.role === "tool" &&
        message.tool_call_id === "call-browser-read",
    );
    const writeResult = finalMessages.find(
      (message) =>
        message.role === "tool" &&
        message.tool_call_id === "call-browser-write",
    );
    expect(readResults).toHaveLength(2);
    expect(readResults[0]?.content).toContain(originalContent.trim());
    expect(readResults[1]?.content).toContain(updatedContent.trim());
    expect(writeResult?.content).toContain("Wrote file successfully: sample.txt");
    const toolResults = finalMessages.filter(
      (message) => message.role === "tool",
    );
    expect(toolResults.map((message) => message.tool_call_id)).toEqual([
      "call-browser-read",
      "call-browser-write",
      "call-browser-read",
    ]);
    const promptIndex = finalMessages.findIndex(
      (message) =>
        message.role === "user" && message.content === completedPrompt,
    );
    expect(promptIndex).toBeGreaterThanOrEqual(0);
    expect(promptIndex).toBeLessThan(finalMessages.indexOf(readCalls[0]));
    expect(finalMessages.indexOf(readCalls[0])).toBeLessThan(
      finalMessages.indexOf(readResults[0]),
    );
    expect(finalMessages.indexOf(readResults[0])).toBeLessThan(
      finalMessages.indexOf(writeCall),
    );
    expect(finalMessages.indexOf(writeCall)).toBeLessThan(
      finalMessages.indexOf(writeResult!),
    );
    expect(finalMessages.indexOf(writeResult!)).toBeLessThan(
      finalMessages.indexOf(readCalls[1]),
    );
    expect(finalMessages.indexOf(readCalls[1])).toBeLessThan(
      finalMessages.indexOf(readResults[1]),
    );
    expect(browserErrors).toEqual([]);
    await composer.fill(failedPrompt);
    await composer.press("Enter");
    await expect
      .poll(
        async () => {
          const response = await page.request.get(
            `${runtimeUrl}/api/sessions/${encodeURIComponent(completedSession.session.id)}/result`,
          );
          if (!response.ok()) return null;
          const result = (await response.json()) as RuntimeSessionResult;
          return result.session.status;
        },
        { timeout: 60_000 },
      )
      .toBe("failed");
    const failedResultResponse = await page.request.get(
      `${runtimeUrl}/api/sessions/${encodeURIComponent(completedSession.session.id)}/result`,
    );
    expect(failedResultResponse.ok()).toBe(true);
    const failedResult =
      (await failedResultResponse.json()) as RuntimeSessionResult;
    expect(failedResult.session.status).toBe("failed");
    expect(failedResult.status).toBe("failed");
    if (!failedResult.error) {
      throw new Error("failed runtime result did not preserve its provider error");
    }
    await test.info().attach("failed-terminal-ui-proof", {
      body: JSON.stringify({
        session_id: completedSession.session.id,
        session_status: failedResult.session.status,
        result_status: failedResult.status,
        error: failedResult.error,
        visible_dom: await page.locator("body").innerText(),
      }),
      contentType: "application/json",
    });
    await expect(
      page.getByText(failedResult.error, { exact: true }),
    ).toBeVisible({ timeout: 30_000 });
  });
});
