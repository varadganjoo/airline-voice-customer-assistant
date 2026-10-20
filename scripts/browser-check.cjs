// Optional browser smoke: set PLAYWRIGHT_MODULE to an existing Playwright installation.
// Uses a fake microphone, not the operator's microphone. Gemini usage is billable.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const fs = require('node:fs/promises');
(async () => {
  await fs.mkdir('artifacts', { recursive: true });
  const browser = await chromium.launch({ channel: 'chrome', headless: true, args: ['--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream', '--autoplay-policy=no-user-gesture-required'] });
  try {
    const context = await browser.newContext({ permissions: ['microphone'], viewport: { width: 1440, height: 1050 } });
    const page = await context.newPage();
    await page.addInitScript(() => {
      window.__testSockets = [];
      window.__completedTurns = 0;
      const Original = window.WebSocket;
      window.WebSocket = class extends Original { constructor(...args) {
        super(...args); window.__testSockets.push(this);
        this.addEventListener('message', async event => {
          const message = JSON.parse(typeof event.data === 'string' ? event.data : await event.data.text());
          if (message.serverContent?.turnComplete) window.__completedTurns++;
        });
      } };
      window.__spokenHandoff = '';
      const speak = speechSynthesis.speak.bind(speechSynthesis);
      speechSynthesis.speak = utterance => { window.__spokenHandoff = utterance.text; speak(utterance); };
    });
    const errors = []; page.on('pageerror', e => errors.push(e.message));
    const toolCalls = [];
    page.on('response', async response => {
      if (response.url().endsWith('/api/tool')) {
        const data = await response.json();
        if (data.result && !data.result.error) toolCalls.push(response.request().postDataJSON().name);
      }
    });
    await page.goto('http://localhost:3000');
    if (/gemini|google|sqlite/i.test(await page.locator('body').innerText())) throw new Error('Implementation branding is visible');
    await page.screenshot({ path: 'artifacts/desktop.png', fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.screenshot({ path: 'artifacts/mobile.png', fullPage: true });
    if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)) throw new Error('Mobile horizontal overflow');
    await page.setViewportSize({ width: 1440, height: 1050 });
    await page.getByRole('button', { name: 'Call Skyway', exact: true }).click();
    await page.waitForFunction(() => document.getElementById('status').textContent .startsWith('On call'), { timeout: 45000 });
    await page.getByRole('button', { name: 'Mute', exact: true }).click();
    await page.locator('#transcript p.assistant').first().waitFor({ timeout: 30000 });
    await page.waitForFunction(() => window.__completedTurns >= 1);
    const reference = await page.locator('#bookings .booking').first().locator('code').textContent();
    async function say(message) { await page.locator('#text').fill(message); await page.getByRole('button', { name: 'Send', exact: true }).click(); }
    async function sayAndWait(message) {
      const before = await page.evaluate(() => window.__completedTurns);
      await say(message);
      await page.waitForFunction(count => window.__completedTurns > count, before, { timeout: 45000 });
    }
    await sayAndWait(`I'd like to check my booking. The confirmation number is ${reference.split('').join(' ')}.`);
    await sayAndWait('Yes, that is right.');
    await sayAndWait('My name is Micheal Jackson, M I C H E A L, J A C K S O N.');
    if (toolCalls.includes('request_verification')) throw new Error('Lookup started before the spelled name was confirmed');
    if (!/Micheal|Mike.*India.*Charlie.*Hotel.*Echo/is.test(await page.locator('#transcript p.assistant').last().innerText())) throw new Error('Passenger name was not read back');
    await sayAndWait('Sorry, correction: it is Michael, M I C H A E L.');
    if (!/Alpha.*Echo/is.test(await page.locator('#transcript p.assistant').last().innerText())) throw new Error('Corrected name was not read back');
    await say('Yes, that is correct.');
    await page.locator('#inbox .booking').first().waitFor({ timeout: 45000 });
    const code = await page.locator('#inbox .booking').first().locator('code').textContent();
    await say(`The code is ${code.split('').join(' ')}.`);
    await page.waitForFunction(() => /verify booking complete/.test(document.getElementById('tool-status').textContent), { timeout: 45000 });
    console.log('PASS: browser audio connection and model-driven identity verification');
    console.log('PASS: phonetic readback, correction, and explicit confirmation before verification');
    if (process.env.TEST_RECONNECT === '1') {
      await page.evaluate(() => window.__testSockets.at(-1).close());
      await page.waitForFunction(() => window.__testSockets.length >= 2 && document.getElementById('status').textContent .startsWith('On call'), { timeout: 45000 });
      console.log('PASS: connection recovery after forced WebSocket closure');
    }
    await sayAndWait('Please cancel the whole trip and refund me.');
    if (toolCalls.includes('confirm_action')) throw new Error('Action committed before the caller agreed');
    await say('Yes, please go ahead.');
    await page.waitForFunction(() => /confirm action complete/.test(document.getElementById('tool-status').textContent), { timeout: 45000 });
    console.log('PASS: model proposed and committed a simulated refund after a spoken yes');
    await page.screenshot({ path: 'artifacts/verified-call.png', fullPage: true });
    await say('Please transfer me to a customer service representative now.');
    await page.waitForFunction(() => window.__spokenHandoff === 'The call is being forwarded to a customer service representative now.', { timeout: 45000 });
    console.log('PASS: exact spoken handoff, call ended');
    if (errors.length) throw new Error(errors.join('; '));
    console.log('PASS: desktop/mobile layout, start/mute/handoff controls, no page errors');
  } catch (e) {
    console.error('FAIL:', e.message);
    const pages = browser.contexts().flatMap(c => c.pages());
    if (pages[0]) {
      await pages[0].screenshot({ path: 'artifacts/browser-failure.png', fullPage: true });
      console.error((await pages[0].locator('#error').textContent()) || (await pages[0].locator('#tool-status').textContent()));
      console.error(await pages[0].locator('#transcript').textContent());
    }
    process.exitCode = 1;
  } finally { await browser.close(); }
})();
