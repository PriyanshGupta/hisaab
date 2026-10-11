import { formatDate, formatInstantDate } from "./dates";

test("a calendar date is shown as written", () => {
  expect(formatDate("2026-07-11")).toBe("11 Jul 2026");
});

test("a UTC timestamp is shown as its India date", () => {
  // 20:00 UTC on 11 Oct is 01:30 on 12 Oct in India; slicing the string would say 11 Oct.
  expect(formatInstantDate("2026-10-11T20:00:00.000Z")).toBe("12 Oct 2026");
  expect(formatInstantDate("2026-10-11T18:00:00.000Z")).toBe("11 Oct 2026");
  expect(formatInstantDate("2026-08-31T20:00:00.000Z")).toBe("1 Sep 2026"); // same month names as formatDate
});
