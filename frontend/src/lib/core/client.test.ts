import { afterEach, describe, expect, it, vi } from "vitest";
import { CoreApiError, CoreClient } from "./client";
import {
  frameSource,
  installRpcPeer,
  type MockResponse,
} from "../../test/rpc-peer";

const json = (body: object) => new Response(JSON.stringify(body));
afterEach(() => vi.unstubAllGlobals());

describe("WebSocket RPC client", () => {
  it("reports a lost mutation response without resending on reconnect", async () => {
    let finish: (response: MockResponse) => void = () => {};
    const handler = vi.fn(async (method: string): Promise<MockResponse> => {
      if (method === "command.invoke")
        return new Promise((resolve) => {
          finish = resolve;
        });
      return json({ revision_id: "revision", commands: [] });
    });
    const peer = installRpcPeer(handler);
    const client = new CoreClient();
    const mutation = client.invoke("session", "write", {}, "revision");
    await vi.waitFor(() =>
      expect(handler).toHaveBeenCalledWith("command.invoke", expect.anything()),
    );
    peer.drop();
    await expect(mutation).rejects.toMatchObject({ code: "outcome_unknown" });
    finish(json({ status: "ok" }));
    expect(await client.commandCatalog()).toEqual({
      revisionId: "revision",
      commands: [],
    });
    expect(
      handler.mock.calls.filter(([method]) => method === "command.invoke"),
    ).toHaveLength(1);
    client.close();
  });

  it("can be reused after closing an in-progress authentication", async () => {
    installRpcPeer(async () => json({ revision_id: "revision", commands: [] }));
    const client = new CoreClient();
    const first = client.commandCatalog();
    client.close();
    await expect(first).rejects.toBeInstanceOf(CoreApiError);
    expect((await client.commandCatalog()).revisionId).toBe("revision");
    client.close();
  });

  it("stops on a malformed event instead of reconnecting past it", async () => {
    let subscriptions = 0;
    installRpcPeer(async () => {
      subscriptions++;
      return frameSource(
        [
          JSON.stringify({
            kind: "event",
            session_id: "s",
            event: { sequence: "bad" },
          }),
        ],
        [],
      );
    });
    const client = new CoreClient();
    const errors: Error[] = [];
    const applied = vi.fn();
    const stop = client.streamEvents("s", 0, applied, (error) =>
      errors.push(error),
    );
    await vi.waitFor(() => expect(errors).toHaveLength(1));
    expect(errors[0]).toMatchObject({ code: "protocol_error" });
    expect(applied).not.toHaveBeenCalled();
    await new Promise((resolve) => setTimeout(resolve, 1100));
    expect(subscriptions).toBe(1);
    stop();
    client.close();
  });
});
