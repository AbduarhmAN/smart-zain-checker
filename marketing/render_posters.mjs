import { fileURLToPath, pathToFileURL } from 'node:url';
import { dirname, join } from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';

const directory = dirname(fileURLToPath(import.meta.url));
const chrome = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const run = promisify(execFile);

async function capture(url, file, width, height) {
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
    url,
  ]);
}

for (const [id, file] of [
  ['poster-time', 'poster-time.png'],
  ['poster-differences', 'poster-differences.png'],
  ['poster-price', 'poster-price.png'],
]) {
  await capture(`${pathToFileURL(join(directory, 'posters.html')).href}?poster=${id}`, file, 1080, 1350);
}
await capture(pathToFileURL(join(directory, 'logo.svg')).href, 'logo.png', 800, 800);
await capture(pathToFileURL(join(directory, 'page-cover.svg')).href, 'page-cover.png', 1640, 624);
