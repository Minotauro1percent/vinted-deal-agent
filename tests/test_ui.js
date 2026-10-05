// End-to-End-Test der Web-Oberfläche (docs/index.html) mit simulierter GitHub-API.
// Start:  NODE_PATH=$(npm root -g) node tests/test_ui.js [ausgabeordner]
const {chromium} = require('playwright');
const fs = require('fs'), path = require('path');
const ROOT = path.join(__dirname, '..');
const OUT = process.argv[2] || path.join(ROOT, 'tests', 'out');
fs.mkdirSync(OUT, {recursive: true});

const files = {};
for (const f of ['watchlist.json', 'settings.json', 'state.json', 'last_run.txt']) {
  const p = path.join(ROOT, f);
  if (fs.existsSync(p)) files[f] = {text: fs.readFileSync(p, 'utf8'), sha: 'sha0'};
}
let shaN = 1, puts = [], conflictOnce = true;
const J = name => JSON.parse(files[name].text);

let ok = true;
const check = (c, label) => { console.log((c ? 'PASS ' : 'FAIL ') + label); ok = ok && !!c; };

(async () => {
  const browser = await chromium.launch({executablePath: '/opt/pw-browsers/chromium-1194/chrome-linux/chrome'}).catch(() => chromium.launch());
  const ctx = await browser.newContext({viewport: {width: 1100, height: 900}, locale: 'de-AT', timezoneId: 'Europe/Vienna'});
  const page = await ctx.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('console', m => { if (m.type() === 'error' && !m.text().startsWith('Failed to load resource')) errors.push(m.text()); });  // 401/409 sind im Test gewollt

  await page.route('https://api.github.com/**', async route => {
    const req = route.request(), url = new URL(req.url());
    const m = /\/repos\/([^/]+\/[^/]+)\/contents\/(.+)$/.exec(url.pathname);
    if (req.headers()['authorization'] !== 'Bearer github_pat_TEST') return route.fulfill({status: 401, json: {message: 'Bad credentials'}});
    if (!m || m[1] !== 'Minotauro1percent/vinted-deal-agent') return route.fulfill({status: 404, json: {message: 'Not Found'}});
    const name = decodeURIComponent(m[2]);
    if (req.method() === 'GET') {
      const f = files[name];
      if (!f) return route.fulfill({status: 404, json: {message: 'Not Found'}});
      const b64 = Buffer.from(f.text, 'utf8').toString('base64').replace(/(.{60})/g, '$1\n');
      return route.fulfill({json: {sha: f.sha, content: b64, encoding: 'base64'}});
    }
    if (req.method() === 'PUT') {
      const body = JSON.parse(req.postData());
      const f = files[name];
      if (conflictOnce && name === 'settings.json') { conflictOnce = false; f.sha = 'sha-agent'; return route.fulfill({status: 409, json: {message: 'conflict'}}); }
      if (f && body.sha !== f.sha) return route.fulfill({status: 409, json: {message: 'sha mismatch'}});
      files[name] = {text: Buffer.from(body.content, 'base64').toString('utf8'), sha: 'sha' + shaN++};
      puts.push({name, message: body.message});
      return route.fulfill({json: {content: {sha: files[name].sha}}});
    }
    route.fulfill({status: 405, json: {}});
  });

  await page.goto('file://' + path.join(ROOT, 'docs', 'index.html'));
  // 1) Einrichtung
  await page.waitForSelector('#tokIn');
  check(await page.inputValue('#repoIn') === 'Minotauro1percent/vinted-deal-agent', 'Repo vorausgefüllt');
  await page.fill('#tokIn', 'falsch'); await page.click('#conBtn');
  await page.waitForSelector('.err');
  check((await page.textContent('.err')).includes('Token ungültig'), 'Falscher Token → klare Fehlermeldung');
  await page.fill('#tokIn', 'github_pat_TEST'); await page.click('#conBtn');
  await page.waitForSelector('.watch');
  const n0 = J('watchlist.json').length;
  check(await page.locator('.watch').count() === n0, `Liste zeigt alle ${n0} Suchen`);
  check(await page.evaluate(() => localStorage.getItem('vda_token')) === 'github_pat_TEST', 'Token nur lokal im Browser gespeichert');
  await page.screenshot({path: path.join(OUT, '1-uebersicht.png'), fullPage: true});

  // 2) Neue Technik-Suche mit festem Termin
  await page.click('#addBtn');
  await page.click('#eCat button[data-v="technik"]');
  check(await page.isVisible('#secTech') && !(await page.isVisible('#secClothes')), 'Technik: Speicher/SIM statt Größen');
  check(await page.isChecked('#eSim'), 'Technik-Vorlage: SIM-frei vorausgewählt');
  check((await page.textContent('#eExclude')).includes('icloud'), 'Technik-Vorlage: Ausschlüsse (icloud, Hülle …)');
  await page.fill('#eName', 'Xiaomi 17');
  await page.fill('#eQueries input', 'xiaomi 17'); await page.keyboard.press('Enter');
  await page.fill('#eQueries input', 'xiaomi 17 256gb'); await page.keyboard.press('Enter');
  await page.fill('#eBrands input', 'Xiaomi'); await page.keyboard.press('Enter');
  await page.fill('#eBrands input', 'Redmi');
  await page.click('#eBrands .tag button');             // „Xiaomi“ entfernen, während „Redmi“ noch getippt ist
  check((await page.textContent('#eBrands')).includes('Redmi') && !(await page.textContent('#eBrands')).includes('Xiaomi'), 'Tag entfernen behält getippten Text');
  await page.click('#eBrands .tag button');
  await page.fill('#eBrands input', 'Xiaomi'); await page.keyboard.press('Enter');
  await page.click('#eStorage .chip[data-v="256 GB"]');
  await page.click('#eStorage .chip[data-v="512 GB"]');
  await page.click('#eSave');
  check((await page.textContent('#eErr')).includes('Maximalpreis'), 'Ohne Max-Preis: Hinweis statt Speichern');
  await page.fill('#eMax', '600');
  await page.click('#eMode button[data-v="times"]');
  await page.click('#ePresets [data-p="mo8"]');
  check((await page.textContent('#eNext')).includes('Mo 08:00'), 'Vorschau „Mo 08:00“ + nächster Termin');
  await page.screenshot({path: path.join(OUT, '2-editor-technik.png')});
  await page.click('#eSave');
  await page.waitForFunction(() => !document.querySelector('#editor').open);
  let wl = J('watchlist.json'); const x = wl.find(w => w.id === 'xiaomi-17');
  check(x && x.category === 'technik' && x.maxPrice === 600 && x.simFree === true, 'Neue Suche gespeichert (Kategorie, Preis, SIM)');
  check(x && JSON.stringify(x.storage) === '["256 GB","512 GB"]' && JSON.stringify(x.queries) === '["xiaomi 17","xiaomi 17 256gb"]', 'Speicher + Suchbegriffe gespeichert');
  check(x && JSON.stringify(x.schedule) === '{"mode":"times","days":[1],"times":["08:00"]}', 'Zeitplan „jeden Montag 8:00“ gespeichert');
  check(await page.locator('.watch').count() === n0 + 1, 'Liste aktualisiert');

  // 3) Bestehende Kleidungs-Suche bearbeiten: Größen, Farben, eigenes Intervall, zwei Uhrzeiten
  const rl = page.locator('.watch', {hasText: 'Ralph Lauren'}).first();
  await rl.locator('[data-act="edit"]').click();
  check(await page.isVisible('#secClothes') && !(await page.isVisible('#secTech')), 'Kleidung: Größen + Farben sichtbar');
  for (const s of ['M', 'L']) await page.click(`#eSizes .chip[data-v="${s}"]`);
  await page.fill('#eSizeAdd', '40'); await page.click('#eSizeAddBtn');
  for (const c of ['Blau', 'Grau', 'Beige']) await page.click(`#eColors .chip[data-v="${c}"]`);
  await page.click('#eMode button[data-v="interval"]');
  await page.selectOption('#eEvery', '30');
  await page.screenshot({path: path.join(OUT, '3-editor-kleidung.png')});
  await page.click('#eSave'); await page.waitForFunction(() => !document.querySelector('#editor').open);
  wl = J('watchlist.json'); const r = wl.find(w => /ralph/i.test(w.name));
  check(JSON.stringify(r.sizes) === '["M","L","40"]', 'Größen gespeichert: ' + JSON.stringify(r.sizes));
  check(JSON.stringify(r.colors) === '["Blau","Grau","Beige"]', 'Farben gespeichert');
  check(JSON.stringify(r.schedule) === '{"mode":"interval","everyMin":30}', 'Intervall 30 Min. gespeichert');
  check(r.require && r.require.includes('zopf') && r.resaleMin === 35, 'Unveränderte Felder bleiben erhalten');
  check(wl.indexOf(r) === 0, 'Reihenfolge bleibt (wichtig für /liste-Nummern)');

  // 4) Duplizieren → Feste Zeiten werktags + 2 Uhrzeiten
  await page.locator('.watch', {hasText: 'Barbour'}).first().locator('[data-act="dup"]').click();
  check((await page.inputValue('#eName')).includes('(Kopie)'), 'Duplizieren öffnet Kopie');
  await page.fill('#eName', 'Barbour Beaufort');
  await page.click('#eMode button[data-v="times"]');
  await page.click('#ePresets [data-p="wk730"]');
  await page.fill('#eTimeIn', '18:15'); await page.click('#eTimeAdd');
  await page.click('#eSave'); await page.waitForFunction(() => !document.querySelector('#editor').open);
  wl = J('watchlist.json'); const bb = wl.find(w => w.name === 'Barbour Beaufort');
  check(bb && bb.id === 'barbour-beaufort' && JSON.stringify(bb.schedule) === '{"mode":"times","days":[1,2,3,4,5],"times":["07:30","18:15"]}', 'Kopie mit eigenem Zeitplan Mo–Fr 07:30 + 18:15');
  check(wl.filter(w => /barbour/i.test(w.name)).length === 2, 'Original bleibt erhalten');

  // 5) Aus-/Einschalten, Jetzt suchen, Löschen
  await page.locator('.watch', {hasText: 'Moon Boot'}).locator('.sw').click();
  await page.waitForFunction(() => document.querySelector('.watch.off'));
  check(J('watchlist.json').find(w => w.id === 'moon-boot').active === false, 'Suche ausgeschaltet');
  await page.locator('.watch', {hasText: 'Salomon'}).locator('[data-act="now"]').click();
  await page.waitForSelector('.badge.warn');
  check(J('settings.json').force['salomon-xt-6'] > 0, '„Jetzt suchen“ setzt settings.force (trotz Konflikt beim 1. Versuch)');
  const del = page.locator('.watch', {hasText: 'Longchamp'}).locator('[data-act="del"]');
  await del.click();
  check(J('watchlist.json').some(w => w.id === 'longchamp-le-pliage'), 'Löschen braucht Bestätigung');
  await del.click();
  await page.waitForFunction(() => ![...document.querySelectorAll('.watch .name')].some(e => e.textContent.includes('Longchamp')));
  check(!J('watchlist.json').some(w => w.id === 'longchamp-le-pliage'), 'Suche gelöscht');

  // 6) Agent pausieren + allgemeine Einstellungen
  await page.locator('label.sw:has(#enSw)').click();
  await page.waitForSelector('.pill.off');
  check(J('settings.json').enabled === false, 'Agent pausiert (settings.enabled)');
  await page.selectOption('#gInt', '120'); await page.fill('#gMax', '10'); await page.click('#gSave');
  for (let i = 0; i < 50 && J('settings.json').default_interval_min !== 120; i++) await page.waitForTimeout(100);
  const st = J('settings.json');
  check(st.default_interval_min === 120 && st.max_per_run === 10 && st.force['salomon-xt-6'] > 0, 'Allgemein gespeichert, nichts überschrieben');
  check(puts.every(p => p.message.endsWith('[Web]')), 'Commit-Nachrichten verständlich: ' + puts.map(p => p.message).slice(0, 3).join(' | '));
  await page.screenshot({path: path.join(OUT, '4-nach-aenderungen.png'), fullPage: true});

  // 7) Handy-Ansicht
  await page.setViewportSize({width: 390, height: 844});
  await page.screenshot({path: path.join(OUT, '5-handy.png'), fullPage: false});
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
  check(!overflow, 'Handy: kein horizontales Scrollen');
  await page.locator('.watch', {hasText: 'Xiaomi'}).locator('[data-act="edit"]').click();
  await page.screenshot({path: path.join(OUT, '6-handy-editor.png')});
  const ov2 = await page.evaluate(() => document.querySelector('.sheet').scrollWidth > window.innerWidth);
  check(!ov2, 'Handy-Editor passt in die Breite');
  await page.click('#eCancel');

  check(errors.length === 0, 'Keine JS-Fehler' + (errors.length ? ': ' + errors.join(' | ') : ''));
  fs.writeFileSync(path.join(OUT, 'watchlist.after.json'), files['watchlist.json'].text);
  fs.writeFileSync(path.join(OUT, 'settings.after.json'), files['settings.json'].text);
  await browser.close();
  console.log(ok ? '\nALLE UI-TESTS BESTANDEN' : '\nES GIBT FEHLER');
  process.exit(ok ? 0 : 1);
})().catch(e => { console.error(e); process.exit(1); });
