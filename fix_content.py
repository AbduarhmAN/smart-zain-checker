import os

with open("chrome_extension/content.js", "r", encoding="utf-8") as f:
    content = f.read()

# find where readCurrentResult starts and normalizeAmountValue starts
start_idx = content.find("function readCurrentResult(")
end_idx = content.find("function normalizeAmountValue(")

replacement = """function readCurrentResult(recordType, contract) {
  const rejection = readRejection();
  if (rejection) return rejection;

  if (!hasRequestedIdentity(recordType, contract)) return null;

  for (const selector of [
    "#customAmount",
    'input[name="customAmount"]',
  ]) {
    for (const field of document.querySelectorAll(selector)) {
      let value = String(field.value || "").trim();
      
      if (isVisible(field)) {
        if (!value) {
           const span = document.querySelector(".quickpay__custom-balance-input span[aria-hidden='true']");
           if (span && span.innerText) {
               value = String(span.innerText).trim();
           }
        }
        if (value) {
          return { status: "ok", website_amount: value };
        }
      }
    }
  }

  // Fallback for when Zain hides the input (e.g. 0.00 paid accounts)
  for (const selector of [
    ".quickpay__custom-balance-input span[aria-hidden='true']",
    "#payBillValue span",
  ]) {
    for (const field of document.querySelectorAll(selector)) {
      const value = String(field.innerText || "").trim();
      if (isVisible(field) && value) {
        return { status: "ok", website_amount: value };
      }
    }
  }

  for (const selector of [
    "#formValidateContractPaymentInfo .invalid-feedback",
    ".alert-danger",
    ".text-danger",
  ]) {
    for (const element of document.querySelectorAll(selector)) {
      const text = String(element.innerText || "").trim();
      if (isVisible(element) && text) {
        return { status: "error", message: `Zain error: ${text}` };
      }
    }
  }

  const alertBox = document.querySelector(".alert-danger");
  if (isVisible(alertBox) && alertBox.innerText.includes("غير متوفرة")) {
    return { status: "website_error", message: "Service unavailable." };
  }

  return null;
}

function readCustomAmount() {
  for (const selector of ["#customAmount", 'input[name="customAmount"]']) {
    for (const field of document.querySelectorAll(selector)) {
      let value = String(field.value || "").trim();
      
      if (isVisible(field)) {
        if (!value) {
           const span = document.querySelector(".quickpay__custom-balance-input span[aria-hidden='true']");
           if (span && span.innerText) {
               value = String(span.innerText).trim();
           }
        }
        if (value) return value;
      }
    }
  }
  return "";
}

"""

new_content = content[:start_idx] + replacement + content[end_idx:]

with open("chrome_extension/content.js", "w", encoding="utf-8") as f:
    f.write(new_content)
