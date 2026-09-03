/** Convert unknown thrown values into useful, non-empty UI text. */
export function errorMessage(value: unknown): string {
  if (value instanceof Error && value.message.trim()) return value.message;
  if (typeof value === "string" && value.trim()) return value.trim();
  if (value !== null && typeof value === "object") {
    const message =
      "message" in value ? (value as { message?: unknown }).message : undefined;
    if (typeof message === "string" && message.trim()) return message.trim();
  }
  return "Unknown error";
}
