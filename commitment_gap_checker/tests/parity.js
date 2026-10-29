// Checks the JavaScript port against the Python tool's saved output. Run: node tests/parity.js
const fs = require('fs'), path = require('path'), assert = require('assert');
const G = require('../gap_checker.js'); const root = path.join(__dirname, '..');
const read = f => fs.readFileSync(path.join(root, f), 'utf8');
let n = 0; const ok = (name, fn) => { fn(); n++; console.log('ok  ' + name); };

ok('onboarding sample matches Python output field by field', () => {
  const js = G.analyse(read('sample_data/onboarding_meeting.txt')), py = JSON.parse(read('expected_outputs/onboarding_meeting.json'));
  assert.deepStrictEqual(js.summary, py.summary);
  assert.strictEqual(js.items.length, py.commitments.length);
  js.items.forEach((it, i) => assert.deepStrictEqual(it, py.commitments[i]));
  assert.strictEqual(js.followup_draft, py.followup_draft);
});
ok('contradictory dates: three rejected, month-end resolved', () => {
  const r = G.analyse(read('sample_data/contradictory_dates.txt'));
  assert.deepStrictEqual(r.items.map(i => i.status), ['bad_date', 'bad_date', 'bad_date', 'complete', 'complete']);
  assert.strictEqual(r.items[3].due_date, '2026-10-31');
});
ok('no-commitment transcript is valid', () => assert.strictEqual(G.analyse(read('sample_data/smalltalk.txt')).summary.total, 0));
for (const f of ['empty.txt', 'whitespace_only.txt', 'garbled.txt', 'chinese_transcript.txt'])
  ok('invalid input rejected: ' + f, () => assert.throws(() => G.analyse(read('sample_data/' + f)), G.InputError));
ok('missing meeting date rejected', () => assert.throws(() => G.analyse("Sam (Internal): I'll send it by Friday."), G.InputError));
ok('date box overrides header', () => assert.strictEqual(G.analyse("Meeting date: 2026-01-01\nSam (Internal): I'll send it tomorrow.", '2026-10-27').items[0].due_date, '2026-10-28'));
ok('date resolution', () => {
  const m = G.UTC_D(2026, 10, 27), d = r => { const x = G.resolveDue(r, m); return x.problem || x.date.toISOString().slice(0, 10); };
  assert.deepStrictEqual(['Thursday', 'Tuesday', 'next Friday', 'tomorrow', 'end of the week', 'in 3 business days', 'Nov 6', '11/06', 'Feb 30', 'Oct 15', 'soon'].map(d),
    ['2026-10-29', '2026-11-03', '2026-11-06', '2026-10-28', '2026-10-30', '2026-10-30', '2026-11-06', '2026-11-06', 'invalid', 'before_meeting', 'imprecise']);
});
console.log(`\n${n} checks passed`);
