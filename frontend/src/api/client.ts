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

export function apiError(response: Response, error: unknown): ApiError {
  const detail =
    typeof error === "object" && error !== null && "detail" in error && typeof error.detail === "string"
      ? error.detail
      : response.statusText;
  return new ApiError(response.status, detail);
}
