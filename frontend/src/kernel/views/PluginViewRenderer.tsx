import React from "react";
import { useTranslation } from "react-i18next";
import type {
  ChoicesViewData,
  CommandDescriptor,
  ConfirmationViewData,
  DiffViewData,
  FieldsViewData,
  FormViewData,
  MarkdownViewData,
  NoticeViewData,
  PluginViewDescriptor,
  StatusViewData,
} from "../../lib/kernel/types";
import type { KernelClient } from "../../lib/kernel/client";
import { NoticeView } from "./NoticeView";
import { MarkdownView } from "./MarkdownView";
import { FieldsView } from "./FieldsView";
import { ChoicesView } from "./ChoicesView";
import { ConfirmationView } from "./ConfirmationView";
import { FormView } from "./FormView";
import { DiffView } from "./DiffView";
import { StatusView } from "./StatusView";

export interface PluginViewRendererProps {
  descriptor: PluginViewDescriptor;
  sessionId: string;
  client: KernelClient;
  catalog: CommandDescriptor[];
  onAction?: (action: string, args: Record<string, unknown>) => void;
  onConfirm?: (requestId: string, decision: "allow" | "deny") => void;
  onCancel?: () => void;
  disabled?: boolean;
}

export const PluginViewRenderer: React.FC<PluginViewRendererProps> = ({
  descriptor,
  sessionId,
  client,
  catalog,
  onAction = () => {},
  onConfirm = () => {},
  onCancel,
  disabled = false,
}) => {
  const { t } = useTranslation();
  const { view_type, data } = descriptor;

  switch (view_type) {
    case "notice":
      return <NoticeView data={data as NoticeViewData} />;
    case "markdown":
      return <MarkdownView data={data as MarkdownViewData} />;
    case "fields":
      return <FieldsView data={data as FieldsViewData} />;
    case "choices":
      return (
        <ChoicesView
          data={data as ChoicesViewData}
          onSelect={onAction}
          disabled={disabled}
        />
      );
    case "confirmation":
      return (
        <ConfirmationView
          data={data as ConfirmationViewData}
          onDecision={onConfirm}
          disabled={disabled}
        />
      );
    case "form":
      return (
        <FormView
          data={data as unknown as FormViewData}
          sessionId={sessionId}
          client={client}
          catalog={catalog}
          argumentSchema={
            catalog.find((c) => c.name === (data as FormViewData).action)
              ?.argument_schema as Record<string, unknown> | undefined
          }
          onSubmit={onAction}
          onCancel={onCancel}
          disabled={disabled}
        />
      );
    case "diff":
      return <DiffView data={data as DiffViewData} />;
    case "status":
      return <StatusView data={data as StatusViewData} />;
    default:
      return (
        <div
          data-testid="unsupported-view-error"
          className="p-4 rounded-lg bg-red-950/80 border border-red-500 text-red-200 text-sm my-2"
        >
          <div className="font-bold text-xs uppercase tracking-wider mb-1">
            {t("kernel.unsupportedView", { viewType: String(view_type) })}
          </div>
          <div>{t("kernel.unsupportedViewHint")}</div>
        </div>
      );
  }
};
