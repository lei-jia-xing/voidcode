import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, afterEach, beforeEach } from "vitest";
import { CoreApp } from "./CoreApp";
import "../i18n";
import {
  installRpcPeer,
  frameSource,
  type MockResponse,
} from "../test/rpc-peer";

if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = () => {};
}

const SESSION = "token-session";
const TOKEN = "pasted-bearer-token";
const STORAGE_KEY = "voidcode:bearer:same-origin";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function handler(method: string): Promise<MockResponse> {
  if (method === "command.catalog") {
    return Promise.resolve(
      jsonResponse({ revision_id: "rev-1", commands: [] }),
    );
  }
  if (method === "session.views") {
    return Promise.resolve(jsonResponse({ revision_id: "rev-1", views: [] }));
  }
  if (method === "session.subscribe") {
    return Promise.resolve(
      frameSource(
        [JSON.stringify({ session: { session_id: SESSION }, events: [] })],
        [],
      ),
    );
  }
  if (method === "health.get") {
    return Promise.resolve(jsonResponse({ status: "ok", fenced: false }));
  }
  if (method.startsWith("session.")) {
    return Promise.resolve(
      jsonResponse({
        sessions: [
          {
            session_id: SESSION,
            workspace: "/tmp/ws",
            status: "idle",
            active_revision_id: "rev-1",
            run_id: "run-1",
            turn: 1,
            last_event_sequence: 0,
          },
        ],
      }),
    );
  }
  return Promise.resolve(jsonResponse({}));
}

describe("bearer token gate", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
    window.history.replaceState(null, "", "/");
  });

  afterEach(() => {
    window.sessionStorage.clear();
    window.history.replaceState(null, "", "/");
    vi.unstubAllGlobals();
  });

  it("prompts for a pasted token without blocking the app", async () => {
    installRpcPeer(handler);
    const { unmount } = render(<CoreApp baseUrl="" />);
    try {
      expect(await screen.findByTestId("token-gate")).toBeVisible();
      // The app still bootstraps underneath (the mock peer accepts null auth).
      expect(await screen.findByTestId(`session-tab-${SESSION}`)).toBeVisible();
      expect(window.location.href).not.toContain("token=");
    } finally {
      unmount();
    }
  });

  it("connects with the pasted token and persists it per tab", async () => {
    installRpcPeer(handler);
    const { unmount } = render(<CoreApp baseUrl="" />);
    try {
      fireEvent.change(await screen.findByTestId("token-gate-input"), {
        target: { value: TOKEN },
      });
      fireEvent.click(screen.getByTestId("token-gate-submit"));
      await waitFor(() => {
        expect(screen.queryByTestId("token-gate")).not.toBeInTheDocument();
      });
      expect(window.sessionStorage.getItem(STORAGE_KEY)).toBe(TOKEN);
      expect(window.location.href).not.toContain(TOKEN);
      expect(await screen.findByTestId(`session-tab-${SESSION}`)).toBeVisible();
    } finally {
      unmount();
    }
  });

  it("reuses the stored token on the next load", async () => {
    window.sessionStorage.setItem(STORAGE_KEY, TOKEN);
    installRpcPeer(handler);
    const { unmount } = render(<CoreApp baseUrl="" />);
    try {
      expect(await screen.findByTestId(`session-tab-${SESSION}`)).toBeVisible();
      expect(screen.queryByTestId("token-gate")).not.toBeInTheDocument();
    } finally {
      unmount();
    }
  });

  it("never reads a token from the URL query string", async () => {
    window.history.replaceState(null, "", "/?token=query-leak-token");
    installRpcPeer(handler);
    const { unmount } = render(<CoreApp baseUrl="" />);
    try {
      // The query token is ignored: the gate still prompts.
      expect(await screen.findByTestId("token-gate")).toBeVisible();
      expect(window.sessionStorage.getItem(STORAGE_KEY)).toBeNull();
    } finally {
      unmount();
    }
  });

  it("an explicit token prop skips the gate", async () => {
    installRpcPeer(handler);
    const { unmount } = render(<CoreApp baseUrl="" token={TOKEN} />);
    try {
      expect(await screen.findByTestId(`session-tab-${SESSION}`)).toBeVisible();
      expect(screen.queryByTestId("token-gate")).not.toBeInTheDocument();
    } finally {
      unmount();
    }
  });
});
