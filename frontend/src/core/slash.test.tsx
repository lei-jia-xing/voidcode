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
  return { rpcMock, invokes, unmount };
}

async function submitSlash(harness: Harness, text: string) {
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
        owner: "delegation",
        name: "agents",
        description: "List profiles",
        argument_schema: { type: "object", properties: {} },
      },
      {
        owner: "delegation",
        name: "agents.show",
        description: "Inspect a profile",
        argument_schema: { type: "object", properties: {} },
      },
    ]);
    fireEvent.change(screen.getByTestId("prompt-input"), {
      target: { value: "/ag" },
    });
    const suggestions = await screen.findByTestId("slash-suggestions");
    expect(suggestions).toHaveTextContent("List profiles");
    expect(suggestions).not.toHaveTextContent("/agents show");
    fireEvent.click(
      screen.getByRole("button", { name: /agents.*List profiles/ }),
    );
    expect(screen.getByTestId("prompt-input")).toHaveValue("/agents ");
    expect(screen.getByTestId("slash-suggestions")).toHaveTextContent(
      "/agents show",
    );
    expect(screen.getByTestId("slash-suggestions")).toHaveTextContent(
      "Inspect a profile",
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
        { owner: "delegation", name: "agents", description: "Profiles" },
        { owner: "delegation", name: "agents.show", description: "Inspect" },
      ],
      {
        status: "ok",
        output: "Profiles",
        presentation: {
          title: "Profiles",
          view_type: "choices",
          data: {
            options: [{ id: "explore", label: "Explore" }],
            action: "agents.show",
            argument_name: "name",
          },
        },
      },
    );
    await submitSlash(h, "/agents");
    fireEvent.click(await screen.findByTestId("choice-option-explore"));
    await waitFor(() => expect(h.invokes).toHaveLength(2));
    expect(h.invokes[1].params).toMatchObject({
      name: "agents.show",
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
          owner: "delegation",
          name: "agents.show",
          description: "Inspect profile",
          has_input: true,
        },
      ],
      { status: "ok", output: "Selected" },
      {
        revision_id: "rev-1",
        presentation: {
          title: "Choose agent",
          view_type: "form",
          data: {
            action: "agents.show",
            fields: [
              {
                name: "name",
                label: "Agent",
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
    await submitSlash(h, "/agents show");
    expect(
      await screen.findByRole("combobox", { name: /Agent/ }),
    ).toBeVisible();
    expect(h.invokes).toHaveLength(0);
    expect(h.rpcMock).toHaveBeenCalledWith("command.input", {
      session_id: SESSION,
      name: "agents.show",
      expected_revision: "rev-1",
    });
    fireEvent.change(screen.getByTestId("form-input-name"), {
      target: { value: "review" },
    });
    fireEvent.click(screen.getByTestId("form-submit-btn"));
    await waitFor(() => expect(h.invokes).toHaveLength(1));
    expect(h.invokes[0].params).toMatchObject({
      name: "agents.show",
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
});
