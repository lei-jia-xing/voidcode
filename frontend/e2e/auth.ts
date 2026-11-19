import type { Page } from "@playwright/test";

/**
 * Session-storage key the web client reads its bearer token from when no
 * `token` prop is given (same-origin bundle: no `?server=` override).
 * The token never travels in the URL: no history, Referer, or access-log leak.
 */
export const TOKEN_STORAGE_KEY = "voidcode:bearer:same-origin";

/** Open the served client with the bearer token seeded (never in the URL). */
export async function gotoAuthed(
  page: Page,
  base: string,
  token: string,
): Promise<void> {
  await page.addInitScript(
    (seed: { key: string; token: string }) => {
      window.sessionStorage.setItem(seed.key, seed.token);
    },
    { key: TOKEN_STORAGE_KEY, token },
  );
  await page.goto(`${base}/`);
}
