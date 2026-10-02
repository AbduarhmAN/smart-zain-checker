/**
 * REST API Client for Smart Zain Checker
 */
const API = {
  async get(endpoint) {
    const res = await fetch(endpoint);
    if (!res.ok) throw new Error(`HTTP Error ${res.status}`);
    return await res.json();
  },

  async post(endpoint, data = {}) {
    const res = await fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    });
    if (!res.ok) {
      const errData = await res.json().catch(() => ({}));
      throw new Error(errData.message || `HTTP Error ${res.status}`);
    }
    return await res.json();
  },

  getSessionInfo() {
    return this.get('/api/session-info');
  },

  getLiveStatus() {
    return this.get('/api/live-status');
  },

  startSession(payload) {
    return this.post('/api/start-session', payload);
  },

  pauseSession() {
    return this.post('/api/pause-session');
  },

  resumeSession() {
    return this.post('/api/resume-session');
  },

  cancelSession() {
    return this.post('/api/cancel-session');
  },

  inspectSheetColumns(workbook, sheetIndex = 0) {
    return this.post('/api/inspect-sheet-columns', { workbook, sheet_index: sheetIndex });
  },

  getQueue() {
    return this.get('/api/queue');
  },

  addQueueJob(payload) {
    return this.post('/api/queue/add', payload);
  },

  removeQueueJob(jobId) {
    return this.post('/api/queue/remove', { job_id: jobId });
  },

  startQueue() {
    return this.post('/api/queue/start');
  },

  testTelegramPing() {
    return this.post('/api/telegram/test-ping');
  },

  getTelegramStatus() {
    return this.get('/api/telegram/status');
  },

  getWorkbookSheets(workbook) {
    return this.get(`/api/workbook-sheets?workbook=${encodeURIComponent(workbook)}`);
  },

  uploadWorkbook(filename, base64Data) {
    return this.post('/api/upload-workbook', { filename, data_base64: base64Data });
  },

  checkSheetStatus(workbook, sheetIndex = 0) {
    return this.get(`/api/check-sheet-status?workbook=${encodeURIComponent(workbook)}&sheet_index=${sheetIndex}`);
  },

  restartQueueJob(payload) {
    return this.post('/api/queue/restart', payload);
  }
};

