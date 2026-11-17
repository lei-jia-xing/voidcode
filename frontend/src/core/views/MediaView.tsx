import React from "react";
import type { MediaViewData } from "../../lib/core/types";
import { componentFg, componentStyle } from "./componentStyle";

export interface MediaViewProps {
  data: MediaViewData;
}

export const MediaView: React.FC<MediaViewProps> = ({ data }) => {
  const fg = componentFg(data);
  const label = data.mime.startsWith("image/")
    ? "Image"
    : data.mime.startsWith("audio/")
      ? "Audio"
      : "Media";
  return (
    <div
      data-testid="media-view"
      style={componentStyle(data)}
      className="rounded-lg border border-neutral-700 bg-neutral-900/60 p-4 my-2 text-sm font-mono"
    >
      <div style={fg} className="text-xs text-neutral-300">
        [{label}: {data.mime}, {data.reference.bytes} bytes, artifact://
        {data.reference.sha256}] {data.alt}
      </div>
    </div>
  );
};
