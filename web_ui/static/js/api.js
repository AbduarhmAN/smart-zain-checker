/**
 * REST API Client for Smart Zain Checker
 */
const API = {
  async request(endpoint, options = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), options.method === 'POST' ? 60000 : 12000);
    try {
      const response = await fetch(endpoint, {...options, signal:controller.signal});
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.message || 'تعذر تنفيذ الطلب ('+response.status+')');
      return data;
    } catch(error) {
      if (error.name === 'AbortError') throw new Error('لم يصل رد البرنامج في المهلة المحددة');
      throw error;
    } finally {clearTimeout(timer);}
  },
  get(endpoint) {return this.request(endpoint);},
  post(endpoint, data = {}) {return this.request(endpoint, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});},

  getSessionInfo() {
    return this.get('/api/session-info');
  },

  getLiveStatus() {
    return this.get('/api/live-status');
  },

  validateSheet(payload) {return this.post('/api/validate-sheet',payload);},

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

  inspectSheetColumns(workbook, sheetIndex = 0, hasHeaders = true) {
    return this.post('/api/inspect-sheet-columns', { workbook, sheet_index: sheetIndex, has_headers:hasHeaders });
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
  },

  restartAllQueueJobs(autoStart = true) {
    return this.post('/api/queue/restart-all', { auto_start: autoStart });
  },

  repairErrors(row = null, workbook = null, sheetIndex = 0) {
    const payload = {};
    if (row != null) payload.row = Number(row);
    if (workbook) payload.workbook = workbook;
    if (sheetIndex !== undefined) payload.sheet_index = sheetIndex;
    return this.post('/api/repair-errors', payload);
  },

  detectResultsSheet(workbook, sheetIndex = 0) {
    return this.get(`/api/detect-results-sheet?workbook=${encodeURIComponent(workbook)}&sheet_index=${sheetIndex}`);
  },

  startRepairSession(workbook, sheetIndex = 0) {
    return this.post('/api/start-repair-session', { workbook, sheet_index: sheetIndex });
  }
};

