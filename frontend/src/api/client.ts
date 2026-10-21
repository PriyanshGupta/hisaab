// The typed API client. `paths` and `components` come from src/api/schema.d.ts,
// generated from FastAPI's OpenAPI schema on every build (npm run gen:api). A
// renamed field in a Pydantic response model therefore breaks `tsc` here,
// at the call site, instead of surfacing as `undefined` in the browser.
import createClient from "openapi-fetch";

import type { components, paths } from "./schema";

export type SessionUser = components["schemas"]["SessionUser"];

/** The server answered, but not with success. `status` drives what the UI says. */
export class ApiError extends Error {
  readonly status: number;
  constructor(status: number, detail: string) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
  }
}

/** The request never got an answer: server stopped, laptop asleep, Wi-Fi gone. */
export class NetworkError extends Error {
  constructor() {
    super("Network request failed");
    this.name = "NetworkError";
  }
}

export const api = createClient<paths>({
  // Same origin in production and in dev (Vite proxies /api), so the session
  // cookie needs no handling here. An absolute base also works under jsdom.
  baseUrl: window.location.origin,
  // Looked up per call rather than captured at import, so tests can stub fetch.
  fetch: (request: Request) => globalThis.fetch(request),
});

/** Run an openapi-fetch call, turning transport failures into NetworkError. */
export async function send<T extends { response: Response }>(call: () => Promise<T>): Promise<T> {
  try {
    return await call();
  } catch (error) {
    if (error instanceof TypeError) throw new NetworkError();
    throw error;
  }
}

/** The response's data, or the server's error as an ApiError (a dropped connection as NetworkError). */
export async function unwrap<T>(call: () => Promise<{ data?: T; error?: unknown; response: Response }>): Promise<T> {
  const { data, error, response } = await send(call);
  if (data === undefined) throw apiError(response, error);
  return data;
}

/**
 * The server's own sentence when it sent one ({"detail": "You already have…"}).
 * FastAPI's field-validation errors carry a list instead; those become one
 * generic sentence, since the form has already checked what it can.
 */
export function apiError(response: Response, error: unknown): ApiError {
  const detail = typeof error === "object" && error !== null && "detail" in error ? error.detail : undefined;
  if (typeof detail === "string") return new ApiError(response.status, detail);
  if (Array.isArray(detail)) return new ApiError(response.status, "Some details aren't valid. Check the form and try again.");
  return new ApiError(response.status, response.statusText || `Request failed (${response.status})`);
}

/** A sentence to show the person for any failed request. */
export function describeError(error: unknown): string {
  if (error instanceof NetworkError) return "Can't reach the Hisaab server. Check that it's running, then try again.";
  if (error instanceof ApiError) {
    if (error.status === 503) return "Hisaab is busy finishing a long job. Try again in a minute.";
    return error.message;
  }
  return "Something went wrong. Try again.";
}
