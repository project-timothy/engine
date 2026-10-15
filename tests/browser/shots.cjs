// Each rendered page at a phone's width, light and dark (CI keeps the photos).
// node tests/browser/shots.cjs <pages dir> <shots dir>; PW = playwright-core, CHROME = the binary.
const { chromium } = require(process.env.PW);
const fs = require('fs');
const path = require('path');
(async () => {
  const [pages, shots] = process.argv.slice(2);
  const browser = await chromium.launch({ executablePath: process.env.CHROME });
  const page = await (await browser.newContext({ viewport: { width: 390, height: 844 } })).newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  for (const file of fs.readdirSync(pages).filter(f => f.endsWith('.html')).sort()) {
    await page.goto('file://' + path.resolve(pages, file));
    for (const scheme of ['light', 'dark']) {
      await page.emulateMedia({ colorScheme: scheme });
      await page.screenshot({ path: `${shots}/${file.replace('.html', '')}-${scheme}.png`, fullPage: true });
    }
    const wide = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
    if (wide) errors.push(`${file} scrolls sideways at 390px`);
  }
  await browser.close();
  if (errors.length) { console.log(errors.join('\n')); process.exit(1); }
  console.log('photographed', fs.readdirSync(shots).length);
})().catch(e => { console.log('ERROR:', e.message); process.exit(1); });
