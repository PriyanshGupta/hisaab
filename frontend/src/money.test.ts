import { formatPaise, paiseToInput, parseRupees } from "./money";

describe("parseRupees", () => {
  test.each([
    ["47000", 4700000],
    ["47,000", 4700000],
    ["1,23,456.78", 12345678],
    ["₹ 47,000.50", 4700050],
    ["₹47,000.5", 4700050],
    ["0.5", 50],
    ["0.05", 5],
    ["  12 ", 1200],
    ["0", 0],
    ["9999999999999.99", 999999999999999], // ₹10 lakh crore: still exact
  ])("%s -> %i paise", (input, paise) => {
    expect(parseRupees(input)).toEqual({ ok: true, paise });
  });

  test("a minus sign is allowed only when asked for", () => {
    expect(parseRupees("-1,500", { allowNegative: true })).toEqual({ ok: true, paise: -150000 });
    expect(parseRupees("-1,500")).toEqual({ ok: false, error: "Enter a positive amount." });
  });

  test.each([
    ["", "Enter an amount."],
    ["   ", "Enter an amount."],
    ["12.345", "Use at most 2 decimal places."],
    ["abc", "Enter an amount like 47,000 or 47,000.50."],
    ["1.2.3", "Enter an amount like 47,000 or 47,000.50."],
    ["12e3", "Enter an amount like 47,000 or 47,000.50."],
    ["99999999999999", "That amount is too large."],
  ])("%j is rejected: %s", (input, error) => {
    expect(parseRupees(input)).toEqual({ ok: false, error });
  });

  test("no floating point: 0.1 + 0.2 style inputs stay exact", () => {
    expect(parseRupees("0.29")).toEqual({ ok: true, paise: 29 }); // 0.29 * 100 = 28.999999999999996 in floats
    expect(parseRupees("1.15")).toEqual({ ok: true, paise: 115 }); // 1.15 * 100 = 114.99999999999999
  });
});

describe("formatPaise", () => {
  test.each([
    [4700000, "₹47,000.00"],
    [12345678, "₹1,23,456.78"],
    [5, "₹0.05"],
    [0, "₹0.00"],
    [-150000, "−₹1,500.00"],
  ])("%i -> %s", (paise, text) => {
    expect(formatPaise(paise)).toBe(text);
  });
});

test("paiseToInput round-trips through parseRupees", () => {
  for (const paise of [0, 5, 4700050, 12345678, -150000]) {
    expect(parseRupees(paiseToInput(paise), { allowNegative: true })).toEqual({ ok: true, paise });
  }
});
