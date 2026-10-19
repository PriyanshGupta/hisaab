// A stand-in for the backend in component tests: stubs global fetch with
// canned responses keyed by "METHOD /path" (query strings ignored), and records
// every call made: `calls` as keys, `requests` with their parsed JSON bodies,
// `urls` in full, query strings included.
import { vi } from "vitest";

export type Reply = { status: number; body?: unknown } | "network-error";

export function mockApi(initial: Record<string, Reply>) {
  const routes = new Map(Object.entries(initial));
  const calls: string[] = [];
  const urls: string[] = [];
  const requests: { key: string; body: unknown }[] = [];

  vi.stubGlobal("fetch", async (input: Request) => {
    const key = `${input.method} ${new URL(input.url).pathname}`;
    calls.push(key);
    urls.push(input.url);
    const text = await input.clone().text();
    requests.push({ key, body: text ? JSON.parse(text) : undefined });
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
    urls,
    requests,
    /** JSON bodies sent to this "METHOD /path", in order. */
    bodiesFor(key: string) {
      return requests.filter((r) => r.key === key).map((r) => r.body);
    },
    set(key: string, reply: Reply) {
      routes.set(key, reply);
    },
  };
}
