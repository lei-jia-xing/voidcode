import { existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const scriptDir = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(scriptDir, '..');
const frontendDir = resolve(repoRoot, 'frontend');
const vitestEntrypoint = resolve(frontendDir, 'node_modules', 'vitest', 'vitest.mjs');
const vitestConfig = resolve(frontendDir, 'vite.config.ts');
const forwardedArgs = process.argv.slice(2).map((arg) => {
  if (arg === 'frontend') {
    return '.';
  }
  if (arg.startsWith('frontend/')) {
    return arg.slice('frontend/'.length);
  }
  if (arg.startsWith('./frontend/')) {
    return `./${arg.slice('./frontend/'.length)}`;
  }
  return arg;
});

if (!existsSync(vitestEntrypoint)) {
  console.error('error: frontend Vitest dependency is missing. Run "mise run frontend:install" first.');
  process.exit(1);
}

const result = Bun.spawnSync({
  cmd: [process.execPath, vitestEntrypoint, '--config', vitestConfig, ...forwardedArgs],
  cwd: frontendDir,
  stdout: 'inherit',
  stderr: 'inherit',
  stdin: 'inherit',
});

process.exit(result.exitCode);
