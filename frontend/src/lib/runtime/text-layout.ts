import { layout, prepare } from "@chenglou/pretext";

const STREAM_TEXT_ESTIMATE_WIDTH = 640;
const STREAM_TEXT_LINE_HEIGHT = 23;
const STREAM_TEXT_FONT = "14px Inter, sans-serif";

export function estimateStreamedTextHeight(
  text: string,
  width = STREAM_TEXT_ESTIMATE_WIDTH,
) {
  const normalized = text.trim();
  if (!normalized) return 0;
  try {
    const prepared = prepare(normalized, STREAM_TEXT_FONT, {
      whiteSpace: "normal",
    });
    return Math.ceil(layout(prepared, width, STREAM_TEXT_LINE_HEIGHT).height);
  } catch {
    return 0;
  }
}

export { STREAM_TEXT_ESTIMATE_WIDTH };
