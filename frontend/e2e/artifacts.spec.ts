import { test, expect } from "@playwright/test";
import { spawn, execFile, type ChildProcess } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { createServer } from "node:net";
import * as os from "node:os";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

// This runs the current built bundle against an authenticated real Core. No RPC mocks.
test.setTimeout(120000);
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const python = path.join(root, ".venv", "bin", "python");
const token = "artifact-surface-token";
let server: ChildProcess | undefined;
let directory = "";
let base = "";
let logs = "";
let scene: { text_session: string; image_session: string; text_sequence: number; image_sequence: number; png_sha256: string; png_bytes: number };

async function freePort(): Promise<number> {
  const { promise, resolve, reject } = Promise.withResolvers<number>();
  const listener = createServer();
  listener.on("error", reject);
  listener.listen(0, "127.0.0.1", () => {
    const address = listener.address();
    if (!address || typeof address === "string") return reject(new Error("No free port"));
    listener.close(() => resolve(address.port));
  });
  return promise;
}

test.beforeAll(async () => {
  directory = mkdtempSync(path.join(os.tmpdir(), "voidcode-artifact-surface-"));
  const workspace = path.join(directory, "workspace");
  const state = path.join(directory, "state");
  const env = { ...process.env, PYTHONPATH: `${root}${path.delimiter}${path.join(root, "src")}`, PYTHONUNBUFFERED: "1" };
  const prepared = await promisify(execFile)(python, [path.join(root, "frontend", "e2e", "artifact_scene.py"), workspace, state], { cwd: root, env });
  scene = JSON.parse(prepared.stdout.trim().split("\n").at(-1)!);
  base = `http://127.0.0.1:${await freePort()}`;
  server = spawn(python, ["-m", "voidcode", "--workspace", workspace, "--state-dir", state, "serve", "--port", new URL(base).port, "--bearer-token", token], { cwd: root, env, stdio: ["ignore", "pipe", "pipe"] });
  server.stdout?.on("data", (chunk) => { logs += String(chunk); });
  server.stderr?.on("data", (chunk) => { logs += String(chunk); });
  const deadline = Date.now() + 30000;
  while (Date.now() < deadline) {
    if (server.exitCode !== null) throw new Error(logs);
    try {
      if ((await fetch(`${base}/api/health`, { headers: { Authorization: `Bearer ${token}` } })).ok) return;
    } catch { /* isolated server is starting */ }
    const delay = Promise.withResolvers<void>();
    setTimeout(delay.resolve, 50);
    await delay.promise;
  }
  throw new Error(`Core did not start: ${logs}`);
});

test.afterAll(async () => {
  if (server && server.exitCode === null) {
    const child = server;
    const stopped = Promise.withResolvers<void>();
    const timer = setTimeout(() => { child.kill("SIGKILL"); stopped.resolve(); }, 10000);
    child.once("exit", () => { clearTimeout(timer); stopped.resolve(); });
    child.kill("SIGTERM");
    await stopped.promise;
  }
  if (directory) rmSync(directory, { recursive: true, force: true });
});

test("real immutable text/binary/error pages and decoded read-tool PNG", async ({ page }) => {
  const reads: Array<{ sha256: string; offset: number }> = [];
  page.on("websocket", (socket) => socket.on("framesent", (event) => {
    const message = JSON.parse(String(event.payload));
    if (message.method === "artifact.read") reads.push(message.params);
  }));
  await page.goto(`${base}/?token=${token}`);
  await page.getByTestId(`session-tab-${scene.text_session}`).click();
  const card = page.getByTestId(`event-${scene.text_sequence}`);
  await expect(card).toContainText("SELECTED_INLINE_SUMMARY");
  await card.getByRole("button", { name: "View artifacts" }).click();
  const viewer = card.getByRole("region", { name: "Artifact viewer" });
  await expect(viewer.getByTestId("artifact-range")).toContainText("bytes [0, 65536)");
  await expect(viewer.getByTestId("artifact-page-content")).toHaveText("a".repeat(65535));
  await viewer.getByRole("button", { name: "Next", exact: true }).click();
  await expect(viewer.getByTestId("artifact-page-content")).toHaveText("🎯 complete later page\n");
  await expect(viewer.getByTestId("artifact-range")).toContainText("EOF yes");
  await expect(viewer.getByRole("button", { name: "Next", exact: true })).toBeDisabled();
  await viewer.getByRole("button", { name: "Back", exact: true }).click();
  await expect(viewer.getByTestId("artifact-page-content")).toHaveText("a".repeat(65535));
  const select = card.getByRole("combobox", { name: "Artifact source" });
  await select.selectOption({ label: "Result body.stderr" });
  await expect(viewer.getByTestId("artifact-page-content")).toHaveText("stderr is independent");
  await select.selectOption({ label: "Result body.binary" });
  await expect(viewer.getByTestId("artifact-page-content")).toHaveText("00000000  62 69 6e 61 72 79 00 ff 80 6f 72 69 67 69 6e 61\n00000010  6c");
  await select.selectOption({ label: "Result body.missing" });
  await expect(viewer.getByRole("alert")).toContainText("artifact is not stored");
  await expect(viewer.getByTestId("artifact-page-content")).toHaveCount(0);
  await expect(viewer).not.toContainText("stderr is independent");
  await select.selectOption({ label: "Complete structured result JSON" });
  await expect(viewer.getByTestId("artifact-page-content")).toContainText("WHOLE_STRUCTURED_RECORD");
  await expect(viewer.getByTestId("artifact-page-content")).toContainText('"stdout"');
  await select.selectOption({ label: "Result body.corrupt_png" });
  await expect(viewer.getByRole("alert")).toContainText("The source image could not be decoded");

  await page.getByTestId(`session-tab-${scene.image_session}`).click();
  const imageCard = page.getByTestId(`event-${scene.image_sequence}`);
  await imageCard.getByRole("button", { name: "View artifacts" }).click();
  const imageViewer = imageCard.getByRole("region", { name: "Artifact viewer" });
  const image = imageViewer.getByRole("img");
  await expect(imageViewer).toContainText(`artifact://${scene.png_sha256}`);
  await expect(imageViewer).toContainText("Decoded image: 384×300");
  expect(await image.evaluate((node: HTMLImageElement) => [node.complete, node.naturalWidth, node.naturalHeight])).toEqual([true, 384, 300]);
  expect(reads.filter((read) => read.sha256 === scene.png_sha256).map((read) => read.offset)).toContain(327680);
  const oldUrl = await image.getAttribute("src");
  await imageViewer.screenshot({ path: path.join(os.tmpdir(), "voidcode-artifact-browser.png") });
  await imageCard.getByRole("button", { name: "Hide artifacts" }).click();
  expect(await page.evaluate(async (url) => { try { await fetch(url!); return false; } catch { return true; } }, oldUrl)).toBe(true);
  await imageCard.getByRole("button", { name: "View artifacts" }).click();
  await expect(imageCard.getByRole("region", { name: "Artifact viewer" })).toContainText("Decoded image: 384×300");
  console.log(`Observed real Core/RPC text next/back UTF-8, independent sources, binary hex, missing/corrupt errors; PNG ${scene.png_bytes} bytes, 384x300; screenshot /tmp/voidcode-artifact-browser.png`);
});
