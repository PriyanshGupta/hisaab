import { expect, test } from "vitest";

import { fromParams, indiaToday, periodAt, periodTitle, shift, toParams } from "./period";

test("a month runs from the 1st to its last day", () => {
  expect(periodAt("month", "2026-02-14")).toEqual({ unit: "month", start: "2026-02-01", end: "2026-02-28" });
  expect(periodAt("month", "2028-02-03").end).toBe("2028-02-29");
});

test("a week runs Monday to Sunday, across a month end", () => {
  expect(periodAt("week", "2026-10-01")).toEqual({ unit: "week", start: "2026-09-28", end: "2026-10-04" });
  expect(periodAt("week", "2026-10-19").start).toBe("2026-10-19"); // a Monday is its own start
});

test("stepping moves by one whole unit", () => {
  expect(shift(periodAt("month", "2026-01-31"), -1)).toEqual({ unit: "month", start: "2025-12-01", end: "2025-12-31" });
  expect(shift(periodAt("week", "2026-10-01"), 1).start).toBe("2026-10-05");
  expect(shift(periodAt("day", "2026-12-31"), 1).start).toBe("2027-01-01");
  const custom = { unit: "custom" as const, start: "2026-09-01", end: "2026-09-15" };
  expect(shift(custom, 1)).toEqual(custom);
});

test("titles read like a person would say them", () => {
  expect(periodTitle(periodAt("month", "2026-10-05"))).toBe("October 2026");
  expect(periodTitle(periodAt("day", "2026-10-05"))).toBe("5 Oct 2026");
  expect(periodTitle(periodAt("week", "2026-10-14"))).toBe("12 – 18 Oct 2026");
  expect(periodTitle(periodAt("week", "2026-10-01"))).toBe("28 Sep – 4 Oct 2026");
  expect(periodTitle(periodAt("week", "2026-12-30"))).toBe("28 Dec 2026 – 3 Jan 2027");
  expect(periodTitle({ unit: "custom", start: "2026-09-01", end: "2026-10-15" })).toBe("1 Sep – 15 Oct 2026");
});

test("today is India's date, not the browser's", () => {
  expect(indiaToday(new Date("2026-10-31T19:00:00Z"))).toBe("2026-11-01"); // 00:30 in India
});

test("the URL round-trips a period, and nonsense falls back to this month", () => {
  const p = periodAt("week", "2026-10-14");
  expect(fromParams(new URLSearchParams(toParams(p)), "2026-10-19")).toEqual(p);
  const custom = { unit: "custom" as const, start: "2026-09-01", end: "2026-09-15" };
  expect(fromParams(new URLSearchParams(toParams(custom)), "2026-10-19")).toEqual(custom);
  expect(fromParams(new URLSearchParams("unit=custom&from=2026-09-15&to=2026-09-01"), "2026-10-19"))
    .toEqual(periodAt("month", "2026-10-19"));
  expect(fromParams(new URLSearchParams("unit=year&from=x"), "2026-10-19")).toEqual(periodAt("month", "2026-10-19"));
});
