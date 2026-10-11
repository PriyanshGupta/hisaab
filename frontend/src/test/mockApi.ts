// A stand-in for the backend in component tests: stubs global fetch with
// canned responses keyed by "METHOD /path", and records every call made.
import { vi } from "vitest";

export type Reply = { status: number; body?: unknown } | "network-error";

export function mockApi(initial: Record<string, Reply>) {
  const routes = new Map(Object.entries(initial));
  const calls: string[] = [];

  vi.stubGlobal("fetch", async (input: Request) => {
    const key = `${input.method} ${new URL(input.url).pathname}`;
    calls.push(key);
    const reply = routes.get(key);
    if (reply === undefined) throw new Error(`mockApi: no reply for ${key}`);
    if (reply === "network-error") throw new TypeError("Failed to fetch");
    const hasBody = reply.status !== 204 && reply.body !== undefined;
    return new Response(hasBody ? JSON.stringify(reply.body) : null, {
      status: reply.status,
      headers: hasBody ? { "content-type": "application/json" } : {},
    });
  });

  return {
    calls,
    set(key: string, reply: Reply) {
      routes.set(key, reply);
    },
  };
}
