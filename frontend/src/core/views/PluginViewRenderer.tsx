import React from "react";
import { useTranslation } from "react-i18next";
import type {
  ChoicesViewData,
  CommandDescriptor,
  ConfirmationViewData,
  DiffViewData,
  FieldsViewData,
  FormViewData,
  JsonViewData,
  MarkdownViewData,
  MediaViewData,
  NoticeViewData,
  PluginViewDescriptor,
  StatusViewData,
  TableViewData,
} from "../../lib/core/types";
import type { CoreClient } from "../../lib/core/client";
import { NoticeView } from "./NoticeView";
import { MarkdownView } from "./MarkdownView";
import { FieldsView } from "./FieldsView";
import { ChoicesView } from "./ChoicesView";
import { ConfirmationView } from "./ConfirmationView";
import { FormView } from "./FormView";
import { DiffView } from "./DiffView";
import { StatusView } from "./StatusView";
import { TableView } from "./TableView";
import { JsonView } from "./JsonView";
import { MediaView } from "./MediaView";

export interface PluginViewRendererProps {
  descriptor: PluginViewDescriptor;
  sessionId: string;
  /**
   * The revision of the views envelope that rendered this descriptor. Every
   * action the view can dispatch travels under exactly this revision: nothing
   * captured under one generation is ever relabelled with a later one.
   */
  revisionId: string;
  client: CoreClient;
  catalog: CommandDescriptor[];
  onAction?: (
    action: string,
    args: Record<string, unknown>,
    revisionId: string,
    prepareInput?: boolean,
  ) => void;
  onConfirm?: (
    requestId: string,
    decision: "allow" | "deny",
    revisionId: string,
  ) => void;
  onCancel?: () => void;
  disabled?: boolean;
}

export const PluginViewRenderer: React.FC<PluginViewRendererProps> = ({
  descriptor,
  sessionId,
  revisionId,
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
      return (
        <MarkdownView
          data={data as MarkdownViewData}
          disabled={disabled}
          onAction={(action, args, prepareInput) =>
            onAction(action, args, revisionId, prepareInput)
          }
        />
      );
    case "fields":
      return <FieldsView data={data as FieldsViewData} />;
    case "choices":
      return (
        <ChoicesView
          title={descriptor.title}
          data={data as ChoicesViewData}
          onSelect={(action, args) =>
            onAction(
              action,
              args,
              revisionId,
              (data as ChoicesViewData).prepare_input === true,
            )
          }
          disabled={disabled}
        />
      );
    case "confirmation":
      return (
        <ConfirmationView
          data={data as ConfirmationViewData}
          onDecision={(requestId, decision) =>
            onConfirm(requestId, decision, revisionId)
          }
          disabled={disabled}
        />
      );
    case "form":
      return (
        <FormView
          title={descriptor.title}
          data={data as unknown as FormViewData}
          sessionId={sessionId}
          revisionId={revisionId}
          client={client}
          catalog={catalog}
          argumentSchema={
            catalog.find((c) => c.name === (data as FormViewData).action)
              ?.argument_schema as Record<string, unknown> | undefined
          }
          onSubmit={(action, args) => onAction(action, args, revisionId)}
          onCancel={onCancel}
          disabled={disabled}
        />
      );
    case "diff":
      return <DiffView data={data as DiffViewData} />;
    case "status":
      return <StatusView data={data as StatusViewData} />;
    case "table":
      return <TableView data={data as TableViewData} />;
    case "json":
      return <JsonView data={data as JsonViewData} />;
    case "media":
      return <MediaView data={data as MediaViewData} />;
    default:
      return (
        <div
          data-testid="unsupported-view-error"
          className="p-4 rounded-lg bg-red-950/80 border border-red-500 text-red-200 text-sm my-2"
        >
          <div className="font-bold text-xs uppercase tracking-wider mb-1">
            {t("core.unsupportedView", { viewType: String(view_type) })}
          </div>
          <div>{t("core.unsupportedViewHint")}</div>
        </div>
      );
  }
};
