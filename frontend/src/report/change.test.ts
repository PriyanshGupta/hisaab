import { describeChange } from "./change";

test.each([
  [450_000, 330_000, "+₹1,200", "₹1,200 more than Sep"],
  [100_000, 130_050, "−₹301", "₹301 less than Sep"], // whole rupees, rounded
  [50_000, 0, "new", "none in Sep"],
  [50_000, 49_950, "same", "the same as Sep"], // under a rupee apart
  [0, 0, "same", "the same as Sep"],
  [0, 20_000, "−₹200", "₹200 less than Sep"], // spent last month, nothing this month
])("%i paise now against %i before reads %s", (now, before, mark, said) => {
  expect(describeChange(now, before, "Sep")).toEqual({ mark, said });
});
