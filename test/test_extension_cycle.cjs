const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const swCode = fs.readFileSync(
  path.join(__dirname, '..', 'chrome_extension', 'service-worker.js'),
  'utf8'
);

async function runExtensionCycleTest() {
  const storage = {};
  let removedTabIds = [];
  let removedWindowIds = [];
  let createdWindows = [];
  let reloadedTabIds = [];
  let updatedTabs = [];
  let cycleRefreshedAcks = [];

  let currentWindows = [
    { id: 10, incognito: false },
    { id: 20, incognito: true },
    { id: 21, incognito: true },
  ];
  let currentTabs = [
    { id: 100, windowId: 10, incognito: false, url: 'about:blank' },
    { id: 200, windowId: 20, incognito: true, url: 'https://app.sa.zain.com/ar/contract-payment?contract=10001' },
    { id: 201, windowId: 21, incognito: true, url: 'https://app.sa.zain.com/ar/quickpay?account=20001' },
  ];

  let nextBridgeTask = null;

  const chromeMock = {
    webNavigation: {
      onCompleted: { addListener: () => {} },
      onHistoryStateUpdated: { addListener: () => {} },
    },
    runtime: {
      onMessage: { addListener: () => {} },
    },
    extension: {
      isAllowedIncognitoAccess: (cb) => cb(true),
    },
    storage: {
      local: {
        get: async (keys) => {
          if (typeof keys === 'string') return { [keys]: storage[keys] };
          if (Array.isArray(keys)) {
            const out = {};
            keys.forEach((k) => { out[k] = storage[k]; });
            return out;
          }
          return { ...storage };
        },
        set: async (items) => {
          Object.assign(storage, items);
        },
        remove: async (keys) => {
          const arr = Array.isArray(keys) ? keys : [keys];
          arr.forEach((k) => delete storage[k]);
        },
        clear: async () => {
          Object.keys(storage).forEach((k) => delete storage[k]);
        },
      },
    },
    windows: {
      get: async (id) => {
        const w = currentWindows.find((x) => x.id === id);
        if (!w) throw new Error('Window not found');
        return w;
      },
      getAll: async () => [...currentWindows],
      remove: async (id) => {
        removedWindowIds.push(id);
        currentWindows = currentWindows.filter((w) => w.id !== id);
        currentTabs = currentTabs.filter((t) => t.windowId !== id);
      },
      create: async (opts) => {
        const newWinId = 300 + createdWindows.length;
        const newTabId = 3000 + createdWindows.length;
        const newWin = {
          id: newWinId,
          incognito: Boolean(opts.incognito),
          tabs: [{ id: newTabId, windowId: newWinId, incognito: Boolean(opts.incognito), url: opts.url }],
        };
        createdWindows.push(newWin);
        currentWindows.push({ id: newWinId, incognito: Boolean(opts.incognito) });
        currentTabs.push({ id: newTabId, windowId: newWinId, incognito: Boolean(opts.incognito), url: opts.url });
        // Also simulate an extra blank tab in the window to test closeExtraTabsInWindow
        if (opts.incognito) {
          currentTabs.push({ id: newTabId + 99, windowId: newWinId, incognito: true, url: 'chrome://newtab' });
        }
        return newWin;
      },
    },
    tabs: {
      get: async (id) => currentTabs.find((t) => t.id === id) || null,
      query: async (q) => {
        if (q && Number.isInteger(q.windowId)) {
          return currentTabs.filter((t) => t.windowId === q.windowId);
        }
        return [...currentTabs];
      },
      remove: async (ids) => {
        const list = Array.isArray(ids) ? ids : [ids];
        removedTabIds.push(...list);
        currentTabs = currentTabs.filter((t) => !list.includes(t.id));
      },
      update: async (tabId, props) => {
        updatedTabs.push({ tabId, props });
        const t = currentTabs.find((x) => x.id === tabId);
        if (t && props.url) t.url = props.url;
        return t;
      },
      reload: async (tabId) => {
        reloadedTabIds.push(tabId);
      },
      sendMessage: async () => ({ status: 'ready' }),
    },
    scripting: {
      executeScript: async () => {},
    },
  };

  const sandbox = {
    chrome: chromeMock,
    console,
    setTimeout: (fn) => { fn(); return 1; },
    clearTimeout: () => {},
    URL,
    encodeURIComponent,
    Promise,
    Number,
    String,
    Boolean,
    Array,
    Object,
    fetch: async (url, opts) => {
      if (url.endsWith('/task')) {
        return {
          ok: true,
          status: 200,
          json: async () => ({ ...nextBridgeTask }),
        };
      }
      if (url.endsWith('/handoff-ready')) {
        return {
          ok: true,
          status: 200,
          json: async () => ({ status: 'handoff_acknowledged' }),
        };
      }
      if (url.endsWith('/cycle-refreshed')) {
        const body = JSON.parse(opts.body);
        cycleRefreshedAcks.push(body);
        if (nextBridgeTask) nextBridgeTask.refresh_once = false;
        return {
          ok: true,
          status: 200,
          json: async () => ({ status: 'cycle_refresh_acknowledged' }),
        };
      }
      throw new Error('Unexpected fetch URL: ' + url);
    },
  };

  vm.createContext(sandbox);
  vm.runInContext(swCode, sandbox);

  // 1. Test openFreshIncognitoChecker for Service Number (2...) Cycle #1
  await sandbox.openFreshIncognitoChecker({
    task_id: 'run:0:1:wallet:1:2011111111',
    row_number: 1,
    record_type: 'wallet',
    search_number: '2011111111',
    cycle_mode: 'SERVICE_2',
    service_cycle_id: 1,
    refresh_once: true,
  });

  // Verify old Incognito tabs (200, 201) and old Incognito windows (20, 21) were closed first!
  assert(removedTabIds.includes(200) && removedTabIds.includes(201), 'All old Incognito tabs must be closed first');
  assert(removedWindowIds.includes(20) && removedWindowIds.includes(21), 'All old Incognito windows must be closed first');

  // Verify strictly 1 Incognito tab remains open in the new Incognito window
  const activeIncognitoTabs = currentTabs.filter((t) => t.incognito);
  assert.strictEqual(activeIncognitoTabs.length, 1, 'Strictly 1 fresh Incognito tab must remain open');
  const activeTabId = activeIncognitoTabs[0].id;

  // 2. Test first poll_task in Cycle #1 -> must set do_single_refresh = true ONCE
  nextBridgeTask = {
    status: 'check',
    task_id: 'run:0:1:wallet:1:2011111111',
    row_number: 1,
    record_type: 'wallet',
    search_number: '2011111111',
    cycle_mode: 'SERVICE_2',
    service_cycle_id: 1,
    refresh_once: true,
    sequence: 1,
    total: 5,
  };

  const poll1 = await sandbox.handleMessage(
    { type: 'poll_task' },
    { tab: { id: activeTabId, incognito: true, url: activeIncognitoTabs[0].url } }
  );
  assert.strictEqual(poll1.do_single_refresh, true, 'First poll of Cycle #1 must trigger do_single_refresh=true');
  assert.strictEqual(cycleRefreshedAcks.length, 1, 'Must acknowledge /cycle-refreshed once for Cycle #1');

  // 3. Test second poll_task in Cycle #1 (after the 1 refresh) -> must have do_single_refresh = false
  const poll1After = await sandbox.handleMessage(
    { type: 'poll_task' },
    { tab: { id: activeTabId, incognito: true, url: activeIncognitoTabs[0].url } }
  );
  assert.strictEqual(poll1After.do_single_refresh, false, 'Second poll in Cycle #1 must NOT refresh again');

  // 4. Test consecutive Service Number 2022222222 in Cycle #1 -> reuses same tab, NO reload
  nextBridgeTask = {
    status: 'check',
    task_id: 'run:1:1:wallet:2:2022222222',
    row_number: 2,
    record_type: 'wallet',
    search_number: '2022222222',
    cycle_mode: 'SERVICE_2',
    service_cycle_id: 1,
    refresh_once: false,
    sequence: 2,
    total: 5,
  };
  const poll2 = await sandbox.handleMessage(
    { type: 'poll_task' },
    { tab: { id: activeTabId, incognito: true, url: activeIncognitoTabs[0].url } }
  );
  assert.strictEqual(poll2.do_single_refresh, false, 'Consecutive Service 2... in Cycle #1 must NOT refresh');

  const reloadsBeforeNav2 = reloadedTabIds.length;
  await sandbox.handleMessage(
    {
      type: 'navigate_task',
      task: poll2,
      previous_website_amount: '100.00',
      reload: false,
    },
    { tab: { id: activeTabId, incognito: true, url: activeIncognitoTabs[0].url } }
  );
  assert.strictEqual(reloadedTabIds.length, reloadsBeforeNav2, 'navigate_task with reload=false must NOT reload the tab');
  assert.strictEqual(
    activeIncognitoTabs[0].url,
    'https://app.sa.zain.com/ar/quickpay?account=2022222222',
    'Must update the exact same Incognito tab to the next 2... service number URL'
  );

  console.log('✅ Extension service-worker cycle & Incognito tab test passed 100%!');
}

runExtensionCycleTest().catch((err) => {
  console.error('❌ Test failed:', err);
  process.exit(1);
});
