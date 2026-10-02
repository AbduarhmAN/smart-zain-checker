import { fileURLToPath, pathToFileURL } from 'node:url';
import { dirname, join } from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';

const directory = dirname(fileURLToPath(import.meta.url));
const chrome = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const run = promisify(execFile);
const source = pathToFileURL(join(directory, 'designs.html')).href;

for (const [id, file, width, height] of [
  ['offer', 'offer.png', 1080, 1350],
  ['positive', 'positive.png', 1080, 1350],
  ['negative', 'negative.png', 1080, 1350],
  ['multiple-debts', 'multiple-debts.png', 1080, 1350],
  ['sheet-concept', 'sheet-concept.png', 1600, 900],
]) {
  await run(chrome, [
    '--headless=new',
    '--disable-gpu',
    '--hide-scrollbars',
    '--no-first-run',
    '--no-default-browser-check',
    '--force-device-scale-factor=1',
    '--virtual-time-budget=1000',
    `--window-size=${width},${height}`,
    `--screenshot=${join(directory, file)}`,
    `--user-data-dir=${join(directory, '.chrome-render-profile')}`,
    `${source}?art=${id}`,
  ]);
}
