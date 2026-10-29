import { render, screen, fireEvent, waitFor } from "@testing-library/react";
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

async function mount(commands: unknown[]): Promise<Harness> {
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
      if (method === "command.invoke") {
        invokes.push({ method, params });
        return jsonResponse({ status: "ok", output: "done" });
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
    expect(screen.getByTestId("slash-suggestions")).toHaveTextContent(
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
