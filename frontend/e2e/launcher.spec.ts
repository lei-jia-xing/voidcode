import { test, expect } from '@playwright/test';
import { spawn, ChildProcess } from 'child_process';
import * as path from 'path';
import { fileURLToPath } from 'url';

let proc: ChildProcess;
let launcherUrl: string;

let stdoutData = '';
let stderrData = '';

test.describe('VoidCode Web Launcher', () => {
  test.beforeAll(async () => {
    const rootDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
    const port = Math.floor(Math.random() * 50000) + 10000;

    proc = spawn('uv', ['run', 'voidcode', 'web', '--port', port.toString(), '--workspace', rootDir], {
      cwd: rootDir,
      env: { ...process.env, PYTHONUNBUFFERED: '1' },
    });

    stdoutData = '';
    stderrData = '';
    launcherUrl = await new Promise<string>((resolve, reject) => {
      let foundUrl = '';
      let uvicornReady = false;

      proc.stdout?.on('data', (data) => {
        stdoutData += data.toString();
        if (!foundUrl) {
          const match = stdoutData.match(/Local server running at:\s+(http:\/\/[^\s]+)/);
          if (match) {
            foundUrl = match[1];
            if (uvicornReady) resolve(foundUrl);
          }
        }
      });

      proc.stderr?.on('data', (data) => {
        stderrData += data.toString();
        if (!uvicornReady) {
          if (stderrData.includes('Uvicorn running on')) {
            uvicornReady = true;
            if (foundUrl) resolve(foundUrl);
          }
        }
      });

      proc.on('close', (code) => {
        if (!foundUrl || !uvicornReady) reject(new Error(`voidcode closed with code ${code}. Output: ${stdoutData} | Err: ${stderrData}`));
      });
    });
  });

  test.afterAll(() => {
    if (proc) proc.kill();
  });

  test('should fallback to SPA on unknown routes', async ({ page }) => {
    const response = await page.goto(`${launcherUrl}/some-non-existent-route`);
    expect(response?.status()).toBe(200);
    await expect(page).toHaveTitle(/VoidCode/i);
    await expect(page.locator('textarea').first()).toBeVisible({ timeout: 20000 });
  });

  test('should run a task using env-backed key without browser-side API key entry', async ({ page }) => {
    test.skip(!process.env.OPENCODE_API_KEY, 'Skipping live smoke test because OPENCODE_API_KEY is not set in environment.');

    await page.goto(launcherUrl);
    await expect(page).toHaveTitle(/VoidCode/i);

    const settingsModal = page.locator('div[role="dialog"]');
    await expect(settingsModal).not.toBeVisible();

    const input = page.locator('textarea');
    await expect(input).toBeVisible({ timeout: 20000 });
    await input.fill('read README.md');
    await input.press('Enter');

    await expect(page.getByText('Agent Busy')).toBeVisible({ timeout: 10000 });
    await expect(page.getByText('Agent Idle')).toBeVisible({ timeout: 60000 });

    const runErrorBanner = page.locator('div.flex-shrink-0[class*="bg-rose-500/10"]');
    await expect(runErrorBanner).not.toBeVisible();

    await expect(page.getByText('Assistant').first()).toBeVisible();
  });
});
