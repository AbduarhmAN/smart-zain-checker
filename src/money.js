const ARABIC_INDIC_DIGITS = "٠١٢٣٤٥٦٧٨٩";
const EASTERN_ARABIC_DIGITS = "۰۱۲۳۴۵۶۷۸۹";

export function toWesternDigits(value) {
  return String(value)
    .replace(/[٠-٩]/g, (digit) => String(ARABIC_INDIC_DIGITS.indexOf(digit)))
    .replace(/[۰-۹]/g, (digit) => String(EASTERN_ARABIC_DIGITS.indexOf(digit)));
}

export function moneyToHalalas(value) {
  if (typeof value === "number") {
    if (!Number.isFinite(value)) {
      throw new Error(`Invalid numeric amount: ${value}`);
    }

    return BigInt(Math.round(value * 100));
  }

  if (typeof value === "bigint") {
    return value * 100n;
  }

  if (value === null || value === undefined || String(value).trim() === "") {
    throw new Error("Amount is empty.");
  }

  const normalized = toWesternDigits(value)
    .replace(/[\u200e\u200f\u202a-\u202e\u2066-\u2069]/g, "")
    .replace(/\u066c/g, ",")
    .replace(/\u066b/g, ".")
    .trim();

  const matches = normalized.match(/[+-]?\d[\d\s,.']*/g);
  if (!matches || matches.length !== 1) {
    throw new Error(`Could not read one amount from: ${String(value)}`);
  }

  let token = matches[0].replace(/[\s']/g, "");
  let sign = 1n;

  if (token.startsWith("-")) {
    sign = -1n;
    token = token.slice(1);
  } else if (token.startsWith("+")) {
    token = token.slice(1);
  }

  const decimalSeparatorIndex = findDecimalSeparatorIndex(token);
  let wholePart;
  let fractionalPart;

  if (decimalSeparatorIndex === -1) {
    wholePart = token.replace(/[,.]/g, "");
    fractionalPart = "";
  } else {
    wholePart = token.slice(0, decimalSeparatorIndex).replace(/[,.]/g, "");
    fractionalPart = token.slice(decimalSeparatorIndex + 1).replace(/[,.]/g, "");
  }

  if (!/^\d+$/.test(wholePart) || !/^\d*$/.test(fractionalPart)) {
    throw new Error(`Invalid amount: ${String(value)}`);
  }

  if (fractionalPart.length > 2 && /[1-9]/.test(fractionalPart.slice(2))) {
    throw new Error(`Amount has more than two decimal places: ${String(value)}`);
  }

  const halalas = BigInt(wholePart) * 100n + BigInt((fractionalPart + "00").slice(0, 2));
  return sign * halalas;
}

export function halalasToNumber(value) {
  const maximumSafeHalalas = BigInt(Number.MAX_SAFE_INTEGER);
  if (value > maximumSafeHalalas || value < -maximumSafeHalalas) {
    throw new Error("Amount is too large to write safely to Excel.");
  }

  return Number(value) / 100;
}

function findDecimalSeparatorIndex(token) {
  const lastDot = token.lastIndexOf(".");
  const lastComma = token.lastIndexOf(",");

  if (lastDot !== -1 && lastComma !== -1) {
    return Math.max(lastDot, lastComma);
  }

  const separatorIndex = Math.max(lastDot, lastComma);
  if (separatorIndex === -1) {
    return -1;
  }

  const separator = token[separatorIndex];
  const occurrences = token.split(separator).length - 1;
  const digitsAfterSeparator = token.length - separatorIndex - 1;

  if (occurrences === 1 && digitsAfterSeparator > 0 && digitsAfterSeparator <= 2) {
    return separatorIndex;
  }

  if (occurrences > 1 && digitsAfterSeparator > 0 && digitsAfterSeparator <= 2) {
    return separatorIndex;
  }

  return -1;
}

