import { formatDate, formatInstant, formatInstantDate, threeMonthsAgoIso } from "./dates";

test("a calendar date is shown as written", () => {
  expect(formatDate("2026-07-11")).toBe("11 Jul 2026");
});

test("a UTC timestamp is shown as its India date", () => {
  // 20:00 UTC on 11 Oct is 01:30 on 12 Oct in India; slicing the string would say 11 Oct.
  expect(formatInstantDate("2026-10-11T20:00:00.000Z")).toBe("12 Oct 2026");
  expect(formatInstantDate("2026-10-11T18:00:00.000Z")).toBe("11 Oct 2026");
  expect(formatInstantDate("2026-08-31T20:00:00.000Z")).toBe("1 Sep 2026"); // same month names as formatDate
});

test("three months back clamps to the month's last day, as the server's backfill start does", () => {
  expect(threeMonthsAgoIso(new Date(2026, 4, 31))).toBe("2026-02-28");
  expect(threeMonthsAgoIso(new Date(2026, 9, 11))).toBe("2026-07-11");
  expect(threeMonthsAgoIso(new Date(2026, 0, 15))).toBe("2025-10-15");
});

test("an instant reads as India's date and time", () => {
  expect(formatInstant("2026-10-05T03:44:00.000Z")).toBe("5 Oct 2026 at 09:14");
  expect(formatInstant("2026-10-11T20:00:00.000Z")).toBe("12 Oct 2026 at 01:30");
});
