import { existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const scriptDir = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(scriptDir, '..');
const frontendDir = resolve(repoRoot, 'frontend');
const playwrightEntrypoint = resolve(frontendDir, 'node_modules', '@playwright', 'test', 'cli.js');
const playwrightConfig = resolve(frontendDir, 'playwright.config.ts');

if (!existsSync(playwrightEntrypoint)) {
  console.error(
    'error: frontend Playwright dependency is missing. Run "mise run frontend:install" first.',
  );
  process.exit(1);
}

const result = Bun.spawnSync({
  cmd: [process.execPath, playwrightEntrypoint, ...process.argv.slice(2), '--config', playwrightConfig],
  cwd: repoRoot,
  stdout: 'inherit',
  stderr: 'inherit',
  stdin: 'inherit',
});

process.exit(result.exitCode);
