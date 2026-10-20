const ZAIN_PAYMENT_URL = "https://business.zain.sa/dashboard/quick-pay";
const ZAIN_QUICKPAY_URL = "https://app.sa.zain.com/ar/quickpay";
const RESULT_TIMEOUT_MS = 60_000;
const RESULT_SETTLE_MS = 750;
const AMOUNT_STABILITY_MS = 500;
const UNCHANGED_AMOUNT_GRACE_MS = 4_000;
const ZERO_AMOUNT_GRACE_MS = 5_000;
const REDIRECT_GRACE_MS = 6_000;

let loopRunning = false;

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type === "ping") {
    sendResponse({ status: "ready" });
    return;
  }
  if (message?.type === "wake") {
    startLoop();
  }
});

const contentMemoryStorage = {};

async function safeStorageGet(keys) {
  const isArray = Array.isArray(keys);
  const keyList = isArray ? keys : (typeof keys === "string" ? [keys] : Object.keys(keys || {}));
  let result = {};
  try {
    result = await chrome.storage.local.get(keys);
  } catch (error) {
    console.warn("[ZainChecker:Content] Storage get failed, using memory:", error);
  }
  for (const k of keyList) {
    if (result[k] === undefined && contentMemoryStorage[k] !== undefined) {
      result[k] = contentMemoryStorage[k];
    }
  }
  return result;
}

async function safeStorageSet(items) {
  if (!items || typeof items !== "object") return;
  Object.assign(contentMemoryStorage, items);
  try {
    await chrome.storage.local.set(items);
  } catch (error) {
    console.warn("[ZainChecker:Content] Storage set failed, retained in memory:", error);
  }
}

function detectWorkerIdentity() {
  const hash = window.location.hash || "";
  if (hash.includes("worker=worker_2") || hash.includes("worker_2") || hash.includes("proxy")) {
    return "Worker 2 (Proxy)";
  }
  if (hash.includes("worker=worker_1") || hash.includes("worker_1") || hash.includes("router")) {
    return "Worker 1 (Router)";
  }
  return null;
}

console.log("[ZainChecker:Content] Script loaded on:", window.location.href);
initializeChecker();

async function initializeChecker() {
  const detectedWorker = detectWorkerIdentity();
  if (detectedWorker) {
    await safeStorageSet({ zainWorkerId: detectedWorker });
  }

  console.log("[ZainChecker:Content] initializeChecker starting on:", window.location.href, "Worker:", detectedWorker);
  try {
    await autoStartIfRequested();
    await startLoop();
  } catch (error) {
    console.error("[ZainChecker:Content] initializeChecker error:", error);
    if (!isExtensionContextInvalidated(error)) {
      try {
        await safeStorageSet({
          zainCheckerStatus: `Page error: ${String(error?.message || error)}`,
        });
      } catch (reportingError) {
        if (!isExtensionContextInvalidated(reportingError)) throw reportingError;
      }
    }
  }
}

async function autoStartIfRequested() {
  const pageUrl = new URL(window.location.href);
  let recordType = "";
  if (pageUrl.searchParams.has("contract") || pageUrl.pathname.endsWith("/contract-payment") || pageUrl.pathname.includes("quick-pay")) {
    recordType = "account";
  } else if (pageUrl.searchParams.has("account") || pageUrl.pathname.endsWith("/quickpay")) {
    recordType = "wallet";
  }
  if (!recordType) return;

  const detectedWorker = detectWorkerIdentity();
  const state = await safeStorageGet(["zainCheckerRunning", "zainWorkerId"]);
  const workerId = detectedWorker || state.zainWorkerId || null;

  // SAFETY GUARD: If not marked with a checker worker and checker is not running,
  // do NOT auto-start! (Prevents hijacking normal user browsing)
  if (!detectedWorker && !state.zainCheckerRunning) {
    console.log("[ZainChecker:Content] Tab has no checker worker tag and checker is not running; skipping auto-start.");
    return;
  }

  const effectiveWorker = workerId || "Worker 1 (Router)";
  const searchNumber = readPageSearchNumber(pageUrl, recordType);

  if (!/^\d+$/.test(searchNumber)) {
    if (state.zainCheckerRunning || detectedWorker) {
      console.log("[ZainChecker:Content] Base quick-pay URL opened while checker is running; adopting tab and starting loop.");
      try {
        await sendMessage({ type: "adopt_tab", worker_id: effectiveWorker });
      } catch (e) {}
      startLoop();
    }
    return;
  }

  console.log("[ZainChecker:Content] autoStartIfRequested: zainCheckerRunning =", state.zainCheckerRunning, "recordType =", recordType, "searchNumber =", searchNumber, "worker =", effectiveWorker);
  if (state.zainCheckerRunning) {
    try {
      const res = await sendMessage({ type: "adopt_tab", worker_id: effectiveWorker });
      console.log("[ZainChecker:Content] adopt_tab response:", JSON.stringify(res));
    } catch (e) {
      console.warn("[ZainChecker:Content] adopt_tab non-fatal error:", e);
    }
    return;
  }

  try {
    const res = await sendMessage({
      type: "auto_start",
      record_type: recordType,
      search_number: searchNumber,
      worker_id: effectiveWorker,
    });
    console.log("[ZainChecker:Content] auto_start response:", JSON.stringify(res));
  } catch (e) {
    console.warn("[ZainChecker:Content] auto_start non-fatal error:", e);
  }
}

async function startLoop() {
  if (loopRunning) {
    console.log("[ZainChecker:Content] startLoop already running, skipping duplicate call.");
    return;
  }
  loopRunning = true;
  console.log("[ZainChecker:Content] startLoop started.");

  try {
    while (await shouldRun()) {
      let task;
      const workerState = await safeStorageGet("zainWorkerId");
      const workerId = detectWorkerIdentity() || workerState.zainWorkerId || "Worker 1 (Router)";
      try {
        task = await sendMessage({ type: "poll_task", worker_id: workerId });
        console.log(`[ZainChecker:Content][${workerId}] Polled task from SW:`, JSON.stringify(task));
      } catch (sendError) {
        if (isExtensionContextInvalidated(sendError)) {
          console.warn("[ZainChecker:Content] Context invalidated, breaking loop.");
          break;
        }
        console.warn("[ZainChecker:Content] poll_task sendError:", sendError);
        await delay(1_000);
        continue;
      }

      if (!task || typeof task !== "object") {
        console.warn("[ZainChecker:Content] Received invalid task:", task);
        await delay(1_000);
        continue;
      }

      if (task.status === "check") {
        const checkStartTime = Date.now();
        console.log(`[ZainChecker:Content][${workerId}] Received task to check: row`, task.row_number, "number", task.search_number);
        await processTask(task);

        // Safe pacing sleep: ensure 3.9 seconds between checks per worker to prevent IP ban
        const elapsed = Date.now() - checkStartTime;
        const TARGET_CHECK_INTERVAL_MS = 3_900;
        if (elapsed < TARGET_CHECK_INTERVAL_MS) {
          const sleepRemaining = TARGET_CHECK_INTERVAL_MS - elapsed;
          console.log(`[ZainChecker:Content][${workerId}] Safe pacing sleep: ${sleepRemaining}ms (target ${TARGET_CHECK_INTERVAL_MS}ms).`);
          await delay(sleepRemaining);
        }
        continue;
      }

      if (task.status === "complete" || task.status === "error") {
        console.log("[ZainChecker:Content] Loop terminating on status:", task.status);
        break;
      }

      if (task.status === "inactive") {
        console.warn("[ZainChecker:Content] SW reported inactive, sending adopt_tab and retrying...");
        try {
          await sendMessage({ type: "adopt_tab", worker_id: workerId });
        } catch {}
        await delay(1_000);
        continue;
      }

      const retrySeconds = Number(task.retry_after_seconds ?? 2.5);
      if (retrySeconds > 0) {
        console.log(`[ZainChecker:Content] Task status "${task.status}". Waiting ${retrySeconds}s...`);
        await delay(retrySeconds * 1_000);
      }
    }
  } catch (error) {
    console.error("[ZainChecker:Content] startLoop catch:", error);
    if (!isExtensionContextInvalidated(error)) {
      try {
        await safeStorageSet({
          zainCheckerStatus: `Page error: ${String(error?.message || error)}`,
        });
      } catch (reportingError) {
        if (!isExtensionContextInvalidated(reportingError)) throw reportingError;
      }
    }
  } finally {
    loopRunning = false;
    console.log("[ZainChecker:Content] startLoop exited, loopRunning set to false.");
  }
}

async function shouldRun() {
  const state = await safeStorageGet("zainCheckerRunning");
  const running = Boolean(state.zainCheckerRunning);
  console.log("[ZainChecker:Content] shouldRun check:", running);
  return running;
}

async function processTask(task) {
  const state = await safeStorageGet("zainCheckerActiveTask");
  let activeTask = state.zainCheckerActiveTask;
  console.log("[ZainChecker:Content] processTask for", task.search_number, "activeTask was:", JSON.stringify(activeTask), "do_single_refresh:", task.do_single_refresh);

  if (task.do_single_refresh === true) {
    console.log(
      "[ZainChecker:Content] Performing single one-time page refresh (1x only) for Service Number (2...) Cycle #",
      task.service_cycle_id,
    );
    activeTask = {
      task_id: task.task_id,
      row_number: task.row_number,
      record_type: task.record_type,
      search_number: task.search_number,
      stage: "navigating",
      previous_website_amount: "",
    };
    await safeStorageSet({ zainCheckerActiveTask: activeTask });
    await navigateTask(task, "", true);
    return true;
  }

  if (!sameTask(activeTask, task)) {
    const isRequestedPage = isRequestedRecordPage(
      task.record_type,
      task.search_number,
    );
    console.log("[ZainChecker:Content] !sameTask. isRequestedPage for", task.search_number, "is:", isRequestedPage);
    activeTask = {
      task_id: task.task_id,
      row_number: task.row_number,
      record_type: task.record_type,
      search_number: task.search_number,
      stage: "navigating",
      previous_website_amount: isRequestedPage ? "" : readCustomAmount(),
    };
    await safeStorageSet({ zainCheckerActiveTask: activeTask });
    if (!isRequestedPage) {
      console.log("[ZainChecker:Content] Reusing same tab and navigating to:", task.search_number);
      await navigateTask(task, activeTask.previous_website_amount, false);
      const navWaitDeadline = Date.now() + 15_000;
      while (Date.now() < navWaitDeadline) {
        await delay(400);
        if (isRequestedRecordPage(task.record_type, task.search_number)) {
          console.log("[ZainChecker:Content] Tab reached requested record page:", task.search_number);
          break;
        }
      }
    }
  }

  activeTask.stage = "reading";
  await safeStorageSet({ zainCheckerActiveTask: activeTask });
  console.log("[ZainChecker:Content] Calling waitForResult for", task.search_number);
  const result = await waitForResult(
    task.record_type,
    task.search_number,
    activeTask.previous_website_amount,
  );
  console.log("[ZainChecker:Content] waitForResult result:", JSON.stringify(result));
  return submitResult(task, result);
}

async function submitResult(task, result) {
  await delay(900);
  const workerState = await safeStorageGet("zainWorkerId");
  const workerId = detectWorkerIdentity() || workerState.zainWorkerId || "Worker 1 (Router)";
  console.log(`[ZainChecker:Content][${workerId}] submitResult for`, task.search_number, "result:", JSON.stringify(result));
  const response = await sendMessage({
    type: "submit_result",
    worker_id: workerId,
    result: {
      task_id: task.task_id,
      worker_id: workerId,
      row_number: task.row_number,
      contract: task.search_number,
      record_type: task.record_type,
      search_number: task.search_number,
      ...result,
    },
  });
  console.log("[ZainChecker:Content] submitResult response from SW:", JSON.stringify(response));

  if (
    ![
      "ok",
      "waiting",
      "recheck_mismatch",
      "reload_mismatch",
      "retry_redirect",
      "network_wait",
      "open_incognito",
      "incognito_failed",
      "complete",
      "error",
    ].includes(response?.status)
  ) {
    throw new Error(response?.message || "Python rejected the browser result.");
  }

  if (response.status === "reload_mismatch") {
    const retryTask = { ...task, task_id: response.task_id || task.task_id };
    await safeStorageSet({
      zainCheckerActiveTask: {
        task_id: retryTask.task_id,
        row_number: task.row_number,
        record_type: task.record_type,
        search_number: task.search_number,
        stage: "navigating",
        previous_website_amount: "",
      },
    });
    const retrySeconds = Math.max(Number(response.retry_after_seconds || 1), 1);
    await delay(retrySeconds * 1_000);
    await navigateTask(retryTask, "", true);
    return true;
  }

  if (response.status === "retry_redirect") {
    const retryTask = { ...task, task_id: response.task_id || task.task_id };
    await safeStorageSet({
      zainCheckerActiveTask: {
        task_id: retryTask.task_id,
        row_number: task.row_number,
        record_type: task.record_type,
        search_number: task.search_number,
        stage: "navigating",
        previous_website_amount: "",
      },
    });
    const retrySeconds = Math.max(Number(response.retry_after_seconds || 1), 1);
    await delay(retrySeconds * 1_000);
    await navigateTask(retryTask, "", true);
    return true;
  }

  if (
    response.status !== "open_incognito" &&
    response.status !== "incognito_failed"
  ) {
    await safeStorageSet({ zainCheckerActiveTask: null });
  }
  return (
    response.status === "open_incognito" ||
    response.status === "incognito_failed"
  );
}

async function waitForResult(recordType, searchNumber, previousWebsiteAmount) {
  const activeClock = createActiveClock();
  const previousNormalized = normalizeAmountValue(previousWebsiteAmount);
  let candidateNormalized = "";
  let candidateSince = 0;
  let wrongPageSince = 0;

  try {
    while (activeClock.elapsed() < RESULT_TIMEOUT_MS) {
      const rejection = readRejection();
      if (rejection) {
        const snapshot = collectPageSnapshot(recordType, searchNumber);
        console.warn("[ZainChecker:RejectionSnapshot]", JSON.stringify(snapshot));
        return {
          ...rejection,
          current_url: window.location.href,
          page_title: document.title || "",
          page_snapshot: snapshot,
          hidden_seconds: activeClock.hiddenMilliseconds() / 1_000,
        };
      }

      const activeElapsed = activeClock.elapsed();
      if (document.readyState === "loading") {
        await waitForPageSignal(1_000);
        continue;
      }

      if (!isRequestedRecordPage(recordType, searchNumber)) {
        const currentUrl = window.location.href.toLowerCase();
        const isBusinessQuickPay = currentUrl.includes("business.zain.sa") && currentUrl.includes("quick-pay");
        const isExplicitWrongPage = !isBusinessQuickPay && (
          currentUrl.includes("/home") ||
          (currentUrl.includes("/dashboard") && !currentUrl.includes("quick-pay")) ||
          currentUrl.includes("/login") ||
          currentUrl === "https://app.sa.zain.com" ||
          currentUrl === "https://app.sa.zain.com/" ||
          currentUrl === "https://sa.zain.com" ||
          currentUrl === "https://sa.zain.com/"
        );
        if (!wrongPageSince) wrongPageSince = activeElapsed;
        const graceLimit = isExplicitWrongPage ? 4_000 : 15_000;
        if (activeElapsed - wrongPageSince >= graceLimit) {
          const snapshot = collectPageSnapshot(recordType, searchNumber);
          console.warn("[ZainChecker:RedirectSnapshot]", JSON.stringify(snapshot));
          return {
            status: "redirected",
            current_url: window.location.href,
            page_title: document.title || "",
            page_snapshot: snapshot,
            hidden_seconds: activeClock.hiddenMilliseconds() / 1_000,
          };
        }
        await waitForPageSignal(1_000);
        continue;
      }
      wrongPageSince = 0;

      const result = readCurrentResult(recordType, searchNumber);
      if (result && result.status !== "ok") {
        return {
          ...result,
          hidden_seconds: activeClock.hiddenMilliseconds() / 1_000,
        };
      }

      if (result?.status === "ok") {
        const currentNormalized = normalizeAmountValue(result.website_amount);
        if (currentNormalized !== candidateNormalized) {
          candidateNormalized = currentNormalized;
          candidateSince = activeElapsed;
        }

        const stableFor = activeElapsed - candidateSince;
        const isPreviousValue = Boolean(
          previousNormalized && currentNormalized === previousNormalized,
        );
        const isProvisionalZero = isZeroAmount(currentNormalized) && !result.settled;
        const requiredWait = Math.max(
          AMOUNT_STABILITY_MS,
          isPreviousValue ? UNCHANGED_AMOUNT_GRACE_MS : RESULT_SETTLE_MS,
          isProvisionalZero ? ZERO_AMOUNT_GRACE_MS : 0,
        );

        if (stableFor >= requiredWait) {
          return {
            ...result,
            provisional_zero: isProvisionalZero,
            hidden_seconds: activeClock.hiddenMilliseconds() / 1_000,
          };
        }
      }
      await waitForPageSignal(1_000);
    }
  } finally {
    activeClock.dispose();
  }

  const snapshot = collectPageSnapshot(recordType, searchNumber);
  console.warn("[ZainChecker:TimeoutSnapshot]", JSON.stringify(snapshot));
  return {
    status: "page_timeout",
    message: "Zain did not show an amount before the timeout.",
    current_url: window.location.href,
    page_title: document.title || "",
    page_snapshot: snapshot,
    hidden_seconds: activeClock.hiddenMilliseconds() / 1_000,
    debug: JSON.stringify(snapshot),
  };
}

async function navigateTask(task, previousWebsiteAmount, reload = false) {
  const workerState = await safeStorageGet("zainWorkerId");
  const effectiveWorker = detectWorkerIdentity() || workerState.zainWorkerId || "Worker 1 (Router)";
  const workerTag = effectiveWorker.includes("Proxy") ? "worker_2" : "worker_1";
  const directUrl = buildDirectUrl(task.record_type, task.search_number, workerTag);
  try {
    sessionStorage.clear();
  } catch {}
  console.log(`[ZainChecker:Content][${effectiveWorker}] navigateTask target:`, directUrl, "currentUrl:", window.location.href, "reload:", reload);
  const response = await sendMessage({
    type: "navigate_task",
    worker_id: effectiveWorker,
    task: {
      task_id: task.task_id,
      worker_id: effectiveWorker,
      worker_tag: workerTag,
      row_number: task.row_number,
      record_type: task.record_type,
      search_number: task.search_number,
    },
    previous_website_amount: previousWebsiteAmount,
    reload,
  });
  console.log(`[ZainChecker:Content][${effectiveWorker}] navigateTask response:`, JSON.stringify(response));
  if (response?.status !== "navigating") {
    throw new Error(response?.message || "The extension could not navigate the checker tab.");
  }
}

function createActiveClock() {
  const startedAt = Date.now();
  let hiddenAt = document.hidden ? startedAt : 0;
  let completedHiddenMilliseconds = 0;

  const onVisibilityChange = () => {
    const now = Date.now();
    if (document.hidden && !hiddenAt) {
      hiddenAt = now;
    } else if (!document.hidden && hiddenAt) {
      completedHiddenMilliseconds += now - hiddenAt;
      hiddenAt = 0;
    }
  };
  document.addEventListener("visibilitychange", onVisibilityChange);

  const hiddenMilliseconds = () =>
    completedHiddenMilliseconds + (hiddenAt ? Date.now() - hiddenAt : 0);

  return {
    elapsed: () => Date.now() - startedAt,
    hiddenMilliseconds,
    dispose: () =>
      document.removeEventListener("visibilitychange", onVisibilityChange),
  };
}

function waitForPageSignal(maximumWaitMilliseconds) {
  return new Promise((resolve) => {
    let settled = false;
    const finish = () => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      observer.disconnect();
      document.removeEventListener("visibilitychange", finish);
      document.removeEventListener("DOMContentLoaded", finish);
      resolve();
    };
    const observer = new MutationObserver(finish);
    if (document.documentElement) {
      observer.observe(document.documentElement, {
        subtree: true,
        childList: true,
        characterData: true,
        attributes: true,
        attributeFilter: ["value", "class", "style"],
      });
    } else {
      document.addEventListener("DOMContentLoaded", finish, { once: true });
    }
    document.addEventListener("visibilitychange", finish);
    const timer = setTimeout(finish, maximumWaitMilliseconds);
  });
}

function isRequestedRecordPage(recordType, searchNumber) {
  if (!isSupportedRecordType(recordType)) return false;
  const pageUrl = new URL(window.location.href);
  if (readPageSearchNumber(pageUrl, recordType) === String(searchNumber)) {
    return true;
  }
  const bodyText = normalizeDigits(String(document.body?.innerText || ""));
  if (searchNumber && bodyText.includes(String(searchNumber))) {
    return true;
  }
  return (
    recordType === "wallet" &&
    isRequestedWalletContent(String(searchNumber))
  );
}

function isRequestedWalletContent(searchNumber) {
  const bodyText = normalizeDigits(String(document.body?.innerText || ""));
  const hasQuickPayHeading =
    bodyText.includes("الدفع السريع") ||
    /quick\s*pay/i.test(bodyText);
  const hasAmountHeading =
    bodyText.includes("إجمالي المبلغ المطلوب") ||
    bodyText.includes("ادفع فاتورتك المتبقية") ||
    /total\s+(?:required|due)\s+amount|remaining\s+bill/i.test(bodyText);
  const escapedNumber = searchNumber.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const hasRequestedIdentity =
    new RegExp(`(?:^|[^0-9])${escapedNumber}(?:[^0-9]|$)`).test(bodyText) ||
    new RegExp(`${escapedNumber}\\s*\\|\\s*(?:مفوتر|postpaid)`, "i").test(bodyText);
  return hasQuickPayHeading && hasAmountHeading && hasRequestedIdentity;
}

function readPageSearchNumber(pageUrl, recordType) {
  if (recordType === "account") {
    if (pageUrl.searchParams.has("contract")) {
      return String(pageUrl.searchParams.get("contract") || "").trim();
    }
    if (pageUrl.pathname.endsWith("/contract-payment") || pageUrl.pathname.includes("quick-pay")) {
      return String(pageUrl.searchParams.get("contract") || "").trim();
    }
  }
  if (recordType === "wallet") {
    if (pageUrl.searchParams.has("account")) {
      return String(pageUrl.searchParams.get("account") || "").trim();
    }
    if (pageUrl.pathname.endsWith("/quickpay")) {
      return String(pageUrl.searchParams.get("account") || "").trim();
    }
  }
  return "";
}

function buildDirectUrl(recordType, searchNumber, workerTag = "") {
  const encoded = encodeURIComponent(searchNumber);
  const hashPart = workerTag ? `#worker=${workerTag}` : "";
  let base;
  if (recordType === "account") {
    base = `${ZAIN_PAYMENT_URL}?contract=${encoded}&language=ar${hashPart}`;
  } else if (recordType === "wallet") {
    base = `${ZAIN_QUICKPAY_URL}?account=${encoded}${hashPart}`;
  } else {
    throw new Error("Python returned an unsupported record type.");
  }
  return base;
}

function isSupportedRecordType(recordType) {
  return recordType === "account" || recordType === "wallet";
}

function readCurrentResult(recordType, contract) {
  const rejection = readRejection();
  if (rejection) return rejection;

  // If Business Flutter app is still loading splash screen, hold on
  const loadingEl = document.getElementById("loading");
  if (loadingEl && isVisible(loadingEl)) {
    return null;
  }

  if (!hasRequestedIdentity(recordType, contract)) return null;

  for (const selector of [
    "#customAmount",
    'input[name="customAmount"]',
    '#formSaveContractPaymentConfig input[name="amount"]',
    'input[name="amount"]',
    'input[name="paymentAmount"]',
    'input#amount',
  ]) {
    for (const field of document.querySelectorAll(selector)) {
      let value = String(field.value || "").trim();
      
      if (isVisible(field)) {
        if (!value) {
          const span = document.querySelector(".quickpay__custom-balance-input");
          if (span) {
            const spanText = String(span.innerText || "").trim();
            if (spanText) {
              value = spanText;
            }
          }
        }
        if (value) {
          return { status: "ok", website_amount: value };
        }
      }
    }
  }

  // Fallback for when Zain renders amount in span or container (e.g. 0.00 paid accounts or balance text)
  for (const selector of [
    ".quickpay__custom-balance-input",
    ".quickpay__custom-balance-input span",
    "#payBillValue span",
    "#payBillValue",
    ".due-amount",
    ".total-amount",
    "[data-amount]",
  ]) {
    for (const field of document.querySelectorAll(selector)) {
      const value = String(field.innerText || "").trim();
      if (isVisible(field) && value) {
        return { status: "ok", website_amount: value };
      }
    }
  }

  const rawBody = normalizeDigits(String(document.body?.innerText || ""));

  // Check settled state (English & Arabic)
  const isSettled = /already been settled|has already been settled|تسوية فاتورتك|تمت تسوية|تم سداد فاتورتك|تم سداد الفاتورة|لا توجد فواتير مستحقة|لا توجد مبالغ مستحقة/i.test(rawBody);

  // Match Total Required / Total Due / Amount Due (with optional currency symbols & commas)
  const amountMatch = rawBody.match(/(?:Total\s*Required|Total\s*(?:Due|Bill)|إجمالي\s*الاستحقاق|إجمالي\s*المبلغ\s*المطلوب|المبلغ\s*المطلوب|المبلغ\s*المستحق|ادفع\s*فاتورتك\s*المتبقية|إجمالي\s*المستحق)[^\d]{0,25}?([0-9]+(?:[,\u066c][0-9]{3})*(?:\.[0-9]{1,2})?)/i);
  if (amountMatch && amountMatch[1]) {
    const parsedAmount = amountMatch[1].replace(/[,\u066c\s]/g, "");
    if (!isNaN(Number(parsedAmount))) {
      return { status: "ok", website_amount: parsedAmount };
    }
  }

  if (isSettled) {
    return { status: "ok", website_amount: "0.00", settled: true };
  }

  // This Zain page explicitly asks for a payment amount but displays no
  // balance. Treat that confirmed empty state as zero after the normal
  // stability guard, instead of scraping unrelated text from the page.
  if (hasEmptyPaymentAmount(recordType)) {
    return { status: "ok", website_amount: "0.00", empty_amount: true };
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

  // Check for business portal error messages in bodyText
  const errorMatch = rawBody.match(/(?:Invalid\s*contract|Contract\s*not\s*found|No\s*records?\s*found|رقم\s*العقد\s*غير\s*صحيح|لم\s*يتم\s*العثور\s*على\s*العقد)/i);
  if (errorMatch) {
    return { status: "error", message: `Zain error: ${errorMatch[0]}` };
  }

  const alertBox = document.querySelector(".alert-danger");
  if (alertBox && isVisible(alertBox) && alertBox.innerText.includes("غير متوفرة")) {
    return { status: "website_error", message: "Service unavailable." };
  }

  return null;
}

function readCustomAmount() {
  for (const selector of ["#customAmount", 'input[name="customAmount"]']) {
    for (const field of document.querySelectorAll(selector)) {
      let value = String(field.value || "").trim();
      
      if (isVisible(field) && value) return value;
    }
  }
  return "";
}

function hasEmptyPaymentAmount(recordType) {
  const bodyText = normalizeDigits(String(document.body?.innerText || ""));
  if (recordType === "account") {
    return (
      bodyText.includes("سداد المدفوعات المتأخرة") ||
      bodyText.includes("أدخل المبلغ المطلوب") ||
      bodyText.includes("أدخل المبلغ") ||
      bodyText.includes("لا توجد مبالغ مستحقة") ||
      bodyText.includes("لا توجد فواتير مستحقة") ||
      bodyText.includes("المبلغ المراد دفعه") ||
      /already been settled|has already been settled|تسوية فاتورتك|تمت تسوية|no\s+(?:due\s+amount|bills?\s+due|outstanding)/i.test(bodyText)
    );
  }
  return (
    (bodyText.includes("إجمالي المبلغ المطلوب") &&
      bodyText.includes("ادفع فاتورتك المتبقية")) ||
    bodyText.includes("لا توجد فواتير مستحقة") ||
    bodyText.includes("لا توجد مبالغ مستحقة") ||
    /already been settled|has already been settled|تسوية فاتورتك|تمت تسوية|no\s+(?:due\s+amount|bills?\s+due|outstanding)/i.test(bodyText)
  );
}

function normalizeAmountValue(value) {
  return normalizeDigits(String(value || ""))
    .replace(/[\s٬,]/g, "")
    .replace("٫", ".")
    .trim();
}

function isZeroAmount(normalizedValue) {
  return Boolean(normalizedValue) && /^0*(?:\.0*)?$/.test(normalizedValue);
}

function hasRequestedIdentity(recordType, searchNumber) {
  const expected = String(searchNumber);
  const escaped = expected.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const bodyText = normalizeDigits(String(document.body?.innerText || ""));
  const visibleIdentity = new RegExp(
    `(?:^|[^0-9])${escaped}(?:[^0-9]|$)`,
  ).test(bodyText);
  if (visibleIdentity) return true;

  const selectors = recordType === "account"
    ? ["#txtContract", 'input[name="contract"]']
    : ["#txtAccount", 'input[name="account"]'];
  const inputIdentity = selectors.some((selector) =>
    Array.from(document.querySelectorAll(selector)).some(
      (field) => normalizeDigits(String(field.value || "").trim()) === expected,
    ),
  );
  if (inputIdentity) return true;

  // The contract and wallet pages do not consistently render the search number as text.
  // Its exact query parameter (?contract= or ?account=), new task ID, and
  // previous-amount guard together provide the identity check.
  return (
    isSupportedRecordType(recordType) &&
    readPageSearchNumber(new URL(window.location.href), recordType) === expected
  );
}

function readRejection() {
  const bodyText = String(document.body?.innerText || "");
  const hasF5Rejection = bodyText.includes("The requested URL was rejected.");
  const hasBotChallenge =
    bodyText.includes("This question is for testing whether you are a human visitor") ||
    bodyText.includes("prevent automated spam submission") ||
    bodyText.includes("What code is in the image?");
  const hasCaptchaInputs = Boolean(
    document.querySelector('input[name="answer"], #ans') &&
    document.querySelector('button#jar, input[type="submit"]')
  );
  const hasSupportId = /Your support ID is:\s*<?\s*([0-9]+)/i.test(bodyText);
  const hasCloudflareChallenge =
    (bodyText.includes("Just a moment...") || bodyText.includes("Verify you are human")) &&
    bodyText.length < 500;

  if (hasCloudflareChallenge) {
    return {
      status: "rejected",
      support_id: "cloudflare_challenge",
    };
  }

  if (!hasF5Rejection && !hasBotChallenge && !hasCaptchaInputs && !(hasSupportId && bodyText.length < 500)) {
    return null;
  }

  const match = bodyText.match(/Your support ID is:\s*<?\s*([0-9]+)/i);
  return {
    status: "rejected",
    support_id: match ? match[1] : "",
  };
}

function collectPageSnapshot(recordType = "", searchNumber = "") {
  try {
    const pageUrl = window.location.href;
    const title = document.title || "";
    const bodyText = normalizeDigits(String(document.body?.innerText || "")).trim();
    const amountInput = document.querySelector("#customAmount, input[name='customAmount']");
    const amountInputValue = amountInput ? String(amountInput.value || "") : null;
    const amountSpan = document.querySelector(".quickpay__custom-balance-input, #payBillValue");
    const amountSpanText = amountSpan ? String(amountSpan.innerText || "").trim() : null;
    const alertDanger = document.querySelector(".alert-danger, .invalid-feedback, .text-danger");
    const alertText = alertDanger ? String(alertDanger.innerText || "").trim() : null;

    let pageSearchNum = "";
    try {
      pageSearchNum = readPageSearchNumber(new URL(pageUrl), recordType);
    } catch {}

    const hasExpectedIdentity = Boolean(
      searchNumber && hasRequestedIdentity(recordType, searchNumber)
    );

    return {
      full_url: pageUrl,
      page_title: title,
      page_search_number: pageSearchNum,
      amount_input_value: amountInputValue,
      amount_span_text: amountSpanText,
      alert_text: alertText,
      has_expected_identity: hasExpectedIdentity,
      body_length: bodyText.length,
      body_text_sample: bodyText.slice(0, 1500),
      timestamp: new Date().toISOString(),
    };
  } catch (err) {
    return { error: String(err?.message || err) };
  }
}

function isVisible(element) {
  if (!element || !(element instanceof Element)) return false;
  try {
    const style = window.getComputedStyle(element);
    const rectangle = element.getBoundingClientRect();
    return (
      style.display !== "none" &&
      style.visibility !== "hidden" &&
      style.opacity !== "0" &&
      rectangle.width > 0 &&
      rectangle.height > 0
    );
  } catch {
    return false;
  }
}

function sameTask(activeTask, task) {
  return Boolean(
    activeTask &&
      activeTask.task_id === task.task_id &&
      activeTask.row_number === task.row_number &&
      activeTask.record_type === task.record_type &&
      activeTask.search_number === task.search_number,
  );
}

function normalizeDigits(value) {
  const source = "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹";
  const target = "01234567890123456789";
  return Array.from(value, (character) => {
    const index = source.indexOf(character);
    return index === -1 ? character : target[index];
  }).join("");
}

function sendMessage(message) {
  return chrome.runtime.sendMessage(message);
}

function isExtensionContextInvalidated(error) {
  return (
    !chrome.runtime?.id ||
    String(error?.message || error).includes("Extension context invalidated")
  );
}

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function waitUntilPageVisible() {
  if (!document.hidden) return 0;
  const hiddenAt = Date.now();
  await new Promise((resolve) => {
    const onVisibilityChange = () => {
      if (document.hidden) return;
      document.removeEventListener("visibilitychange", onVisibilityChange);
      resolve();
    };
    document.addEventListener("visibilitychange", onVisibilityChange);
  });
  await delay(750);
  return Date.now() - hiddenAt;
}
