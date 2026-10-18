import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { NoticeView } from "./views/NoticeView";
import { MarkdownView } from "./views/MarkdownView";
import { FieldsView } from "./views/FieldsView";
import { ChoicesView } from "./views/ChoicesView";
import { ConfirmationView } from "./views/ConfirmationView";
import { FormView } from "./views/FormView";
import { DiffView } from "./views/DiffView";
import { StatusView } from "./views/StatusView";
import { PluginViewRenderer } from "./views/PluginViewRenderer";
import type {
  CommandDescriptor,
  PluginViewDescriptor,
} from "../lib/kernel/types";
import type { KernelClient } from "../lib/kernel/client";
import "../i18n";
describe("NoticeView", () => {
  it("renders notice message and level badge", () => {
    render(
      <NoticeView data={{ message: "All systems nominal", level: "info" }} />,
    );
    expect(screen.getByTestId("notice-view")).toHaveTextContent(
      "All systems nominal",
    );
    expect(screen.getByTestId("notice-view")).toHaveAttribute(
      "data-level",
      "info",
    );
  });

  it("renders warning and error levels", () => {
    const { rerender } = render(
      <NoticeView data={{ message: "Low disk", level: "warning" }} />,
    );
    expect(screen.getByTestId("notice-view")).toHaveAttribute(
      "data-level",
      "warning",
    );
    rerender(<NoticeView data={{ message: "Disk full", level: "error" }} />);
    expect(screen.getByTestId("notice-view")).toHaveAttribute(
      "data-level",
      "error",
    );
  });
});

describe("MarkdownView", () => {
  it("renders markdown structure rather than raw text", () => {
    render(
      <MarkdownView
        data={{ content: "## Hello Kernel\n\n- first item\n- second item\n" }}
      />,
    );
    const view = screen.getByTestId("markdown-view");
    expect(view.querySelector("h2")).not.toBeNull();
    expect(view.querySelectorAll("li")).toHaveLength(2);
  });
});

describe("FieldsView", () => {
  it("renders key value rows", () => {
    render(
      <FieldsView
        data={{
          rows: [
            { label: "Status", value: "active" },
            { label: "Replicas", value: "3" },
          ],
        }}
      />,
    );
    expect(screen.getByTestId("fields-view")).toHaveTextContent("Status:");
    expect(screen.getByTestId("fields-view")).toHaveTextContent("active");
    expect(screen.getByTestId("fields-view")).toHaveTextContent("Replicas:");
    expect(screen.getByTestId("fields-view")).toHaveTextContent("3");
  });
});

describe("ChoicesView", () => {
  it("renders options and triggers selection with configured argument_name", () => {
    const onSelect = vi.fn();
    render(
      <ChoicesView
        data={{
          action: "set_mode",
          argument_name: "mode_id",
          options: [
            { id: "fast", label: "Fast Mode" },
            { id: "slow", label: "Slow Mode" },
          ],
        }}
        onSelect={onSelect}
      />,
    );

    expect(screen.getByTestId("choices-view")).toHaveTextContent("Fast Mode");
    expect(screen.getByTestId("choices-view")).toHaveTextContent("Slow Mode");

    fireEvent.click(screen.getByTestId("choice-option-fast"));
    expect(onSelect).toHaveBeenCalledWith("set_mode", { mode_id: "fast" });
  });
});

describe("ConfirmationView", () => {
  it("renders request_id, message and binds allow/deny decision", () => {
    const onDecision = vi.fn();
    render(
      <ConfirmationView
        data={{
          request_id: "req-1234",
          message: "Delete database table?",
          actions: ["allow", "deny"],
        }}
        onDecision={onDecision}
      />,
    );

    expect(screen.getByTestId("confirmation-view")).toHaveTextContent(
      "req-1234",
    );
    expect(screen.getByTestId("confirmation-view")).toHaveTextContent(
      "Delete database table?",
    );

    fireEvent.click(screen.getByTestId("confirm-allow-btn"));
    expect(onDecision).toHaveBeenCalledWith("req-1234", "allow");

    fireEvent.click(screen.getByTestId("confirm-deny-btn"));
    expect(onDecision).toHaveBeenCalledWith("req-1234", "deny");
  });
});

describe("FormView", () => {
  const dummyCatalog: CommandDescriptor[] = [
    {
      owner: "test",
      name: "get_initial_form",
      description: "Read initial objective",
      effects: ["read"],
      capabilities: [],
      client_capabilities: [],
      busy_gate: false,
    },
    {
      owner: "test",
      name: "unsafe_write_action",
      description: "Mutating action",
      effects: ["write"],
      capabilities: [],
      client_capabilities: [],
      busy_gate: false,
    },
  ];
  it("renders inputs with initial values and submits values", async () => {
    const onSubmit = vi.fn();
    const mockClient = {
      invoke: vi.fn(),
    } as unknown as KernelClient;

    render(
      <FormView
        data={{
          action: "update_profile",
          fields: [
            {
              name: "username",
              label: "User Name",
              field_type: "text",
              required: true,
            },
            {
              name: "bio",
              label: "Biography",
              field_type: "multiline",
              required: false,
            },
            {
              name: "active",
              label: "Active",
              field_type: "boolean",
              required: false,
            },
          ],
          initial_values: { username: "alice", active: true },
        }}
        sessionId="sess-1"
        client={mockClient}
        catalog={dummyCatalog}
        onSubmit={onSubmit}
      />,
    );

    expect(screen.getByTestId("form-input-username")).toHaveValue("alice");
    expect(screen.getByTestId("form-input-active")).toHaveValue("true");

    fireEvent.change(screen.getByTestId("form-input-username"), {
      target: { value: "bob" },
    });
    fireEvent.click(screen.getAllByTestId("form-submit-btn").at(-1)!);

    // Blank optional text is omitted, never fabricated as "".
    expect(onSubmit).toHaveBeenCalledWith("update_profile", {
      username: "bob",
      active: true,
    });
  });

  it("omits blank optional numerics instead of fabricating 0, and keeps 1e3 literal text invalid", async () => {
    const onSubmit = vi.fn();
    const mockClient = { invoke: vi.fn() } as unknown as KernelClient;

    render(
      <FormView
        data={{
          action: "set_budget",
          fields: [
            {
              name: "budget",
              label: "Budget",
              field_type: "integer",
              required: false,
            },
            {
              name: "ratio",
              label: "Ratio",
              field_type: "number",
              required: false,
            },
          ],
        }}
        sessionId="sess-1"
        client={mockClient}
        catalog={dummyCatalog}
        onSubmit={onSubmit}
      />,
    );

    // Both start empty and submit as omitted keys (no 0 injection).
    fireEvent.click(screen.getAllByTestId("form-submit-btn").at(-1)!);
    expect(onSubmit).toHaveBeenCalledWith("set_budget", {});

    // "1e3" is not an integer literal: visible error, nothing submitted.
    onSubmit.mockClear();
    fireEvent.change(screen.getByTestId("form-input-budget"), {
      target: { value: "1e3" },
    });
    fireEvent.click(screen.getAllByTestId("form-submit-btn").at(-1)!);
    expect(onSubmit).not.toHaveBeenCalled();
    expect(
      screen.getAllByTestId("form-validation-error").at(-1)!,
    ).not.toBeEmptyDOMElement();

    // A real number is submitted verbatim.
    fireEvent.change(screen.getByTestId("form-input-budget"), {
      target: { value: "-12" },
    });
    fireEvent.change(screen.getByTestId("form-input-ratio"), {
      target: { value: "2.5e-2" },
    });
    fireEvent.click(screen.getAllByTestId("form-submit-btn").at(-1)!);
    expect(onSubmit).toHaveBeenCalledWith("set_budget", {
      budget: -12,
      ratio: 0.025,
    });
  });

  it("keeps missing, null and explicit booleans distinct on submit", async () => {
    const onSubmit = vi.fn();
    const mockClient = { invoke: vi.fn() } as unknown as KernelClient;
    const schema = {
      type: "object",
      properties: {
        flag: { type: "boolean" },
        maybe: { anyOf: [{ type: "boolean" }, { type: "null" }] },
      },
    };

    const { unmount } = render(
      <FormView
        data={{
          action: "set_flags",
          fields: [
            {
              name: "flag",
              label: "Flag",
              field_type: "boolean",
              required: false,
            },
            {
              name: "maybe",
              label: "Maybe",
              field_type: "boolean",
              required: false,
            },
          ],
        }}
        sessionId="sess-1"
        client={mockClient}
        catalog={dummyCatalog}
        argumentSchema={schema}
        onSubmit={onSubmit}
      />,
    );

    // Absent fields are omitted: an untouched form never clears a nullable value.
    fireEvent.click(screen.getAllByTestId("form-submit-btn").at(-1)!);
    expect(onSubmit).toHaveBeenCalledWith("set_flags", {});

    // A deliberate blank on the nullable field is an explicit clear.
    onSubmit.mockClear();
    fireEvent.change(screen.getByTestId("form-input-maybe"), {
      target: { value: "" },
    });
    fireEvent.click(screen.getAllByTestId("form-submit-btn").at(-1)!);
    expect(onSubmit).toHaveBeenCalledWith("set_flags", { maybe: null });

    // An explicit false is submitted as false, not dropped.
    onSubmit.mockClear();
    fireEvent.change(screen.getByTestId("form-input-flag"), {
      target: { value: "false" },
    });
    fireEvent.click(screen.getAllByTestId("form-submit-btn").at(-1)!);
    expect(onSubmit).toHaveBeenCalledWith("set_flags", {
      flag: false,
      maybe: null,
    });

    // An explicit true is submitted as true.
    onSubmit.mockClear();
    fireEvent.change(screen.getByTestId("form-input-flag"), {
      target: { value: "true" },
    });
    fireEvent.click(screen.getAllByTestId("form-submit-btn").at(-1)!);
    expect(onSubmit).toHaveBeenCalledWith("set_flags", {
      flag: true,
      maybe: null,
    });

    // A missing required nullable field is refused until the user acts on it.
    unmount();
    onSubmit.mockClear();
    render(
      <FormView
        data={{
          action: "set_budget",
          fields: [
            {
              name: "budget",
              label: "Budget",
              field_type: "integer",
              required: true,
            },
          ],
        }}
        sessionId="sess-1"
        client={mockClient}
        catalog={dummyCatalog}
        argumentSchema={{
          type: "object",
          properties: {
            budget: { anyOf: [{ type: "integer" }, { type: "null" }] },
          },
        }}
        onSubmit={onSubmit}
      />,
    );
    fireEvent.click(screen.getAllByTestId("form-submit-btn").at(-1)!);
    expect(onSubmit).not.toHaveBeenCalled();
    expect(
      screen.getAllByTestId("form-validation-error").at(-1)!,
    ).not.toBeEmptyDOMElement();

    // A missing required boolean is refused rather than coerced to false.
    // A real session switch remounts the form (views are keyed per session), so
    // the required case starts from a fresh instance rather than carried state.
    onSubmit.mockClear();
    unmount();
    render(
      <FormView
        data={{
          action: "set_required",
          fields: [
            {
              name: "flag",
              label: "Flag",
              field_type: "boolean",
              required: true,
            },
          ],
        }}
        sessionId="sess-1"
        client={mockClient}
        catalog={dummyCatalog}
        argumentSchema={{
          type: "object",
          properties: { flag: { type: "boolean" } },
        }}
        onSubmit={onSubmit}
      />,
    );
    fireEvent.click(screen.getAllByTestId("form-submit-btn").at(-1)!);
    expect(onSubmit).not.toHaveBeenCalled();
    expect(
      screen.getAllByTestId("form-validation-error").at(-1)!,
    ).not.toBeEmptyDOMElement();
  });

  it("treats an omitted required key as required (SDK default true)", async () => {
    const onSubmit = vi.fn();
    const mockClient = { invoke: vi.fn() } as unknown as KernelClient;

    render(
      <FormView
        data={{
          action: "create_item",
          fields: [
            {
              name: "req",
              label: "Required",
              field_type: "text",
              required: true,
            },
          ],
        }}
        sessionId="sess-1"
        client={mockClient}
        catalog={dummyCatalog}
        onSubmit={onSubmit}
      />,
    );

    fireEvent.click(screen.getAllByTestId("form-submit-btn").at(-1)!);
    expect(onSubmit).not.toHaveBeenCalled();
    expect(
      screen.getAllByTestId("form-validation-error").at(-1)!,
    ).not.toBeEmptyDOMElement();
  });

  it("validates required fields", async () => {
    const onSubmit = vi.fn();
    const mockClient = { invoke: vi.fn() } as unknown as KernelClient;

    render(
      <FormView
        data={{
          action: "submit_order",
          fields: [
            {
              name: "item",
              label: "Item Name",
              field_type: "text",
              required: true,
            },
          ],
        }}
        sessionId="sess-1"
        client={mockClient}
        catalog={dummyCatalog}
        onSubmit={onSubmit}
      />,
    );

    fireEvent.click(screen.getAllByTestId("form-submit-btn").at(-1)!);
    expect(onSubmit).not.toHaveBeenCalled();
    expect(
      screen.getAllByTestId("form-validation-error").at(-1)!,
    ).not.toBeEmptyDOMElement();
  });

  it("verifies load_action is declared READ and loads fields", async () => {
    const onSubmit = vi.fn();
    const mockClient = {
      invoke: vi.fn().mockResolvedValue({
        status: "ok",
        data: { description: "Loaded long text from read action" },
      }),
    } as unknown as KernelClient;

    render(
      <FormView
        data={{
          action: "save_item",
          load_action: "get_initial_form",
          fields: [
            {
              name: "description",
              label: "Description",
              field_type: "text",
              required: true,
            },
          ],
        }}
        sessionId="sess-1"
        client={mockClient}
        catalog={dummyCatalog}
        onSubmit={onSubmit}
      />,
    );

    await waitFor(() => {
      expect(mockClient.invoke).toHaveBeenCalledWith(
        "sess-1",
        "get_initial_form",
      );
      expect(screen.getByTestId("form-input-description")).toHaveValue(
        "Loaded long text from read action",
      );
    });
  });

  it("prefills a long multiline objective from a declared read action", async () => {
    const onSubmit = vi.fn();
    const longObjective = "目标：" + "思考与验证".repeat(900);
    const mockClient = {
      invoke: vi.fn().mockResolvedValue({
        status: "ok",
        data: { objective: longObjective },
      }),
    } as unknown as KernelClient;

    render(
      <FormView
        data={{
          action: "goal.edit",
          load_action: "goal.load_objective",
          fields: [
            {
              name: "objective",
              label: "Objective",
              field_type: "multiline",
              required: true,
            },
            {
              name: "token_budget",
              label: "Token Budget",
              field_type: "integer",
              required: false,
            },
          ],
          initial_values: { token_budget: 5000 },
        }}
        sessionId="sess-1"
        client={mockClient}
        catalog={[
          ...dummyCatalog,
          {
            owner: "goal",
            name: "goal.load_objective",
            description: "Read the full objective",
            effects: ["read"],
            capabilities: [],
            client_capabilities: [],
            busy_gate: false,
          },
        ]}
        onSubmit={onSubmit}
      />,
    );

    await waitFor(() => {
      expect(screen.getByTestId("form-input-objective")).toHaveValue(
        longObjective,
      );
    });
    expect(screen.getByTestId("form-input-token_budget")).toHaveValue("5000");
  });

  it("refuses to run load_action unless it declares exactly the read effect", async () => {
    const onSubmit = vi.fn();
    const mockClient = { invoke: vi.fn() } as unknown as KernelClient;
    const mixedCatalog: CommandDescriptor[] = [
      {
        owner: "test",
        name: "read_write_probe",
        description: "Read+write probe",
        effects: ["read", "write"],
        capabilities: [],
        client_capabilities: [],
        busy_gate: false,
      },
    ];

    render(
      <FormView
        data={{
          action: "save_item",
          load_action: "read_write_probe",
          fields: [
            {
              name: "title",
              label: "Title",
              field_type: "text",
              required: true,
            },
          ],
        }}
        sessionId="sess-1"
        client={mockClient}
        catalog={mixedCatalog}
        onSubmit={onSubmit}
      />,
    );

    expect(mockClient.invoke).not.toHaveBeenCalled();
    expect(screen.getByTestId("form-load-error")).not.toBeEmptyDOMElement();
  });

  it("drops a late load result once the form moved on", async () => {
    const onSubmit = vi.fn();
    const first = Promise.withResolvers<{
      status: string;
      data: Record<string, unknown>;
    }>();
    const second = Promise.withResolvers<{
      status: string;
      data: Record<string, unknown>;
    }>();
    const mockClient = {
      invoke: vi
        .fn()
        .mockReturnValueOnce(first.promise)
        .mockReturnValueOnce(second.promise),
    } as unknown as KernelClient;

    const { rerender } = render(
      <FormView
        data={{
          action: "edit_item",
          load_action: "get_initial_form",
          fields: [
            {
              name: "summary",
              label: "Summary",
              field_type: "text",
              required: true,
            },
          ],
        }}
        sessionId="sess-1"
        client={mockClient}
        catalog={dummyCatalog}
        onSubmit={onSubmit}
      />,
    );

    // The component rebinds to another session while the first read is pending.
    rerender(
      <FormView
        data={{
          action: "edit_item",
          load_action: "get_initial_form",
          fields: [
            {
              name: "summary",
              label: "Summary",
              field_type: "text",
              required: true,
            },
          ],
        }}
        sessionId="sess-2"
        client={mockClient}
        catalog={dummyCatalog}
        onSubmit={onSubmit}
      />,
    );

    // The stale A response resolves last and must be ignored.
    second.resolve({
      status: "ok",
      data: { summary: "current session value" },
    });
    await waitFor(() => {
      expect(screen.getByTestId("form-input-summary")).toHaveValue(
        "current session value",
      );
    });
    first.resolve({ status: "ok", data: { summary: "stale session value" } });
    await waitFor(() => {
      expect(screen.getByTestId("form-input-summary")).not.toHaveValue(
        "stale session value",
      );
    });
  });

  it("keeps a draft typed while the loader is in flight", async () => {
    const onSubmit = vi.fn();
    const { promise, resolve } = Promise.withResolvers<{
      status: string;
      data: Record<string, unknown>;
    }>();
    const mockClient = {
      invoke: vi.fn().mockReturnValue(promise),
    } as unknown as KernelClient;

    render(
      <FormView
        data={{
          action: "edit_item",
          load_action: "get_initial_form",
          fields: [
            {
              name: "summary",
              label: "Summary",
              field_type: "multiline",
              required: true,
            },
          ],
        }}
        sessionId="sess-1"
        client={mockClient}
        catalog={dummyCatalog}
        onSubmit={onSubmit}
      />,
    );

    // User types while the read is unresolved.
    fireEvent.change(screen.getByTestId("form-input-summary"), {
      target: { value: "my own draft" },
    });

    // Server data arrives afterwards and must not clobber the draft.
    resolve({ status: "ok", data: { summary: "server value" } });
    await waitFor(() => {
      expect(screen.getByTestId("form-input-summary")).toHaveValue(
        "my own draft",
      );
    });
  });
});

describe("DiffView", () => {
  it("renders file path and unified diff lines", () => {
    render(
      <DiffView
        data={{
          path: "src/main.ts",
          before: "const a = 1;",
          after: "const a = 2;",
        }}
      />,
    );
    expect(screen.getByTestId("diff-view")).toHaveTextContent(
      "File: src/main.ts",
    );
    expect(screen.getByTestId("diff-view")).toHaveTextContent("- const a = 1;");
    expect(screen.getByTestId("diff-view")).toHaveTextContent("+ const a = 2;");
  });
});

describe("StatusView", () => {
  it("renders status badge", () => {
    render(<StatusView data={{ label: "Pipeline", value: "Success" }} />);
    expect(screen.getByTestId("status-view")).toHaveTextContent("Pipeline:");
    expect(screen.getByTestId("status-view")).toHaveTextContent("Success");
  });
});

describe("PluginViewRenderer", () => {
  it("renders unsupported view error for invalid view type", () => {
    const mockClient = { invoke: vi.fn() } as unknown as KernelClient;
    render(
      <PluginViewRenderer
        descriptor={{
          owner: "test",
          name: "test_view",
          title: "Test",
          view_type:
            "invalid_mystery_view" as unknown as PluginViewDescriptor["view_type"],
          data: {} as unknown as PluginViewDescriptor["data"],
        }}
        sessionId="s1"
        client={mockClient}
        catalog={[]}
      />,
    );
    const banner = screen.getByTestId("unsupported-view-error");
    expect(banner).not.toBeEmptyDOMElement();
    expect(banner).toHaveTextContent("invalid_mystery_view");
  });
});
