const BRIDGE_URL = "http://127.0.0.1:8766";
const BRIDGE_TOKEN =
  "614e3c6720854e8cb09fa5bc41c609a184cc75123924446aa306f08f0efa085b";
const ZAIN_PAYMENT_URL = "https://business.zain.sa/dashboard/quick-pay";
const ZAIN_QUICKPAY_URL = "https://app.sa.zain.com/ar/quickpay";
let incognitoHandoffPromise = null;
const SESSION_KEEPER_KEY = "zainCheckerSessionKeeperWindowId";

async function handleNavigationEvent(details) {
  if (details.frameId !== 0) return;
  const tab = await chrome.tabs.get(details.tabId).catch(() => null);
  if (!tab || tab.incognito !== true) return;
  if (!(await isTargetTab(details.tabId, { tab }))) return;
  try {
    await chrome.tabs.update(details.tabId, { autoDiscardable: false });
    await ensureContentScript(details.tabId);
    await wakeTab(details.tabId);
  } catch (error) {
    await setStatus(
      `Navigation error: ${String(error?.message || error)}`,
    );
  }
}

chrome.webNavigation.onCompleted.addListener(handleNavigationEvent, {
  url: [
    { hostEquals: "business.zain.sa", schemes: ["https"] },
    { hostEquals: "app.sa.zain.com", schemes: ["https"] },
  ],
});
chrome.webNavigation.onHistoryStateUpdated.addListener(handleNavigationEvent, {
  url: [
    { hostEquals: "business.zain.sa", schemes: ["https"] },
    { hostEquals: "app.sa.zain.com", schemes: ["https"] },
  ],
});

const memoryStorage = {};

async function safeStorageGet(keys) {
  const isArray = Array.isArray(keys);
  const keyList = isArray ? keys : (typeof keys === "string" ? [keys] : Object.keys(keys || {}));
  let result = {};

  try {
    result = await chrome.storage.local.get(keys);
  } catch (error) {
    console.warn("[ZainChecker:SW] Storage get failed, using memory fallback:", error);
    if (String(error?.message || error).includes("Corruption")) {
      try {
        await chrome.storage.local.clear();
      } catch {}
    }
  }

  for (const k of keyList) {
    if (result[k] === undefined && memoryStorage[k] !== undefined) {
      result[k] = memoryStorage[k];
    }
  }
  return result;
}

async function safeStorageSet(items) {
  if (!items || typeof items !== "object") return;
  Object.assign(memoryStorage, items);

  try {
    await chrome.storage.local.set(items);
  } catch (error) {
    console.warn("[ZainChecker:SW] Storage set failed, retained in memory:", error);
    if (String(error?.message || error).includes("Corruption")) {
      try {
        await chrome.storage.local.clear();
      } catch {}
    }
  }
}

async function safeStorageRemove(keys) {
  const keyList = Array.isArray(keys) ? keys : [keys];
  for (const k of keyList) {
    delete memoryStorage[k];
  }
  try {
    await chrome.storage.local.remove(keys);
  } catch (error) {
    console.warn("[ZainChecker:SW] Storage remove failed:", error);
  }
}

function isAllowedCheckerTab(sender) {
  if (!sender || !sender.tab) return false;
  // STRICT GUARD: Only incognito tabs are ever allowed! Never touch normal/personal tabs!
  if (sender.tab.incognito !== true) {
    console.log("[ZainChecker:SW] Rejected non-incognito tab:", sender.tab.id);
    return false;
  }
  const url = String(sender.tab.url || sender.url || "");
  return url.startsWith("https://business.zain.sa/") || url.startsWith("https://app.sa.zain.com/") || url.startsWith("https://sa.zain.com/");
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  handleMessage(message, sender)
    .then(sendResponse)
    .catch(async (error) => {
      const detail = String(error?.message || error);
      console.error("[ZainChecker:SW] Message handler error for", message?.type, detail, error?.stack);
      try {
        await setStatus(`Error: ${detail}`);
      } catch (statusError) {
        console.warn("[ZainChecker:SW] Could not write error status:", statusError);
      }
      try {
        sendResponse({ status: "bridge_error", message: detail });
      } catch (respError) {
        console.warn("[ZainChecker:SW] Could not send bridge_error response:", respError);
      }
    });
  return true;
});

async function handleMessage(message, sender) {
  console.log("[ZainChecker:SW] Message:", message?.type, "tabId:", sender.tab?.id, "incognito:", sender.tab?.incognito, "url:", sender.url || sender.tab?.url, "worker_id:", message?.worker_id);

  if (message?.worker_id) {
    await safeStorageSet({ zainWorkerId: message.worker_id });
  }

  if (message?.type === "adopt_tab") {
    const tabId = sender.tab?.id;
    if (Number.isInteger(tabId) && isAllowedCheckerTab(sender)) {
      const workerId = message.worker_id || (await safeStorageGet("zainWorkerId")).zainWorkerId || "Worker 1 (Router)";
      await safeStorageSet({
        zainCheckerRunning: true,
        zainCheckerTabId: tabId,
        zainWorkerId: workerId,
      });
      try {
        const task = await fetchJson(`${BRIDGE_URL}/task?worker=${encodeURIComponent(workerId)}`, { worker_id: workerId });
        if (task?.status === "check") {
          await ensureHandoffAcknowledged(task);
        }
      } catch {
        // Non-fatal
      }
      await closeSessionKeeperWindow();
      return { status: "adopted" };
    }
    return { status: "ignored" };
  }

  if (message?.type === "auto_start") {
    const tabId = sender.tab?.id;
    const windowId = sender.tab?.windowId;
    if (!Number.isInteger(tabId)) {
      return { status: "ignored", reason: "no_tab_id", sender_tab: sender.tab };
    }
    if (!isAllowedCheckerTab(sender)) {
      return { status: "ignored", reason: "not_allowed", sender_tab: sender.tab, sender_url: sender.url };
    }

    const workerId = message.worker_id || (await safeStorageGet("zainWorkerId")).zainWorkerId || "Worker 1 (Router)";
    await safeStorageSet({ zainWorkerId: workerId });

    let task;
    try {
      task = await fetchJson(`${BRIDGE_URL}/task?worker=${encodeURIComponent(workerId)}`, { worker_id: workerId });
    } catch {
      return { status: "bridge_offline" };
    }

    if (task?.status !== "check") {
      return { status: "no_task" };
    }

    await safeStorageSet({
      zainCheckerRunning: false,
      zainCheckerStatus: "Closing other Incognito windows...",
      zainCheckerActiveTask: null,
    });
    if (Number.isInteger(windowId)) {
      await closeIncognitoWindowsExcept(windowId);
      await closeExtraTabsInWindow(windowId, tabId);
    }
    await safeStorageSet({
      zainCheckerRunning: true,
      zainCheckerTabId: tabId,
      zainWorkerId: workerId,
      zainCheckerStatus: `Starting ${task.record_type} ${task.search_number} automatically...`,
      zainCheckerActiveTask: {
        task_id: task.task_id,
        worker_id: workerId,
        row_number: task.row_number,
        record_type: task.record_type,
        search_number: task.search_number,
        stage: "navigating",
        previous_website_amount: null,
      },
    });
    await ensureHandoffAcknowledged(task);
    await closeSessionKeeperWindow();
    return { status: "started" };
  }

  if (message?.type === "start") {
    const tabId = Number(message.tabId);
    if (!Number.isInteger(tabId)) {
      throw new Error("No active Chrome tab was found.");
    }

    const tab = await chrome.tabs.get(tabId);
    if (!tab.incognito) {
      const task = await fetchJson(`${BRIDGE_URL}/task`);
      if (task.status !== "check") {
        throw new Error(
          task.message || "Python did not provide a record to check.",
        );
      }
      await runIncognitoHandoff(task);
      return { status: "started" };
    }

    await safeStorageSet({
      zainCheckerRunning: true,
      zainCheckerTabId: tabId,
      zainCheckerStatus: "Starting...",
      zainCheckerActiveTask: null,
    });

    if (!String(tab.url || "").startsWith("https://app.sa.zain.com/") && !String(tab.url || "").startsWith("https://business.zain.sa/")) {
      const task = await fetchJson(`${BRIDGE_URL}/task`);
      if (task.status !== "check") {
        throw new Error(task.message || "Python did not provide a record to check.");
      }
      await ensureHandoffAcknowledged(task);
      await chrome.tabs.update(tabId, {
        url: buildDirectUrl(task.record_type, task.search_number),
      });
    } else {
      try {
        const task = await fetchJson(`${BRIDGE_URL}/task`);
        if (task?.status === "check") {
          await ensureHandoffAcknowledged(task);
        }
      } catch {}
      await ensureContentScript(tabId);
      await wakeTab(tabId);
    }
    return { status: "started" };
  }

  if (message?.type === "stop") {
    const state = await safeStorageGet("zainCheckerTabId");
    await safeStorageSet({
      zainCheckerRunning: false,
      zainCheckerStatus: "Stopped.",
      zainCheckerActiveTask: null,
    });
    if (Number.isInteger(state.zainCheckerTabId)) {
      await wakeTab(state.zainCheckerTabId);
    }
    return { status: "stopped" };
  }

  if (message?.type === "navigate_task") {
    if (!(await isTargetTab(sender.tab?.id, sender))) {
      console.log("[ZainChecker:SW] navigate_task rejected: not target tab");
      return { status: "inactive" };
    }
    const task = normalizeTask(message.task);
    const tabId = sender.tab.id;
    const storedWorker = await safeStorageGet("zainWorkerId");
    const effectiveWorker = message.worker_id || storedWorker.zainWorkerId || "Worker 1 (Router)";
    const workerTag = message.task?.worker_tag || (effectiveWorker.includes("Proxy") ? "worker_2" : "worker_1");
    console.log(`[ZainChecker:SW][${effectiveWorker}] navigate_task executing for search_number:`, task.search_number, "reload:", message.reload, "workerTag:", workerTag);
    await safeStorageSet({
      zainCheckerActiveTask: {
        ...task,
        worker_id: effectiveWorker,
        stage: "navigating",
        previous_website_amount: String(
          message.previous_website_amount || "",
        ),
      },
      zainCheckerStatus: `Navigating to ${task.record_type} ${task.search_number}...`,
    });
    const directUrl = buildDirectUrl(task.record_type, task.search_number, workerTag);
    const currentTab = await chrome.tabs.get(tabId).catch(() => null);
    if (message.reload === true) {
      console.log("[ZainChecker:SW] Reloading tabId:", tabId);
      await chrome.tabs.reload(tabId);
    } else {
      console.log("[ZainChecker:SW] Updating same tabId:", tabId, "to directUrl:", directUrl);
      await chrome.tabs.update(tabId, { url: directUrl, autoDiscardable: false });
    }
    return { status: "navigating" };
  }

  if (message?.type === "poll_task") {
    if (!(await isTargetTab(sender.tab?.id, sender))) {
      console.log("[ZainChecker:SW] poll_task rejected: isTargetTab returned false");
      return { status: "inactive" };
    }

    const workerId = message.worker_id || (await safeStorageGet("zainWorkerId")).zainWorkerId || "Worker 1 (Router)";
    await safeStorageSet({ zainWorkerId: workerId });

    let response;
    try {
      response = await fetchJson(`${BRIDGE_URL}/task?worker=${encodeURIComponent(workerId)}`, { worker_id: workerId });
      console.log(`[ZainChecker:SW][${workerId}] poll_task response from bridge:`, JSON.stringify(response));
    } catch (error) {
      const detail =
        `Cannot connect to the Python bridge: ${String(error?.message || error)}`;
      console.error("[ZainChecker:SW] poll_task bridge error:", detail);
      await setStatus(detail);
      return { status: "bridge_error", message: detail };
    }

    if (response?.status === "check") {
      await ensureHandoffAcknowledged(response);

      const isService2Cycle =
        response.cycle_mode === "SERVICE_2" ||
        response.record_type === "wallet" ||
        String(response.search_number || "").startsWith("2");
      const cycleId = Number(response.service_cycle_id || 0);
      const stateCycle = await safeStorageGet("zainCheckerLastRefreshedCycleId");
      const lastRefreshedCycleId = Number(stateCycle.zainCheckerLastRefreshedCycleId || 0);

      if (
        isService2Cycle &&
        cycleId > 0 &&
        response.refresh_once === true &&
        lastRefreshedCycleId !== cycleId
      ) {
        await safeStorageSet({ zainCheckerLastRefreshedCycleId: cycleId });
        await acknowledgeCycleRefreshed(cycleId, response.task_id);
        response.do_single_refresh = true;
      } else {
        response.do_single_refresh = false;
      }
    }

    await updateStatusFromBridge(response);
    return response;
  }

  if (message?.type === "submit_result") {
    if (!(await isTargetTab(sender.tab?.id, sender))) {
      console.log("[ZainChecker:SW] submit_result rejected: isTargetTab returned false");
      return { status: "inactive" };
    }

    const workerId = message.worker_id || (await safeStorageGet("zainWorkerId")).zainWorkerId || "Worker 1 (Router)";
    console.log(`[ZainChecker:SW][${workerId}] Submitting result:`, JSON.stringify(message.result));
    const response = await fetchJson(`${BRIDGE_URL}/result`, {
      method: "POST",
      worker_id: workerId,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...message.result, worker_id: workerId }),
    });
    console.log(`[ZainChecker:SW][${workerId}] Submit response from bridge:`, JSON.stringify(response));
    await updateStatusFromBridge(response);
    return response;
  }

  return { status: "ignored" };
}

async function fetchJson(url, options = {}) {
  const storedWorker = await safeStorageGet("zainWorkerId");
  const workerId = options.worker_id || storedWorker.zainWorkerId || "Worker 1 (Router)";
  const headers = {
    "X-Zain-Bridge-Token": BRIDGE_TOKEN,
    "X-Worker-Id": workerId,
    ...(options.headers || {}),
  };
  const response = await fetch(url, {
    cache: "no-store",
    ...options,
    headers,
  });
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.message || `Bridge returned HTTP ${response.status}.`);
  }
  return payload;
}

async function isTargetTab(tabId, sender) {
  if (sender?.tab && sender.tab.incognito !== true) {
    return false;
  }
  const state = await safeStorageGet([
    "zainCheckerRunning",
    "zainCheckerTabId",
  ]);
  if (!state.zainCheckerRunning) {
    console.log("[ZainChecker:SW] isTargetTab: false because zainCheckerRunning is false");
    return false;
  }
  if (!Number.isInteger(tabId)) {
    console.log("[ZainChecker:SW] isTargetTab: false because tabId not integer:", tabId);
    return false;
  }
  if (tabId === state.zainCheckerTabId) {
    return true;
  }
  if (isAllowedCheckerTab(sender)) {
    await safeStorageSet({ zainCheckerTabId: tabId });
    return true;
  }
  console.log("[ZainChecker:SW] isTargetTab: false for tabId:", tabId, "stateTabId:", state.zainCheckerTabId);
  return false;
}

async function updateStatusFromBridge(response) {
  if (response.status === "check") {
    await setStatus(
      `Checking ${response.sequence} of ${response.total}: ${response.search_number}`,
    );
    return;
  }
  if (response.status === "waiting") {
    await setStatus(
      `Waiting ${response.retry_after_seconds} second(s). ` +
        `Checked ${response.checked ?? 0} of ${response.total ?? "?"}.`,
    );
    return;
  }
  if (response.status === "ok") {
    return;
  }
  if (response.status === "recheck_mismatch") {
    await setStatus(
      `Possible mismatch. Rechecking the same account in ` +
        `${response.retry_after_seconds} second(s)...`,
    );
    return;
  }
  if (response.status === "reload_mismatch") {
    await setStatus(
      `Mismatch remained. Reloading the same account for one final check...`,
    );
    return;
  }
  if (response.status === "retry_redirect") {
    await setStatus(
      `The page did not settle on the requested record. Retrying the same ` +
        `search number once...`,
    );
    return;
  }
  if (response.status === "network_wait") {
    await setStatus(
      `Zain requests paused for safety. Waiting for confirmation in the dashboard. ` +
        `Search number ${response.search_number} was not counted.`,
    );
    return;
  }
  if (response.status === "complete") {
    await safeStorageSet({
      zainCheckerRunning: false,
      zainCheckerStatus: `Complete: ${response.total} account(s).`,
      zainCheckerActiveTask: null,
    });
    return;
  }
  if (response.status === "restarting_instance") {
    await setStatus(
      `Restarting Chrome instance for clean session... Search number: ${response.search_number || ""}`
    );
    return;
  }
  if (response.status === "worker_paused") {
    await setStatus(
      `${response.worker_id || "Worker"} is paused due to IP block. Resume from dashboard anytime.`
    );
    return;
  }
  if (response.status === "open_incognito") {
    await runIncognitoHandoff(response);
    return;
  }
  if (response.status === "incognito_failed") {
    await safeStorageSet({
      zainCheckerRunning: false,
      zainCheckerStatus: "Closing the rejected Incognito session...",
      zainCheckerActiveTask: null,
    });
    await closeAllIncognitoWindows();
    await safeStorageSet({
      zainCheckerStatus: `Error: ${response.message}`,
    });
    return;
  }
  if (response.status === "error") {
    await safeStorageSet({
      zainCheckerRunning: false,
      zainCheckerStatus: `Error: ${response.message}`,
    });
  }
}

async function setStatus(status) {
  try {
    await safeStorageSet({ zainCheckerStatus: status });
  } catch (error) {
    console.warn("[ZainChecker:SW] setStatus non-fatal error:", error);
  }
}

async function runIncognitoHandoff(response) {
  const handoff =
    incognitoHandoffPromise || openFreshIncognitoChecker(response);
  incognitoHandoffPromise = handoff;
  try {
    await handoff;
  } finally {
    if (incognitoHandoffPromise === handoff) {
      incognitoHandoffPromise = null;
    }
  }
}

async function clearZainSessionData() {
  try {
    console.log("[ZainChecker:SW] Purging Zain session data (cookies and storage)...");

    if (chrome.browsingData?.remove) {
      await chrome.browsingData.remove(
        {
          origins: [
            "https://business.zain.sa",
            "https://app.sa.zain.com",
            "https://sa.zain.com",
          ],
        },
        {
          cache: true,
          cookies: true,
          localStorage: true,
          serviceWorkers: true,
          indexedDB: true,
        }
      ).catch((e) => console.warn("[ZainChecker:SW] browsingData.remove warning:", e));
    }

    if (chrome.cookies?.getAll) {
      const stores = await chrome.cookies.getAllCookieStores().catch(() => [{ id: "0" }, { id: "1" }]);
      for (const store of stores) {
        const storeCookies = await chrome.cookies.getAll({ storeId: store.id }).catch(() => []);
        for (const cookie of storeCookies) {
          if (cookie.domain && (cookie.domain.includes("zain.com") || cookie.domain.includes("zain.sa"))) {
            const domain = cookie.domain.startsWith(".") ? cookie.domain.substring(1) : cookie.domain;
            const cookieUrl = `${cookie.secure ? "https://" : "http://"}${domain}${cookie.path || "/"}`;
            await chrome.cookies.remove({
              url: cookieUrl,
              name: cookie.name,
              storeId: store.id,
            }).catch(() => {});
          }
        }
      }
    }
    console.log("[ZainChecker:SW] Zain session data successfully purged.");
  } catch (err) {
    console.warn("[ZainChecker:SW] clearZainSessionData non-fatal error:", err);
  }
}

async function openFreshIncognitoChecker(response) {
  const taskId = String(response.task_id || "").trim();
  const rowNumber = Number(response.row_number);
  const recordType = String(response.record_type || "").trim();
  const searchNumber = String(response.search_number || "").trim();
  if (
    !taskId ||
    !Number.isInteger(rowNumber) ||
    rowNumber <= 0 ||
    !isSupportedRecordType(recordType) ||
    !/^\d+$/.test(searchNumber)
  ) {
    throw new Error("Python returned an invalid record for Incognito retry.");
  }

  const incognitoAllowed = await new Promise((resolve) => {
    chrome.extension.isAllowedIncognitoAccess(resolve);
  });
  if (!incognitoAllowed) {
    await safeStorageSet({
      zainCheckerRunning: false,
      zainCheckerStatus:
        "Enable Allow in Incognito in the extension Details page, then start again.",
      zainCheckerActiveTask: null,
    });
    throw new Error(
      "Automatic Incognito access is disabled. Open chrome://extensions, " +
        "select Zain Account and Wallet Checker > Details, and enable Allow in Incognito.",
    );
  }

  const storedState = await safeStorageGet(["zainWorkerId", "zainCheckerTabId"]);
  const effectiveWorker = storedState.zainWorkerId || "Worker 1 (Router)";
  const workerTag = effectiveWorker.includes("Proxy") ? "worker_2" : "worker_1";
  const directUrl = buildDirectUrl(recordType, searchNumber, workerTag);

  // 1. Identify old window to close
  let oldWindowId = null;
  if (storedState.zainCheckerTabId) {
    try {
      const oldTab = await chrome.tabs.get(storedState.zainCheckerTabId);
      oldWindowId = oldTab?.windowId || null;
    } catch {}
  }

  // Clear any dirty session cookies and storage before opening replacement window
  await clearZainSessionData();

  // 2. Open new window directly with the URL that was failing
  const createdWindow = await chrome.windows.create({
    url: directUrl,
    incognito: true,
    focused: true,
  });
  let createdTabs = createdWindow?.tabs || [];
  if (createdTabs.length === 0 && Number.isInteger(createdWindow?.id)) {
    createdTabs = await chrome.tabs.query({ windowId: createdWindow.id });
  }

  const tabId = createdTabs[0]?.id;
  if (!Number.isInteger(tabId)) {
    throw new Error("Chrome opened a new window but did not return its tab.");
  }

  // Acknowledge immediately so Python knows the window is open and never spawns a new Chrome process!
  try {
    await acknowledgeHandoff({
      task_id: taskId,
      row_number: rowNumber,
      record_type: recordType,
      search_number: searchNumber,
    });
  } catch (ackErr) {
    console.warn("[ZainChecker:SW] Immediate handoff ack warning:", ackErr);
  }

  // 3. Close ONLY the old window that had the problem (leaves other worker intact)
  if (oldWindowId && oldWindowId !== createdWindow.id) {
    try {
      await chrome.windows.remove(oldWindowId);
    } catch {}
  }

  if (Number.isInteger(createdWindow?.id)) {
    await closeExtraTabsInWindow(createdWindow.id, tabId);
  }

  await safeStorageSet({
    zainCheckerRunning: true,
    zainCheckerTabId: tabId,
    zainCheckerStatus: `Checking ${recordType} ${searchNumber} in new window...`,
    zainCheckerActiveTask: {
      task_id: taskId,
      row_number: rowNumber,
      record_type: recordType,
      search_number: searchNumber,
      stage: "navigating",
      previous_website_amount: null,
    },
  });
  await chrome.tabs.update(tabId, { autoDiscardable: false });
}

async function closeAllIncognitoWindows() {
  await closeIncognitoWindowsExcept(null);
}

async function closeExtraTabsInWindow(windowId, keptTabId) {
  if (!Number.isInteger(windowId) || !Number.isInteger(keptTabId)) return;
  try {
    const tabsInWin = await chrome.tabs.query({ windowId });
    const extraTabIds = tabsInWin
      .filter((t) => Number.isInteger(t.id) && t.id !== keptTabId)
      .map((t) => t.id);
    if (extraTabIds.length > 0) {
      await chrome.tabs.remove(extraTabIds);
    }
  } catch {}
}

async function ensureSessionKeeperWindow() {
  const state = await safeStorageGet(SESSION_KEEPER_KEY);
  const existingId = state[SESSION_KEEPER_KEY];
  if (Number.isInteger(existingId)) {
    try {
      const existingWindow = await chrome.windows.get(existingId);
      if (!existingWindow.incognito) return existingId;
    } catch {
      // The stored keeper no longer exists; create a replacement below.
    }
  }

  const keeper = await chrome.windows.create({
    url: "about:blank",
    incognito: false,
    focused: false,
    state: "minimized",
    type: "normal",
  });
  if (!Number.isInteger(keeper?.id)) {
    throw new Error("Chrome could not create the temporary session keeper window.");
  }
  await safeStorageSet({ [SESSION_KEEPER_KEY]: keeper.id });
  return keeper.id;
}

async function closeSessionKeeperWindow() {
  const state = await safeStorageGet(SESSION_KEEPER_KEY);
  const keeperId = state[SESSION_KEEPER_KEY];
  await safeStorageRemove(SESSION_KEEPER_KEY);
  if (!Number.isInteger(keeperId)) return;
  try {
    await chrome.windows.remove(keeperId);
  } catch {
    // It was already closed manually or by Chrome.
  }
}

async function closeIncognitoWindowsExcept(keptWindowId) {
  for (let attempt = 1; attempt <= 10; attempt += 1) {
    // 1. Explicitly close all Incognito tabs first
    try {
      const allTabs = await chrome.tabs.query({});
      const incognitoTabIds = allTabs
        .filter(
          (tab) =>
            tab.incognito &&
            Number.isInteger(tab.id) &&
            tab.windowId !== keptWindowId,
        )
        .map((tab) => tab.id);
      if (incognitoTabIds.length > 0) {
        await chrome.tabs.remove(incognitoTabIds).catch(() => {});
      }
    } catch {}

    // 2. Close all Incognito windows except keptWindowId
    const existingWindows = await chrome.windows.getAll();
    const incognitoWindowIds = existingWindows
      .filter(
        (existingWindow) =>
          existingWindow.incognito &&
          Number.isInteger(existingWindow.id) &&
          existingWindow.id !== keptWindowId,
      )
      .map((existingWindow) => existingWindow.id);
    if (incognitoWindowIds.length === 0) {
      // Extra settle delay after confirming all Incognito tabs and windows are closed
      // so Chrome completely flushes the Incognito profile memory/cookies.
      await new Promise((resolve) => setTimeout(resolve, 350));
      return;
    }

    await Promise.allSettled(
      incognitoWindowIds.map((windowId) => chrome.windows.remove(windowId)),
    );
    await new Promise((resolve) => setTimeout(resolve, 300));
  }

  const remainingWindows = await chrome.windows.getAll();
  if (
    remainingWindows.some(
      (existingWindow) =>
        existingWindow.incognito && existingWindow.id !== keptWindowId,
    )
  ) {
    console.warn("Some older Incognito windows may still be closing.");
  }
}

async function wakeTab(tabId) {
  try {
    await chrome.tabs.sendMessage(tabId, { type: "wake" });
  } catch {
    // The content script starts automatically after a Zain page loads.
  }
}

async function ensureContentScript(tabId) {
  try {
    const response = await chrome.tabs.sendMessage(tabId, { type: "ping" });
    if (response?.status === "ready") return;
  } catch {
    // Existing tabs do not receive a newly installed content script.
  }

  await chrome.scripting.executeScript({
    target: { tabId },
    files: ["content.js"],
  });
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

let handoffAcknowledgedTaskId = null;

async function acknowledgeHandoff(task) {
  const response = await fetchJson(`${BRIDGE_URL}/handoff-ready`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(task),
  });
  if (
    response.status !== "handoff_acknowledged" &&
    response.status !== "complete"
  ) {
    throw new Error("Python rejected the Incognito handoff acknowledgement.");
  }
}

async function acknowledgeCycleRefreshed(serviceCycleId, taskId) {
  try {
    await fetchJson(`${BRIDGE_URL}/cycle-refreshed`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        service_cycle_id: serviceCycleId,
        task_id: taskId || "",
      }),
    });
  } catch (error) {
    console.warn("[ZainChecker:SW] Non-fatal error acknowledging cycle refresh:", error);
  }
}

async function ensureHandoffAcknowledged(task) {
  if (!task || task.status !== "check" || !task.task_id) return;
  if (handoffAcknowledgedTaskId === task.task_id) return;
  try {
    await acknowledgeHandoff({
      task_id: task.task_id,
      row_number: task.row_number,
      record_type: task.record_type,
      search_number: task.search_number,
    });
    handoffAcknowledgedTaskId = task.task_id;
  } catch (error) {
    // Non-fatal if already acknowledged or offline
  }
}

function normalizeTask(task) {
  const normalized = {
    task_id: String(task?.task_id || "").trim(),
    row_number: Number(task?.row_number),
    record_type: String(task?.record_type || "").trim(),
    search_number: String(task?.search_number || "").trim(),
  };
  if (
    !normalized.task_id ||
    !Number.isInteger(normalized.row_number) ||
    normalized.row_number <= 0 ||
    !isSupportedRecordType(normalized.record_type) ||
    !/^\d+$/.test(normalized.search_number)
  ) {
    throw new Error("Python returned an invalid navigation task.");
  }
  return normalized;
}
