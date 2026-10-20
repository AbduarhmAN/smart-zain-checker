const statusElement = document.querySelector("#status");
const startButton = document.querySelector("#start");
const stopButton = document.querySelector("#stop");

startButton.addEventListener("click", async () => {
  startButton.disabled = true;
  statusElement.textContent = "Starting...";

  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!Number.isInteger(tab?.id)) {
      throw new Error("No active Chrome tab was found.");
    }

    const response = await chrome.runtime.sendMessage({
      type: "start",
      tabId: tab.id,
    });
    if (response?.status !== "started") {
      throw new Error(response?.message || "Could not start.");
    }

    statusElement.textContent = "Started. Return to the Zain tab.";
    setTimeout(() => window.close(), 700);
  } catch (error) {
    statusElement.textContent = `Start failed: ${String(error?.message || error)}`;
    startButton.disabled = false;
  }
});

stopButton.addEventListener("click", async () => {
  await chrome.runtime.sendMessage({ type: "stop" });
  await refreshStatus();
});

chrome.storage.onChanged.addListener((changes, areaName) => {
  if (areaName === "local" && changes.zainCheckerStatus) {
    statusElement.textContent = changes.zainCheckerStatus.newValue;
  }
});

async function refreshStatus() {
  try {
    const state = await chrome.storage.local.get("zainCheckerStatus");
    statusElement.textContent = state.zainCheckerStatus || "Ready.";
  } catch {
    statusElement.textContent = "Ready.";
  }
}

refreshStatus();
