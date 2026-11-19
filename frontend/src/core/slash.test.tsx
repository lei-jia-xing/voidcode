import {
  render,
  screen,
  fireEvent,
  waitFor,
  within,
  act,
} from "@testing-library/react";
import { describe, it, expect, vi, afterEach, type Mock } from "vitest";
import { CoreApp } from "./CoreApp";
import "../i18n";
import {
  installRpcPeer,
  frameSource,
  type MockResponse,
} from "../test/rpc-peer";

const SESSION = "slash-session";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function storedEvent(sequence: number, type: string, payload: object) {
  return {
    sequence,
    session_id: SESSION,
    revision_id: "rev-1",
    type,
    payload,
    transcript: null,
  };
}

interface Harness {
  rpcMock: Mock;
  invokes: Array<{ method: string; params: Record<string, unknown> }>;
  unmount: () => void;
  settled: boolean;
}

async function mount(
  commands: unknown[],
  invokeResult: object = { status: "ok", output: "done" },
  inputResult: object = {},
): Promise<Harness> {
  const invokes: Harness["invokes"] = [];
  const head = JSON.stringify({
    session: { session_id: SESSION },
    events: [],
  });
  const rpcMock = vi.fn(
    async (
      method: string,
      params: Record<string, unknown>,
    ): Promise<MockResponse> => {
      if (method === "command.input") return jsonResponse(inputResult);
      if (method === "command.invoke") {
        invokes.push({ method, params });
        return jsonResponse(invokeResult);
      }
      if (method === "command.catalog") {
        return jsonResponse({ revision_id: "rev-1", commands });
      }
      if (method === "session.views") {
        return jsonResponse({ revision_id: "rev-1", views: [] });
      }
      if (method === "session.subscribe") {
        return frameSource([head], []);
      }
      if (method === "health.get") {
        return jsonResponse({ status: "ok", fenced: false });
      }
      if (method === "session.run") {
        invokes.push({ method, params });
        return jsonResponse({ status: "ok" });
      }
      if (method === "session.show") {
        return jsonResponse({
          session_id: SESSION,
          status: "idle",
          run_id: null,
          revision_id: "rev-1",
          turn: 1,
          last_event_sequence: 0,
          checkpoint: null,
        });
      }
      return jsonResponse({
        sessions: [
          {
            session_id: SESSION,
            workspace: "/tmp/ws",
            status: "idle",
            active_revision_id: "rev-1",
            run_id: null,
            turn: 1,
            last_event_sequence: 0,
          },
        ],
      });
    },
  );
  installRpcPeer(rpcMock);
  const { unmount } = render(<CoreApp baseUrl="" />);
  await waitFor(() => {
    expect(screen.getByTestId(`session-tab-${SESSION}`)).toBeVisible();
  });
  return { rpcMock, invokes, unmount, settled: false };
}

async function submitSlash(harness: Harness, text: string) {
  if (!harness.settled) {
    // Drain the mount race before the first submit: the initial reconcile
    // sets "Connected" once its health resolves, and a submit that lands
    // first would have its status clobbered. Later submits need no wait:
    // nothing else reconciles spontaneously.
    await waitFor(() => {
      expect(screen.getByText(/Connected to session/)).toBeVisible();
    });
    harness.settled = true;
  }
  fireEvent.change(screen.getByTestId("prompt-input"), {
    target: { value: text },
  });
  fireEvent.click(screen.getByTestId("send-btn"));
  await waitFor(() => {
    expect(harness.rpcMock).toHaveBeenCalledWith(
      "command.catalog",
      expect.anything(),
    );
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});
if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = () => {};
}

const GOAL_COMMANDS = [
  {
    owner: "goal",
    name: "goal",
    description: "Show goal",
    argument_schema: { type: "object", properties: {} },
  },
  {
    owner: "goal",
    name: "goal.pause",
    description: "Pause goal",
    argument_schema: { type: "object", properties: {} },
  },
];

describe("spaced slash dispatch", () => {
  it("invokes the canonical id of the longest public path", async () => {
    const h = await mount(GOAL_COMMANDS);
    await submitSlash(h, "/goal pause");
    await waitFor(() => {
      expect(h.invokes).toHaveLength(1);
    });
    expect(h.invokes[0].method).toBe("command.invoke");
    expect(h.invokes[0].params.name).toBe("goal.pause");
    expect(h.invokes[0].params.arguments).toEqual({});
    expect(h.invokes[0].params.expected_revision).toBe("rev-1");
    h.unmount();
  });

  it("invokes the parent when only the parent path matches", async () => {
    const h = await mount(GOAL_COMMANDS);
    await submitSlash(h, "/goal");
    await waitFor(() => {
      expect(h.invokes).toHaveLength(1);
    });
    expect(h.invokes[0].params.name).toBe("goal");
    h.unmount();
  });

  it("rejects legacy dotted input without invoking", async () => {
    const h = await mount(GOAL_COMMANDS);
    await submitSlash(h, "/goal.pause");
    await waitFor(() => {
      expect(screen.getByText(/Unknown command/)).toBeVisible();
    });
    expect(h.invokes).toHaveLength(0);
    h.unmount();
  });

  it("keeps JSON args with dots verbatim", async () => {
    const commands = [
      {
        owner: "workflow",
        name: "workflow.status",
        description: "Status",
        client_syntax: "/workflow status",
        argument_schema: {
          type: "object",
          properties: {
            run_id: { type: "string" },
            verbose: { type: "boolean" },
          },
        },
      },
    ];
    const h = await mount(commands);
    await submitSlash(h, '/workflow status {"run_id":"a.b", "verbose": true}');
    await waitFor(() => {
      expect(h.invokes).toHaveLength(1);
    });
    expect(h.invokes[0].params.name).toBe("workflow.status");
    expect(h.invokes[0].params.arguments).toEqual({
      run_id: "a.b",
      verbose: true,
    });
    h.unmount();
  });

  it("maps freeform text to the single required string property", async () => {
    const commands = [
      {
        owner: "cron",
        name: "loop",
        description: "Loop",
        client_syntax: "/loop",
        argument_schema: {
          type: "object",
          properties: { input: { type: "string" } },
          required: ["input"],
        },
      },
    ];
    const h = await mount(commands);
    await submitSlash(h, "/loop 10m do the thing.example.com/path");
    await waitFor(() => {
      expect(h.invokes).toHaveLength(1);
    });
    expect(h.invokes[0].params.name).toBe("loop");
    expect(h.invokes[0].params.arguments).toEqual({
      input: "10m do the thing.example.com/path",
    });
    h.unmount();
  });

  it("fails visibly on an ambiguous public path without invoking", async () => {
    const commands = [
      {
        owner: "a",
        name: "foo.bar",
        description: "dotted",
        argument_schema: { type: "object", properties: {} },
      },
      {
        owner: "b",
        name: "foo bar",
        description: "spaced",
        argument_schema: { type: "object", properties: {} },
      },
    ];
    const h = await mount(commands);
    await submitSlash(h, "/foo bar");
    await waitFor(() => {
      expect(screen.getByText(/Unknown command/)).toBeVisible();
    });
    expect(h.invokes).toHaveLength(0);
    h.unmount();
  });

  it("normalizes a dotted preferred prefix without accepting extra aliases", async () => {
    const commands = [
      {
        owner: "w",
        name: "workflow.status",
        description: "Status",
        client_syntax: "/workflow.inspect",
        argument_schema: { type: "object", properties: {} },
      },
    ];
    const h = await mount(commands);
    for (const input of ["/workflow.inspect", "/workflow status"]) {
      await submitSlash(h, input);
      await waitFor(() => {
        expect(screen.getByText(/Unknown command/)).toBeVisible();
      });
      expect(h.invokes).toHaveLength(0);
    }
    await submitSlash(h, "/workflow inspect");
    await waitFor(() => {
      expect(h.invokes).toHaveLength(1);
    });
    expect(h.invokes[0].params.name).toBe("workflow.status");
    h.unmount();
  });

  it("leaves normal prompts on the run path", async () => {
    const h = await mount(GOAL_COMMANDS);
    fireEvent.change(screen.getByTestId("prompt-input"), {
      target: { value: "hello world" },
    });
    fireEvent.click(screen.getByTestId("send-btn"));
    await waitFor(() => {
      expect(h.invokes).toHaveLength(1);
    });
    expect(h.invokes[0].method).toBe("session.run");
    expect(h.invokes[0].params.prompt).toBe("hello world");
    h.unmount();
  });

  it("suggests spaced public paths, not dotted ids", async () => {
    const commands = [
      {
        owner: "goal",
        name: "goal.pause",
        description: "Pause goal",
        argument_schema: { type: "object", properties: {} },
      },
      {
        owner: "w",
        name: "workflow.status",
        description: "Status",
        client_syntax: "/workflow status",
        argument_schema: { type: "object", properties: {} },
      },
    ];
    const h = await mount(commands);
    fireEvent.change(screen.getByTestId("prompt-input"), {
      target: { value: "/goal" },
    });
    await waitFor(() => {
      expect(screen.getByTestId("slash-suggestions")).toBeVisible();
    });
    expect(screen.getByTestId("slash-suggestions")).toHaveTextContent("/goal");
    expect(screen.getByTestId("slash-suggestions")).not.toHaveTextContent(
      "/goal pause",
    );
    expect(screen.getByTestId("slash-suggestions")).not.toHaveTextContent(
      "goal.pause",
    );
    fireEvent.change(screen.getByTestId("prompt-input"), {
      target: { value: "/workflow " },
    });
    await waitFor(() => {
      expect(screen.getByTestId("slash-suggestions")).toHaveTextContent(
        "/workflow status",
      );
    });
    h.unmount();
  });

  it("groups parent commands and reveals described children after selection", async () => {
    const h = await mount([
      {
        owner: "skills",
        name: "skills",
        description: "List skills",
        argument_schema: { type: "object", properties: {} },
      },
      {
        owner: "skills",
        name: "skills.show",
        description: "Inspect a skill",
        argument_schema: { type: "object", properties: {} },
      },
    ]);
    fireEvent.change(screen.getByTestId("prompt-input"), {
      target: { value: "/sk" },
    });
    const suggestions = await screen.findByTestId("slash-suggestions");
    expect(suggestions).toHaveTextContent("List skills");
    expect(suggestions).not.toHaveTextContent("/skills show");
    fireEvent.click(
      screen.getByRole("button", { name: /skills.*List skills/ }),
    );
    expect(screen.getByTestId("prompt-input")).toHaveValue("/skills ");
    expect(screen.getByTestId("slash-suggestions")).toHaveTextContent(
      "/skills show",
    );
    expect(screen.getByTestId("slash-suggestions")).toHaveTextContent(
      "Inspect a skill",
    );
    h.unmount();
  });

  it("opens a required-argument form and submits typed values under the displayed revision", async () => {
    const h = await mount([
      {
        owner: "goal",
        name: "goal.set",
        description: "Set goal",
        argument_schema: {
          type: "object",
          properties: {
            objective: { type: "string" },
            token_budget: { type: "integer" },
          },
          required: ["objective"],
        },
      },
    ]);
    await submitSlash(h, "/goal set");
    expect(await screen.findByTestId("command-presentation")).toBeVisible();
    expect(h.invokes).toHaveLength(0);
    fireEvent.change(screen.getByTestId("form-input-objective"), {
      target: { value: "Ship it" },
    });
    fireEvent.change(screen.getByTestId("form-input-token_budget"), {
      target: { value: "3000" },
    });
    fireEvent.click(screen.getByTestId("form-submit-btn"));
    await waitFor(() => expect(h.invokes).toHaveLength(1));
    expect(h.invokes[0].params).toMatchObject({
      name: "goal.set",
      arguments: { objective: "Ship it", token_budget: 3000 },
      expected_revision: "rev-1",
    });
    h.unmount();
  });

  it("renders command choices and dispatches their action with the receipt revision", async () => {
    const h = await mount(
      [
        { owner: "skills", name: "skills", description: "Skills" },
        { owner: "skills", name: "skills.show", description: "Inspect" },
      ],
      {
        status: "ok",
        output: "Profiles",
        presentation: {
          title: "Profiles",
          view_type: "choices",
          data: {
            options: [{ id: "explore", label: "Explore" }],
            action: "skills.show",
            argument_name: "name",
          },
        },
      },
    );
    await submitSlash(h, "/skills");
    fireEvent.click(await screen.findByTestId("choice-option-explore"));
    await waitFor(() => expect(h.invokes).toHaveLength(2));
    expect(h.invokes[1].params).toMatchObject({
      name: "skills.show",
      arguments: { name: "explore" },
      expected_revision: "rev-1",
    });
    h.unmount();
  });

  it("preserves the editor and shows the server validation error", async () => {
    const h = await mount(
      [
        {
          owner: "goal",
          name: "goal.set",
          description: "Set goal",
          argument_schema: {
            type: "object",
            properties: { objective: { type: "string" } },
            required: ["objective"],
          },
        },
      ],
      { status: "error", error: "Goal cannot be replaced" },
    );
    await submitSlash(h, "/goal set");
    fireEvent.change(await screen.findByTestId("form-input-objective"), {
      target: { value: "Keep my draft" },
    });
    fireEvent.click(screen.getByTestId("form-submit-btn"));
    await waitFor(() =>
      expect(screen.getByText(/Goal cannot be replaced/)).toBeVisible(),
    );
    expect(screen.getByTestId("form-input-objective")).toHaveValue(
      "Keep my draft",
    );
    expect(screen.getByTestId("command-presentation")).toBeVisible();
    h.unmount();
  });

  it("does not reopen a dismissed editor from a late command receipt", async () => {
    const h = await mount([
      {
        owner: "goal",
        name: "goal.set",
        description: "Set goal",
        argument_schema: {
          type: "object",
          properties: { objective: { type: "string" } },
          required: ["objective"],
        },
      },
    ]);
    const original = h.rpcMock.getMockImplementation()!;
    let reply: ((response: MockResponse) => void) | undefined;
    h.rpcMock.mockImplementation(
      (method: string, params: Record<string, unknown>) => {
        if (method === "command.invoke")
          return new Promise<MockResponse>((resolve) => {
            reply = resolve;
          });
        return original(method, params);
      },
    );
    await submitSlash(h, "/goal set");
    fireEvent.change(await screen.findByTestId("form-input-objective"), {
      target: { value: "Draft" },
    });
    fireEvent.click(screen.getByTestId("form-submit-btn"));
    await waitFor(() => expect(reply).toBeDefined());
    fireEvent.click(
      within(screen.getByTestId("command-presentation")).getAllByRole(
        "button",
        { name: "Cancel" },
      )[0],
    );
    expect(screen.queryByTestId("command-presentation")).toBeNull();
    await act(async () => {
      reply!(
        jsonResponse({
          status: "ok",
          presentation: {
            title: "Late",
            view_type: "notice",
            data: { message: "Late reply" },
          },
        }),
      );
    });
    await waitFor(() =>
      expect(screen.getByTestId("prompt-input")).not.toBeDisabled(),
    );
    expect(screen.queryByTestId("command-presentation")).toBeNull();
    h.unmount();
  });

  it("prepares a dynamic selector without invoking and submits the selected value", async () => {
    const h = await mount(
      [
        {
          owner: "skills",
          name: "skills.show",
          description: "Inspect skill",
          has_input: true,
        },
      ],
      { status: "ok", output: "Selected" },
      {
        revision_id: "rev-1",
        presentation: {
          title: "Choose skill",
          view_type: "form",
          data: {
            action: "skills.show",
            fields: [
              {
                name: "name",
                label: "Skill",
                options: [
                  { id: "explore", label: "Explore workspace" },
                  { id: "review", label: "Review changes" },
                ],
              },
            ],
          },
        },
      },
    );
    await submitSlash(h, "/skills show");
    expect(
      await screen.findByRole("combobox", { name: /Skill/ }),
    ).toBeVisible();
    expect(h.invokes).toHaveLength(0);
    expect(h.rpcMock).toHaveBeenCalledWith("command.input", {
      session_id: SESSION,
      name: "skills.show",
      arguments: {},
      expected_revision: "rev-1",
    });
    fireEvent.change(screen.getByTestId("form-input-name"), {
      target: { value: "review" },
    });
    fireEvent.click(screen.getByTestId("form-submit-btn"));
    await waitFor(() => expect(h.invokes).toHaveLength(1));
    expect(h.invokes[0].params).toMatchObject({
      name: "skills.show",
      arguments: { name: "review" },
      expected_revision: "rev-1",
    });
    h.unmount();
  });

  it("rejects a prepared input returned under another revision", async () => {
    const h = await mount(
      [
        {
          owner: "w",
          name: "workflow.run",
          description: "Run",
          has_input: true,
        },
      ],
      { status: "ok" },
      {
        revision_id: "old-revision",
        presentation: {
          title: "Stale",
          view_type: "notice",
          data: { message: "Old" },
        },
      },
    );
    await submitSlash(h, "/workflow run");
    await waitFor(() =>
      expect(screen.getByText(/changed revision/)).toBeVisible(),
    );
    expect(screen.queryByTestId("command-presentation")).toBeNull();
    expect(h.invokes).toHaveLength(0);
    h.unmount();
  });

  it("passes declared natural text to one field and retains structured JSON arguments", async () => {
    const h = await mount([
      {
        owner: "goal",
        name: "goal.set",
        description: "Set goal",
        text_argument: "objective",
        argument_schema: {
          type: "object",
          properties: {
            objective: { type: "string" },
            token_budget: { type: "integer" },
          },
          required: ["objective"],
        },
      },
    ]);
    await submitSlash(h, "/goal set Ship the command UI");
    await waitFor(() => expect(h.invokes).toHaveLength(1));
    expect(h.invokes[0].params.arguments).toEqual({
      objective: "Ship the command UI",
    });
    await submitSlash(
      h,
      '/goal set {"objective":"Structured","token_budget":2000}',
    );
    await waitFor(() => expect(h.invokes).toHaveLength(2));
    expect(h.invokes[1].params.arguments).toEqual({
      objective: "Structured",
      token_budget: 2000,
    });
    h.unmount();
  });

  it("opens an input action from details and submits fixed revision arguments", async () => {
    const h = await mount(
      [
        { owner: "todo", name: "todo", description: "List tasks" },
        {
          owner: "todo",
          name: "todo.add",
          description: "Add tasks",
          has_input: true,
        },
      ],
      {
        status: "ok",
        presentation: {
          title: "Todo list",
          view_type: "markdown",
          data: {
            content: "Empty list",
            actions: [
              { label: "Add tasks", action: "todo.add", prepare_input: true },
            ],
          },
        },
      },
      {
        revision_id: "rev-1",
        presentation: {
          title: "Add tasks",
          view_type: "form",
          data: {
            action: "todo.add",
            arguments: { expected_revision: 7 },
            fields: [
              { name: "items", label: "Tasks", field_type: "multiline" },
            ],
          },
        },
      },
    );
    await submitSlash(h, "/todo");
    fireEvent.click(await screen.findByRole("button", { name: "Add tasks" }));
    const input = await screen.findByTestId("form-input-items");
    expect(h.invokes).toHaveLength(1);
    expect(screen.queryByTestId("form-input-expected_revision")).toBeNull();
    fireEvent.change(input, { target: { value: "First\nSecond" } });
    fireEvent.click(screen.getByTestId("form-submit-btn"));
    await waitFor(() => expect(h.invokes).toHaveLength(2));
    expect(h.invokes[1].params).toMatchObject({
      name: "todo.add",
      arguments: { items: "First\nSecond", expected_revision: 7 },
      expected_revision: "rev-1",
    });
    h.unmount();
  });

  it("hides helper commands from discovery while preserving explicit invocation", async () => {
    const h = await mount([
      { owner: "p", name: "visible", description: "Visible" },
      {
        owner: "p",
        name: "internal.only",
        description: "Helper",
        slash_visible: false,
      },
    ]);
    fireEvent.change(screen.getByTestId("prompt-input"), {
      target: { value: "/" },
    });
    expect(
      await screen.findByTestId("slash-suggestions"),
    ).not.toHaveTextContent("internal");
    await submitSlash(h, "/internal only");
    await waitFor(() => expect(h.invokes).toHaveLength(1));
    expect(h.invokes[0].params.name).toBe("internal.only");
    h.unmount();
  });

  it("uses keyboard completion to enter namespaces without dispatching", async () => {
    const h = await mount([
      { owner: "goal", name: "goal.pause", description: "Pause" },
    ]);
    const input = screen.getByTestId("prompt-input");
    fireEvent.change(input, { target: { value: "/go" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(input).toHaveValue("/goal ");
    expect(h.invokes).toHaveLength(0);
    fireEvent.keyDown(input, { key: "Tab" });
    expect(input).toHaveValue("/goal pause ");
    fireEvent.change(input, { target: { value: "/goal" } });
    fireEvent.keyDown(input, { key: "Escape" });
    expect(screen.queryByTestId("slash-suggestions")).toBeNull();
    expect(input).toHaveValue("/goal");
    h.unmount();
  });

  it("renders the human path on command rows", async () => {
    const head = JSON.stringify({
      session: { session_id: SESSION },
      events: [
        storedEvent(7, "core.command_completed", {
          name: "goal.pause",
          result: { status: "ok", output: "paused" },
        }),
      ],
    });
    const rpcMock = vi.fn(async (method: string): Promise<MockResponse> => {
      if (method === "command.catalog") {
        return jsonResponse({ revision_id: "rev-1", commands: GOAL_COMMANDS });
      }
      if (method === "session.views") {
        return jsonResponse({ revision_id: "rev-1", views: [] });
      }
      if (method === "session.subscribe") {
        return frameSource([head], []);
      }
      if (method === "health.get") {
        return jsonResponse({ status: "ok", fenced: false });
      }
      return jsonResponse({
        sessions: [
          {
            session_id: SESSION,
            workspace: "/tmp/ws",
            status: "idle",
            active_revision_id: "rev-1",
            run_id: null,
            turn: 1,
            last_event_sequence: 7,
          },
        ],
      });
    });
    installRpcPeer(rpcMock);
    const { unmount } = render(<CoreApp baseUrl="" />);
    await waitFor(() => {
      expect(screen.getByTestId("event-7")).toBeVisible();
    });
    expect(screen.getByTestId("event-7")).toHaveTextContent("/goal pause [ok]");
    expect(screen.getByTestId("event-7")).not.toHaveTextContent("goal.pause");
    unmount();
  });

  it("offers a hidden shortcut action as an accessible button without slash text", async () => {
    const h = await mount(
      [
        {
          owner: "p",
          name: "monitor",
          description: "Running subagents",
          has_input: true,
          slash_visible: false,
          default_shortcut: "alt+a",
        },
      ],
      { status: "ok", output: "done" },
      {
        revision_id: "rev-1",
        presentation: {
          title: "Running subagents",
          view_type: "choices",
          data: {
            options: [{ id: "child-1", label: "child one" }],
            action: "monitor",
            argument_name: "target",
          },
        },
      },
    );
    const button = await screen.findByTestId("shortcut-action-monitor");
    expect(button).toHaveAttribute("aria-label", "Running subagents (alt+a)");
    expect(button).toHaveAttribute("type", "button");
    fireEvent.change(screen.getByTestId("prompt-input"), {
      target: { value: "draft stays" },
    });
    fireEvent.click(button);
    await waitFor(() => {
      expect(screen.getByTestId("command-presentation")).toBeVisible();
    });
    expect(screen.getByTestId("prompt-input")).toHaveValue("draft stays");
    expect(h.rpcMock).toHaveBeenCalledWith(
      "command.input",
      expect.objectContaining({
        name: "monitor",
        arguments: {},
        expected_revision: "rev-1",
      }),
    );
    h.unmount();
  });

  it("shows a shortcut conflict explicitly while keeping both buttons usable", async () => {
    const h = await mount([
      {
        owner: "p",
        name: "one",
        description: "One",
        default_shortcut: "alt+a",
      },
      {
        owner: "q",
        name: "two",
        description: "Two",
        default_shortcut: "alt+a",
      },
    ]);
    await waitFor(() => {
      expect(screen.getByTestId("shortcut-conflict-alt+a")).toBeVisible();
    });
    // No key binds, but both actions stay clickable with conflict labelling.
    expect(screen.getByTestId("shortcut-action-one")).toBeVisible();
    expect(screen.getByTestId("shortcut-action-two")).toBeVisible();
    expect(screen.getByTestId("shortcut-action-one")).toHaveAttribute(
      "aria-label",
      expect.stringContaining("conflict"),
    );
    h.unmount();
  });

  it("keeps a live view refreshing after one failed read", async () => {
    const roster = (options: Array<{ id: string; label: string }>) => ({
      revision_id: "rev-1",
      presentation: {
        title: "Running subagents",
        view_type: "choices",
        data: {
          options,
          action: "monitor",
          argument_name: "target",
          prepare_input: true,
        },
        refresh_after_ms: 50,
      },
    });
    let reads = 0;
    const rpcMock = vi.fn(async (method: string): Promise<MockResponse> => {
      if (method === "command.input") {
        reads += 1;
        if (reads === 2) return jsonResponse({ error: "flaky read" }, 500);
        // The recovery read carries a new node: visible content (not the
        // counter) proves the loop kept going.
        if (reads >= 3)
          return jsonResponse(
            roster([
              { id: "child-1", label: "child one" },
              { id: "child-2", label: "child two arrived" },
            ]),
          );
        return jsonResponse(roster([{ id: "child-1", label: "child one" }]));
      }
      if (method === "command.catalog") {
        return jsonResponse({
          revision_id: "rev-1",
          commands: [
            {
              owner: "p",
              name: "monitor",
              description: "Running subagents",
              has_input: true,
              slash_visible: false,
              default_shortcut: "alt+a",
            },
          ],
        });
      }
      if (method === "session.views") {
        return jsonResponse({ revision_id: "rev-1", views: [] });
      }
      if (method === "session.subscribe") {
        return frameSource(
          [
            JSON.stringify({
              session: { session_id: SESSION },
              events: [],
            }),
          ],
          [],
        );
      }
      if (method === "health.get") {
        return jsonResponse({ status: "ok", fenced: false });
      }
      if (method === "session.show") {
        return jsonResponse({
          session_id: SESSION,
          status: "idle",
          run_id: null,
          revision_id: "rev-1",
          turn: 1,
          last_event_sequence: 0,
          checkpoint: null,
        });
      }
      return jsonResponse({
        sessions: [
          {
            session_id: SESSION,
            workspace: "/tmp/ws",
            status: "idle",
            active_revision_id: "rev-1",
            turn: 1,
            last_event_sequence: 0,
          },
        ],
      });
    });
    installRpcPeer(rpcMock);
    const { unmount } = render(<CoreApp baseUrl="" />);
    await waitFor(() => {
      expect(screen.getByTestId(`session-tab-${SESSION}`)).toBeVisible();
    });
    fireEvent.click(await screen.findByTestId("shortcut-action-monitor"));
    await waitFor(() => {
      expect(screen.getByTestId("command-presentation")).toBeVisible();
    });
    expect(reads).toBe(1);
    // Typing in the prompt bumps the interaction serial mid-poll: the loop
    // must neither freeze nor drop the reply.
    fireEvent.change(screen.getByTestId("prompt-input"), {
      target: { value: "draft during poll" },
    });
    // The failed second read surfaces; the loop reschedules and the new node
    // becomes visible. The counter only gates timing, never the proof.
    await waitFor(() => expect(reads).toBeGreaterThanOrEqual(2), {
      timeout: 5000,
    });
    await waitFor(
      () => {
        expect(screen.getByTestId("choice-option-child-2")).toBeVisible();
      },
      { timeout: 5000 },
    );
    expect(screen.getByTestId("command-presentation")).toHaveTextContent(
      "child two arrived",
    );
    expect(screen.getByTestId("prompt-input")).toHaveValue("draft during poll");
    unmount();
  });

  it("lets a pending poll and a detail navigation resolve without clobbering", async () => {
    const roster = {
      revision_id: "rev-1",
      presentation: {
        title: "Running subagents",
        view_type: "choices",
        data: {
          options: [{ id: "child-1", label: "child one" }],
          action: "monitor",
          argument_name: "target",
          prepare_input: true,
        },
        refresh_after_ms: 50,
      },
    };
    const detail = {
      revision_id: "rev-1",
      presentation: {
        title: "Subagent child-1",
        view_type: "markdown",
        data: {
          content: "child one detail",
          actions: [
            {
              label: "Back to roster",
              action: "monitor",
              arguments: {},
              prepare_input: true,
            },
          ],
        },
        refresh_after_ms: 50,
      },
    };
    const pollGate: { release: ((value: unknown) => void) | null } = {
      release: null,
    };
    let rosterReads = 0;
    const rpcMock = vi.fn(
      async (
        method: string,
        params: Record<string, unknown>,
      ): Promise<MockResponse> => {
        if (method === "command.input") {
          const args = (params.arguments ?? {}) as Record<string, unknown>;
          if ("target" in args) return jsonResponse(detail);
          rosterReads += 1;
          // Hold the first roster *poll* (not the initial open) until
          // navigation has started.
          if (rosterReads === 2 && pollGate.release === null) {
            await new Promise<unknown>((resolve) => {
              pollGate.release = resolve;
            });
          }
          return jsonResponse(roster);
        }
        if (method === "command.catalog") {
          return jsonResponse({
            revision_id: "rev-1",
            commands: [
              {
                owner: "p",
                name: "monitor",
                description: "Running subagents",
                has_input: true,
                slash_visible: false,
                default_shortcut: "alt+a",
              },
            ],
          });
        }
        if (method === "session.views") {
          return jsonResponse({ revision_id: "rev-1", views: [] });
        }
        if (method === "session.subscribe") {
          return frameSource(
            [
              JSON.stringify({
                session: { session_id: SESSION },
                events: [],
              }),
            ],
            [],
          );
        }
        if (method === "health.get") {
          return jsonResponse({ status: "ok", fenced: false });
        }
        if (method === "session.show") {
          return jsonResponse({
            session_id: SESSION,
            status: "idle",
            run_id: null,
            revision_id: "rev-1",
            turn: 1,
            last_event_sequence: 0,
            checkpoint: null,
          });
        }
        return jsonResponse({
          sessions: [
            {
              session_id: SESSION,
              workspace: "/tmp/ws",
              status: "idle",
              active_revision_id: "rev-1",
              run_id: null,
              turn: 1,
              last_event_sequence: 0,
            },
          ],
        });
      },
    );
    installRpcPeer(rpcMock);
    const { unmount } = render(<CoreApp baseUrl="" />);
    await waitFor(() => {
      expect(screen.getByTestId(`session-tab-${SESSION}`)).toBeVisible();
    });
    fireEvent.click(await screen.findByTestId("shortcut-action-monitor"));
    await waitFor(() => {
      expect(screen.getByTestId("command-presentation")).toBeVisible();
    });
    // Navigate to detail while the roster poll is still held: the late
    // roster reply must not overwrite the detail view.
    fireEvent.click(screen.getByTestId("choice-option-child-1"));
    await waitFor(() => {
      expect(screen.getByTestId("command-presentation")).toHaveTextContent(
        "child one detail",
      );
    });
    pollGate.release?.(undefined);
    await waitFor(() => {
      expect(screen.getByTestId("command-presentation")).toHaveTextContent(
        "child one detail",
      );
    });
    unmount();
  });

  it("resumes polling after a failed cancel invoke without losing the detail", async () => {
    const detail = (content: string) => ({
      revision_id: "rev-1",
      presentation: {
        title: "Subagent child-1",
        view_type: "markdown",
        data: {
          content,
          actions: [
            {
              label: "Cancel subtree",
              action: "monitor.cancel",
              arguments: { target: "child-1" },
            },
            {
              label: "Back to roster",
              action: "monitor",
              arguments: {},
              prepare_input: true,
            },
          ],
        },
        refresh_after_ms: 50,
      },
    });
    let polls = 0;
    const rpcMock = vi.fn(
      async (
        method: string,
        params: Record<string, unknown>,
      ): Promise<MockResponse> => {
        if (method === "command.input") {
          const args = (params.arguments ?? {}) as Record<string, unknown>;
          if ("target" in args) {
            polls += 1;
            // Post-failure poll carries fresh content: visible proof the loop
            // resumed instead of freezing on the invoke's serial bump.
            if (polls >= 2) return jsonResponse(detail("child one settled"));
            return jsonResponse(detail("child one live"));
          }
          return jsonResponse({
            revision_id: "rev-1",
            presentation: {
              title: "Running subagents",
              view_type: "choices",
              data: {
                options: [{ id: "child-1", label: "child one" }],
                action: "monitor",
                argument_name: "target",
                prepare_input: true,
              },
              refresh_after_ms: 50,
            },
          });
        }
        if (method === "command.invoke") {
          return jsonResponse({ status: "error", error: "target moved" });
        }
        if (method === "command.catalog") {
          return jsonResponse({
            revision_id: "rev-1",
            commands: [
              {
                owner: "p",
                name: "monitor",
                description: "Running subagents",
                has_input: true,
                slash_visible: false,
                default_shortcut: "alt+a",
              },
              {
                owner: "p",
                name: "monitor.cancel",
                description: "Cancel one subagent",
                has_input: false,
                slash_visible: false,
              },
            ],
          });
        }
        if (method === "session.views") {
          return jsonResponse({ revision_id: "rev-1", views: [] });
        }
        if (method === "session.subscribe") {
          return frameSource(
            [
              JSON.stringify({
                session: { session_id: SESSION },
                events: [],
              }),
            ],
            [],
          );
        }
        if (method === "health.get") {
          return jsonResponse({ status: "ok", fenced: false });
        }
        if (method === "session.show") {
          return jsonResponse({
            session_id: SESSION,
            status: "idle",
            run_id: null,
            revision_id: "rev-1",
            turn: 1,
            last_event_sequence: 0,
            checkpoint: null,
          });
        }
        return jsonResponse({
          sessions: [
            {
              session_id: SESSION,
              workspace: "/tmp/ws",
              status: "idle",
              active_revision_id: "rev-1",
              run_id: null,
              turn: 1,
              last_event_sequence: 0,
            },
          ],
        });
      },
    );
    installRpcPeer(rpcMock);
    const { unmount } = render(<CoreApp baseUrl="" />);
    await waitFor(() => {
      expect(screen.getByTestId(`session-tab-${SESSION}`)).toBeVisible();
    });
    fireEvent.click(await screen.findByTestId("shortcut-action-monitor"));
    await waitFor(() => {
      expect(screen.getByTestId("choice-option-child-1")).toBeVisible();
    });
    fireEvent.click(screen.getByTestId("choice-option-child-1"));
    await waitFor(() => {
      expect(screen.getByTestId("command-presentation")).toHaveTextContent(
        "child one live",
      );
    });
    // The cancel invoke fails (stale target): the view stays, the error
    // surfaces, and the next poll still refreshes the detail content.
    fireEvent.click(screen.getByText("Cancel subtree"));
    await waitFor(() => {
      expect(screen.getByText(/target moved/)).toBeVisible();
    });
    await waitFor(
      () => {
        expect(screen.getByTestId("command-presentation")).toHaveTextContent(
          "child one settled",
        );
      },
      { timeout: 5000 },
    );
    unmount();
  });
});
