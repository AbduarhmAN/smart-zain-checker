import assert from "node:assert/strict";
import test from "node:test";
import { halalasToNumber, moneyToHalalas, toWesternDigits } from "../src/money.js";

test("reads the Excel amount examples exactly", () => {
  assert.equal(moneyToHalalas("126482.9"), 12_648_290n);
  assert.equal(moneyToHalalas("54,815.22"), 5_481_522n);
  assert.equal(moneyToHalalas(200.39), 20_039n);
});

test("reads Arabic digits and separators", () => {
  assert.equal(toWesternDigits("١٢٦٤٨٢"), "126482");
  assert.equal(moneyToHalalas("١٢٦٬٤٨٢٫٨٥ ر.س"), 12_648_285n);
});

test("uses two decimal places for exact comparison", () => {
  assert.equal(moneyToHalalas("46.07"), 4_607n);
  assert.equal(moneyToHalalas("46.00"), 4_600n);
  assert.throws(() => moneyToHalalas("1,000.001"), /more than two decimal places/);
});

test("writes halalas back as an Excel number", () => {
  assert.equal(halalasToNumber(12_648_285n), 126482.85);
  assert.equal(halalasToNumber(-1_055n), -10.55);
});
