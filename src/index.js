import { BETWEEN_CUSTOMERS_DELAY_MS, PROJECT_DIRECTORY } from "./config.js";
import {
  findSourceWorkbook,
  loadAccounts,
  writeWantedAmounts,
} from "./workbook.js";
import { getWebsiteAmount, openZainBrowser } from "./zain.js";

async function main() {
  const workbookPath = await findSourceWorkbook(PROJECT_DIRECTORY);
  const { workbook, customers } = await loadAccounts(workbookPath);

  if (customers.length === 0) {
    console.log("No customer rows were found in the first worksheet.");
    return;
  }

  const { browser, page } = await openZainBrowser();
  const mismatches = [];

  try {
    for (let index = 0; index < customers.length; index += 1) {
      const customer = customers[index];
      let websiteAmount;

      try {
        websiteAmount = await getWebsiteAmount(page, customer.contract);
      } catch (error) {
        throw new Error(`Accounts row ${customer.rowNumber}: ${error.message}`);
      }

      if (websiteAmount !== customer.expectedAmount) {
        mismatches.push({
          expectedAmount: customer.expectedAmount,
          websiteAmount,
        });
      }

      const completed = index + 1;
      if (completed === customers.length || completed % 25 === 0) {
        console.log(`Checked ${completed} of ${customers.length} customers.`);
      }

      if (completed < customers.length) {
        await page.waitForTimeout(BETWEEN_CUSTOMERS_DELAY_MS);
      }
    }
  } finally {
    await browser.close();
  }

  const workbookChanged = await writeWantedAmounts(
    workbookPath,
    workbook,
    mismatches,
  );

  if (workbookChanged) {
    console.log(
      `Created "Wanted Amount" with ${mismatches.length} mismatched amount(s).`,
    );
  } else {
    console.log("All website amounts match column M. The workbook was not changed.");
  }
}

main().catch((error) => {
  console.error(error.message);
  process.exitCode = 1;
});
