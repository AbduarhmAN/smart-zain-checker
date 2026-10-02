import asyncio
import re
from types import TracebackType

from playwright.async_api import (
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from .config import (
    AMOUNT_TIMEOUT_MS,
    PAGE_TIMEOUT_MS,
    ZAIN_BROWSER_PROFILE_DIRECTORY,
    ZAIN_CONTRACT_PAYMENT_URL,
    ZAIN_HOME_URL,
)
from .console import format_console_message, log
from .money import money_to_halalas, to_western_digits


AMOUNT_INPUT_SELECTORS = (
    '#formSaveContractPaymentConfig input[name="amount"]',
    'input[name="amount"]',
)

WEBSITE_ERROR_SELECTORS = (
    "#formValidateContractPaymentInfo .invalid-feedback",
    ".alert-danger",
    ".text-danger",
)

WAIT_FOR_RESULT_SCRIPT = r"""
() => {
    const hasAmountInput = Array.from(
        document.querySelectorAll('input[name="amount"]')
    ).some((element) => String(element.value || '').trim() !== '');

    if (hasAmountInput) return true;

    const bodyText = document.body?.innerText || '';
    if (bodyText.includes('The requested URL was rejected.')) return true;

    const hasLabeledAmount = /(?:المبلغ\s*(?:المستحق|المطلوب|الإجمالي|الواجب)|amount\s*due|due\s*amount)/i.test(bodyText);
    if (hasLabeledAmount) return true;

    return Array.from(document.querySelectorAll(
        '#formValidateContractPaymentInfo .invalid-feedback, .alert-danger, .text-danger'
    )).some((element) => {
        const style = window.getComputedStyle(element);
        return style.display !== 'none'
            && style.visibility !== 'hidden'
            && String(element.innerText || '').trim() !== '';
    });
}
"""


class ZainRequestRejected(RuntimeError):
    def __init__(self, support_id: str | None) -> None:
        self.support_id = support_id
        message = "Zain rejected the requested URL."
        if support_id:
            message = f"{message} Support ID: {support_id}."
        super().__init__(message)


class ZainBrowser:
    def __init__(self) -> None:
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self.page: Page | None = None

    async def __aenter__(self) -> Page:
        self._playwright = await async_playwright().start()
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=ZAIN_BROWSER_PROFILE_DIRECTORY,
            channel="chrome",
            headless=False,
            locale="ar-SA",
            no_viewport=True,
        )
        self.page = (
            self._context.pages[0]
            if self._context.pages
            else await self._context.new_page()
        )
        self.page.set_default_timeout(PAGE_TIMEOUT_MS)
        self.page.set_default_navigation_timeout(PAGE_TIMEOUT_MS)
        return self.page

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._context:
            await self._context.close()
        if self._playwright:
            await self._playwright.stop()


async def wait_for_manual_login(page: Page) -> None:
    await page.goto(ZAIN_HOME_URL, wait_until="domcontentloaded")
    log(
        "The dedicated Zain Chrome profile is persistent. "
        "Log in only if Zain asks, then return to this terminal."
    )
    await asyncio.to_thread(
        input,
        format_console_message("Press Enter when the Zain page is ready: "),
    )


async def get_website_amount(page: Page, contract: str) -> int:
    await page.goto(ZAIN_CONTRACT_PAYMENT_URL, wait_until="domcontentloaded")
    await _raise_if_request_rejected(page)

    contract_input = await _first_visible(page.locator("#txtContract"))
    if not contract_input:
        raise RuntimeError("The Zain contract form has no visible account input.")

    await contract_input.fill(contract)

    submit_button = await _first_visible(
        page.locator('#formValidateContractPaymentInfo button[type="submit"]')
    )
    if not submit_button:
        raise RuntimeError("The Zain contract form has no visible submit button.")

    await submit_button.click()

    try:
        await page.wait_for_function(
            WAIT_FOR_RESULT_SCRIPT,
            timeout=AMOUNT_TIMEOUT_MS,
        )
    except PlaywrightTimeoutError as error:
        await _raise_if_request_rejected(page)
        raise RuntimeError(
            "Zain did not show an amount before the timeout."
        ) from error

    await _raise_if_request_rejected(page)

    amount = await _read_amount_input(page)
    if amount is not None:
        return amount

    visible_amount = await _read_labeled_visible_amount(page, contract)
    if visible_amount is not None:
        return visible_amount

    website_error = await _read_website_error(page)
    if website_error:
        raise RuntimeError(f"Zain did not return an amount: {website_error}")

    raise RuntimeError("Zain returned a result that contained no readable amount.")


async def _read_amount_input(page: Page) -> int | None:
    for selector in AMOUNT_INPUT_SELECTORS:
        fields = page.locator(selector)
        for index in range(await fields.count()):
            raw_value = (await fields.nth(index).input_value()).strip()
            if not raw_value:
                continue
            try:
                return money_to_halalas(raw_value)
            except ValueError:
                continue
    return None


async def _read_labeled_visible_amount(page: Page, contract: str) -> int | None:
    body_text = await page.locator("body").inner_text()
    lines = [line.strip() for line in body_text.splitlines() if line.strip()]
    label_pattern = re.compile(
        r"(?:المبلغ\s*(?:المستحق|المطلوب|الإجمالي|الواجب)|amount\s*due|due\s*amount)",
        re.IGNORECASE,
    )

    for index, line in enumerate(lines):
        if not label_pattern.search(line):
            continue

        nearby_text = " ".join(lines[index : index + 4])
        candidates = re.findall(
            r"[+-]?\d[\d\s,.٫٬]*",
            to_western_digits(nearby_text),
        )

        for candidate in reversed(candidates):
            if candidate.replace(" ", "").replace(",", "") == contract:
                continue
            try:
                return money_to_halalas(candidate)
            except ValueError:
                continue
    return None


async def _read_website_error(page: Page) -> str | None:
    for selector in WEBSITE_ERROR_SELECTORS:
        elements = page.locator(selector)
        for index in range(await elements.count()):
            element = elements.nth(index)
            if not await element.is_visible():
                continue
            text = (await element.inner_text()).strip()
            if text:
                return text
    return None


async def _first_visible(locator):
    for index in range(await locator.count()):
        candidate = locator.nth(index)
        if await candidate.is_visible():
            return candidate
    return None


async def _raise_if_request_rejected(page: Page) -> None:
    body_text = await page.locator("body").inner_text()
    if "The requested URL was rejected." not in body_text:
        return

    support_id_match = re.search(
        r"Your support ID is:\s*<?\s*([0-9]+)",
        body_text,
        re.IGNORECASE,
    )
    support_id = support_id_match.group(1) if support_id_match else None
    raise ZainRequestRejected(support_id)
