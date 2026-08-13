import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const html = fs.readFileSync(new URL('./index.html', import.meta.url), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>\s*<\/body>/i)?.[1];
assert.ok(script, 'Nie znaleziono głównego skryptu panelu');

const start = script.indexOf('const SUPPORTED_LANGUAGES');
const end = script.indexOf('function applyTranslations');
assert.ok(start >= 0 && end > start, 'Nie znaleziono centralnego bloku tłumaczeń');

const context = vm.createContext({});
vm.runInContext(`${script.slice(start, end)}
globalThis.I18N_TEST = {
  supported: SUPPORTED_LANGUAGES,
  translations: TRANSLATIONS,
  polish: PL_TRANSLATIONS,
  normalize: normalizeLanguage,
  translate(language, key) { CURRENT_LANGUAGE = normalizeLanguage(language); return t(key); }
};`, context);

const i18n = context.I18N_TEST;
const polishKeys = Object.keys(i18n.polish).sort();
assert.deepEqual([...i18n.supported], ['pl', 'de', 'en', 'es', 'it']);

for (const language of i18n.supported) {
  const missing = polishKeys.filter(key => i18n.translations[language][key] == null);
  assert.deepEqual(missing, [], `${language}: brakujące klucze: ${missing.join(', ')}`);
  assert.equal(i18n.translate(language, 'orders.downloadPdf').length > 0, true);
}

assert.equal(i18n.normalize(null), 'pl');
assert.equal(i18n.normalize(''), 'pl');
assert.equal(i18n.normalize('xx'), 'pl');
assert.equal(i18n.normalize(' DE '), 'de');
assert.equal(i18n.translate('xx', 'orders.downloadPdf'), 'Pobierz PDF');

const profileLoad = script.indexOf('profile = await fetchClientProfile()');
const appReveal = script.indexOf('showApp(session.user?.email', profileLoad);
assert.ok(profileLoad >= 0 && appReveal > profileLoad, 'Panel musi być pokazany dopiero po pobraniu języka profilu');
assert.ok(script.includes('/api/client/orders/${Number(order.id)}/pdf'), 'Brak chronionego pobierania PDF zamówienia');
assert.ok(!script.includes('/api/client/orders/${Number(order.id)}/pdf?language='), 'Frontend nie może narzucać języka PDF');

console.log(`i18n OK: ${i18n.supported.length} języków, ${polishKeys.length} kluczy na język`);
