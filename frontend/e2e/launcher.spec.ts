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

    proc = spawn(
      'uv',
      ['run', 'voidcode', 'web', '--no-open', '--port', port.toString(), '--workspace', rootDir],
      {
        cwd: rootDir,
        env: { ...process.env, PYTHONUNBUFFERED: '1' },
      },
    );

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

    await expect(page.locator('.prose').first()).toBeVisible();
  });

  test('should answer a runtime question from the browser', async ({ page }) => {
    await page.route('**/api/runtime/run/stream', async (route) => {
      const session = { session: { id: 'question-session' }, status: 'waiting', turn: 1, metadata: {} };
      const requestEvent = {
        session_id: 'question-session',
        sequence: 1,
        event_type: 'runtime.request_received',
        source: 'runtime',
        payload: { prompt: 'ask for direction' },
      };
      const questionEvent = {
        session_id: 'question-session',
        sequence: 2,
        event_type: 'runtime.question_requested',
        source: 'runtime',
        payload: {
          request_id: 'question-1',
          tool: 'question',
          question_count: 1,
          questions: [
            {
              header: 'Direction',
              question: 'Which path should VoidCode take?',
              multiple: false,
              options: [
                { label: 'simple', description: 'Use the simple path' },
                { label: 'detailed', description: 'Use the detailed path' },
              ],
            },
          ],
        },
      };
      await route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: [
          `data: ${JSON.stringify({ kind: 'event', session, event: requestEvent, output: null })}`,
          '',
          `data: ${JSON.stringify({ kind: 'event', session, event: questionEvent, output: null })}`,
          '',
        ].join('\n'),
      });
    });

    await page.route('**/api/sessions/question-session/question', async (route) => {
      const body = route.request().postDataJSON() as { request_id?: string };
      expect(body.request_id).toBe('question-1');
      expect(body).toMatchObject({
        responses: [{ header: 'Direction', answers: ['simple'] }],
      });
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          session: { session: { id: 'question-session' }, status: 'completed', turn: 1, metadata: {} },
          events: [
            {
              session_id: 'question-session',
              sequence: 1,
              event_type: 'runtime.request_received',
              source: 'runtime',
              payload: { prompt: 'ask for direction' },
            },
            {
              session_id: 'question-session',
              sequence: 2,
              event_type: 'runtime.question_answered',
              source: 'runtime',
              payload: { request_id: 'question-1' },
            },
            {
              session_id: 'question-session',
              sequence: 3,
              event_type: 'graph.response_ready',
              source: 'graph',
              payload: { output: 'question answered' },
            },
          ],
          output: 'question answered',
        }),
      });
    });

    await page.route('**/api/sessions', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([
          {
            session: { id: 'question-session' },
            status: 'waiting',
            turn: 1,
            prompt: 'ask for direction',
            updated_at: 1,
          },
        ]),
      });
    });

    await page.route('**/api/sessions/question-session/debug', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          session: { session: { id: 'question-session' }, status: 'waiting', turn: 1, metadata: {} },
          prompt: 'ask for direction',
          persisted_status: 'waiting',
          current_status: 'waiting',
          active: false,
          resumable: true,
          replayable: true,
          terminal: false,
          pending_question: { request_id: 'question-1', tool_name: 'question', question_count: 1, headers: ['Direction'] },
          failure: null,
          last_tool: null,
          suggested_operator_action: 'answer_question',
          operator_guidance: 'Answer the pending question.',
        }),
      });
    });

    await page.route('**/api/sessions/question-session/tasks', async (route) => {
      await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
    });

    await page.goto(launcherUrl);
    const input = page.locator('textarea');
    await expect(input).toBeVisible({ timeout: 20000 });
    await input.fill('ask for direction');
    await input.press('Enter');

    await expect(page.getByText('Question Required')).toBeVisible({ timeout: 10000 });
    await page.getByLabel('simple').check();
    await page.getByRole('button', { name: 'Submit Answer' }).click();

    await expect(page.getByText('question answered')).toBeVisible({ timeout: 10000 });
  });
});
