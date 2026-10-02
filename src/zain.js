import { chromium } from "playwright";
import {
  AMOUNT_TIMEOUT_MS,
  CONTRACT_KEY_DELAY_MS,
  FORM_INTERACTION_DELAY_MS,
  PAGE_TIMEOUT_MS,
  ZAIN_CONTRACT_PAYMENT_URL,
} from "./config.js";
import { moneyToHalalas, toWesternDigits } from "./money.js";

const AMOUNT_INPUT_SELECTORS = [
  '#formSaveContractPaymentConfig input[name="amount"]',
  'input[name="amount"]',
];

const WEBSITE_ERROR_SELECTORS = [
  "#formValidateContractPaymentInfo .invalid-feedback",
  ".alert-danger",
  ".text-danger",
];

export async function openZainBrowser() {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ locale: "ar-SA" });
  const page = await context.newPage();
  page.setDefaultTimeout(PAGE_TIMEOUT_MS);
  page.setDefaultNavigationTimeout(PAGE_TIMEOUT_MS);
  return { browser, page };
}

export async function getWebsiteAmount(page, contract) {
  const url = new URL(ZAIN_CONTRACT_PAYMENT_URL);
  url.searchParams.set("contract", contract);

  await page.goto(url.toString(), { waitUntil: "domcontentloaded" });

  const amountFromUrl = await readAmountInput(page);
  if (amountFromUrl !== null) {
    return amountFromUrl;
  }

  const contractInput = await firstVisible(page.locator("#txtContract"));
  if (contractInput) {
    await page.waitForTimeout(FORM_INTERACTION_DELAY_MS);
    await contractInput.fill("");
    await contractInput.pressSequentially(contract, {
      delay: CONTRACT_KEY_DELAY_MS,
    });
    const submitButton = await firstVisible(
      page.locator('#formValidateContractPaymentInfo button[type="submit"]'),
    );

    if (!submitButton) {
      throw new Error("The Zain contract form has no visible submit button.");
    }

    await page.waitForTimeout(FORM_INTERACTION_DELAY_MS);
    await submitButton.click();
  }

  const deadline = Date.now() + AMOUNT_TIMEOUT_MS;
  while (Date.now() < deadline) {
    const amount = await readAmountInput(page);
    if (amount !== null) {
      return amount;
    }

    const visibleAmount = await readLabeledVisibleAmount(page, contract);
    if (visibleAmount !== null) {
      return visibleAmount;
    }

    const websiteError = await readWebsiteError(page);
    if (websiteError) {
      throw new Error(`Zain did not return an amount: ${websiteError}`);
    }

    await page.waitForTimeout(250);
  }

  throw new Error("Zain did not show an amount before the timeout.");
}

async function readAmountInput(page) {
  for (const selector of AMOUNT_INPUT_SELECTORS) {
    const fields = page.locator(selector);
    const count = await fields.count();

    for (let index = 0; index < count; index += 1) {
      const rawValue = (await fields.nth(index).inputValue()).trim();
      if (!rawValue) {
        continue;
      }

      try {
        return moneyToHalalas(rawValue);
      } catch {
        // Continue to another amount field when this field is not monetary.
      }
    }
  }

  return null;
}

async function readLabeledVisibleAmount(page, contract) {
  const bodyText = await page.locator("body").innerText();
  const lines = bodyText
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);

  const labelPattern =
    /(?:المبلغ\s*(?:المستحق|المطلوب|الإجمالي|الواجب)|amount\s*due|due\s*amount)/i;

  for (let index = 0; index < lines.length; index += 1) {
    if (!labelPattern.test(lines[index])) {
      continue;
    }

    const nearbyText = lines.slice(index, index + 4).join(" ");
    const candidates = toWesternDigits(nearbyText).match(/[+-]?\d[\d\s,.\u066b\u066c]*/g) ?? [];

    for (const candidate of candidates.reverse()) {
      const compactCandidate = candidate.replace(/[\s,]/g, "");
      if (compactCandidate === contract) {
        continue;
      }

      try {
        return moneyToHalalas(candidate);
      } catch {
        // Continue until a valid monetary value is found near the label.
      }
    }
  }

  return null;
}

async function readWebsiteError(page) {
  for (const selector of WEBSITE_ERROR_SELECTORS) {
    const elements = page.locator(selector);
    const count = await elements.count();

    for (let index = 0; index < count; index += 1) {
      const element = elements.nth(index);
      if (!(await element.isVisible().catch(() => false))) {
        continue;
      }

      const text = (await element.innerText()).trim();
      if (text) {
        return text;
      }
    }
  }

  return null;
}

async function firstVisible(locator) {
  const count = await locator.count();

  for (let index = 0; index < count; index += 1) {
    const candidate = locator.nth(index);
    if (await candidate.isVisible().catch(() => false)) {
      return candidate;
    }
  }

  return null;
}
