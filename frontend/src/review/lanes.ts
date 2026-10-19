// The review queue's lanes (slice 17) and which items count as work left: shared by the Review
// page's tally and the report's pending badge, so the two never show different numbers.
import type { ReviewItem } from "../api/review";

export type LaneId = "accounts" | "payees" | "categories" | "checks";
export const LANES: { id: LaneId; name: string }[] = [
  { id: "accounts", name: "Accounts" }, { id: "categories", name: "Categories" },
  { id: "payees", name: "Payees" }, { id: "checks", name: "Checks" },
];
export const OPTIONAL: LaneId = "payees"; // not counted as left, not marked
const CHECK_TAGS = new Set(["parse", "large", "transfer", "statement", "other"]);

export function inLane(lane: LaneId, i: ReviewItem): boolean {
  if (lane === "accounts") return i.tags.includes("account");
  if (lane === "payees") return i.tags.includes("payee");
  if (lane === "categories") return i.tags.includes("uncategorized");
  return i.tags.some((t) => CHECK_TAGS.has(t));
}

/** What's left to sort: every lane but the optional Payees lane. */
export const countLeft = (items: ReviewItem[]) =>
  items.filter((i) => LANES.some((l) => l.id !== OPTIONAL && inLane(l.id, i))).length;
