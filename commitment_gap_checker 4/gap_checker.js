/* Commitment Gap Checker - browser/Node port of gap_checker.py (same rules, same output fields).
   Offline rule-based extractor only. The optional LLM mode of the Python CLI is NOT in the browser. */
(function (root) {
'use strict';
class InputError extends Error {}
const R = String.raw;
const MON = R`(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)`;
const DAYS = R`(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)`;
const MONTHS = ['jan','feb','mar','apr','may','jun','jul','aug','sep','oct','nov','dec'];
const DAY_NAMES = ['monday','tuesday','wednesday','thursday','friday','saturday','sunday'];
const ABBR = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
const NUMW = {one:1,two:2,three:3,four:4,five:5,six:6,seven:7,ten:10};
const DUE = [
  ['iso', R`\b\d{4}-\d{1,2}-\d{1,2}\b`],
  ['mdy', R`\b${MON}\.?\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?\b`],
  ['dmy', R`\b\d{1,2}(?:st|nd|rd|th)?\s+(?:of\s+)?${MON}\b(?:,?\s+\d{4})?`],
  ['slash', R`\b(?!24/7\b)\d{1,2}/\d{1,2}(?:/\d{2,4})?\b`],
  ['end_of', R`\bend of (?:the |this )?(?:day|week|month)\b|\b(?:eod|eow|eom)\b`],
  ['relday', R`\b(?:today|tomorrow|tonight)\b`],
  ['in_n', R`\bin\s+(?:\d+|one|two|three|four|five|six|seven|ten)\s+(?:business\s+)?(?:days?|weeks?)\b`],
  ['weekday', R`\b(?:(?:next|this)\s+)?${DAYS}\b`],
  ['vague', R`\b(?:asap|as soon as (?:possible|we can)|soon|shortly|sometime|at some point|later|whenever|when (?:possible|we can)|in the near future|next week|next month|this week|next quarter)\b`],
].map(([k, p]) => [k, new RegExp(p, 'i')]);
const HEDGE = /\b(maybe|might|perhaps|possibly|probably|hopefully|i guess|sort of|kind of|try to|try and|see if|look into|looking into|take a look|have a look|think about|explore|at some point)\b/i;
const HARD = /integrat|\bsync|automat|migrat|\bapi\b|\bcrm\b|real-time|reconcil|dashboard|webhook|pipeline|rebuild|redesign|across (?:all|every|each)/i;
const GENERIC = new Set(['we','us','our team','the team','team','someone','somebody','anyone','anybody','everyone','nobody','they','you','all']);
const NOT_NAMES = new Set(('that this it there what which who they he she we you i then also yes no and but so well okay ok thanks right sure great today tomorrow everything everyone nothing something someone january february march april may june july august september october november december monday tuesday wednesday thursday friday saturday sunday').split(' '));
const LABEL = {MISSING_OWNER:'missing owner',MISSING_DATE:'missing date',VAGUE_ACTION:'vague wording',BAD_DATE:'date problem',UNCLAIMED_HARD:'unclaimed large item'};
const FIX = {MISSING_OWNER:'Name one person who owns this.',MISSING_DATE:"Agree a specific calendar due date (not 'soon' / 'next week').",VAGUE_ACTION:"Say what 'done' looks like (a deliverable, not 'look into' / 'try').",BAD_DATE:'Re-confirm the date with the people involved.',UNCLAIMED_HARD:'Large item with no owner: assign a lead, or split it into smaller pieces, before the meeting ends.'};
const PRIO = ['BAD_DATE','UNCLAIMED_HARD','MISSING_OWNER','MISSING_DATE','VAGUE_ACTION'];
const STATUS = {BAD_DATE:'bad_date',UNCLAIMED_HARD:'unclaimed_hard',MISSING_OWNER:'missing_owner',MISSING_DATE:'missing_date',VAGUE_ACTION:'vague'};
const COMMON = new Set(('the to a an and or of in on for with that this is are was be will i we you it at by from as can have has not but if so do does what when who how they he she our your my please yes no thanks send share need should would could about up out all').split(' '));

// ---- dates (UTC, so the result never depends on the viewer's time zone) ----
const D = (y, m, d) => { const t = new Date(Date.UTC(y, m - 1, d));
  if (t.getUTCFullYear() !== y || t.getUTCMonth() !== m - 1 || t.getUTCDate() !== d) throw new RangeError('bad date'); return t; };
const add = (t, n) => new Date(t.getTime() + n * 864e5);
const iso = t => t.toISOString().slice(0, 10);
const wd = t => (t.getUTCDay() + 6) % 7;
const days = (a, b) => Math.round((a - b) / 864e5);
function findDue(s) { for (const [k, rx] of DUE) { const m = rx.exec(s); if (m) return [k, m[0].trim()]; } return [null, null]; }
function ymd(y, m, d, meet) {
  if (y != null) return D(y, m, d);
  let c = D(meet.getUTCFullYear(), m, d);
  if (days(meet, c) > 180) c = D(meet.getUTCFullYear() + 1, m, d);
  return c;
}
function specific(kind, raw, meet) {
  const low = raw.toLowerCase(); let m;
  if (kind === 'iso') { const [y, mo, d] = low.split('-').map(Number); return D(y, mo, d); }
  if (kind === 'mdy') { m = new RegExp(`(${MON})\\.?\\s+(\\d{1,2})(?:st|nd|rd|th)?(?:,?\\s+(\\d{4}))?`, 'i').exec(low);
    return ymd(m[3] ? +m[3] : null, MONTHS.indexOf(m[1].slice(0, 3)) + 1, +m[2], meet); }
  if (kind === 'dmy') { m = new RegExp(`(\\d{1,2})(?:st|nd|rd|th)?\\s+(?:of\\s+)?(${MON})\\b(?:,?\\s+(\\d{4}))?`, 'i').exec(low);
    return ymd(m[3] ? +m[3] : null, MONTHS.indexOf(m[2].slice(0, 3)) + 1, +m[1], meet); }
  if (kind === 'slash') { const p = low.split('/').map(Number); // US month/day[/year]
    return ymd(p.length === 3 ? (p[2] < 100 ? p[2] + 2000 : p[2]) : null, p[0], p[1], meet); }
  if (kind === 'end_of') {
    if (low.includes('day') || low === 'eod') return meet;
    if (low.includes('week') || low === 'eow') return add(meet, (4 - wd(meet) + 7) % 7);
    return D(meet.getUTCFullYear(), meet.getUTCMonth() + 1, new Date(Date.UTC(meet.getUTCFullYear(), meet.getUTCMonth() + 1, 0)).getUTCDate()); }
  if (kind === 'relday') return low.includes('tomorrow') ? add(meet, 1) : meet;
  if (kind === 'in_n') { m = /in\s+(\d+|\w+)\s+(business\s+)?(day|week)/.exec(low);
    const n = /^\d+$/.test(m[1]) ? +m[1] : NUMW[m[1]];
    if (m[3] === 'week') return add(meet, 7 * n);
    if (!m[2]) return add(meet, n);
    let t = meet, left = n; while (left > 0) { t = add(t, 1); if (wd(t) < 5) left--; } return t; }
  if (kind === 'weekday') { m = new RegExp(`(next|this)?\\s*(${DAYS})`).exec(low);
    const idx = DAY_NAMES.indexOf(m[2]);
    if (m[1] === 'next') return add(meet, 7 - wd(meet) + idx);
    if (m[1] === 'this') return add(meet, (idx - wd(meet) + 7) % 7);
    return add(meet, ((idx - wd(meet) + 7) % 7) || 7); }
  throw new RangeError(kind);
}
function resolveDue(raw, meet) {
  let [kind, phrase] = findDue(raw); if (!kind) { kind = 'unparsed'; phrase = raw; }
  if (kind === 'vague' || kind === 'unparsed') return {raw: phrase, date: null, problem: 'imprecise', note: `'${phrase}' is not a specific date`};
  let d; try { d = specific(kind, phrase, meet); } catch (e) { if (!(e instanceof RangeError)) throw e;
    return {raw: phrase, date: null, problem: 'invalid', note: `'${phrase}' is not a real calendar date`}; }
  if (d < meet) return {raw: phrase, date: d, problem: 'before_meeting', note: `'${phrase}' resolves to ${iso(d)}, which is before the meeting date ${iso(meet)}`};
  return {raw: phrase, date: d, problem: null, note: `${ABBR[wd(d)]} ${iso(d)}`};
}

// ---- input handling ----
function validateText(text) {
  if (!text || !text.trim()) throw new InputError('the input is empty. Provide a meeting transcript.');
  const ns = [...text].filter(c => !/\s/.test(c));
  const cjk = ns.filter(c => /[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]/.test(c)).length;
  if (cjk / ns.length > 0.3) throw new InputError('the transcript is mostly non-English text. The rule-based extractor supports English only. Translate it first.');
  const letters = ns.filter(c => /\p{L}/u.test(c)).length;
  if (letters / ns.length < 0.6) throw new InputError(`the input does not look like a transcript (only ${Math.floor(letters * 100 / ns.length)}% of characters are letters). It may be garbled or a binary/encoded file.`);
  const tok = text.toLowerCase().match(/[a-z']+/g) || [];
  if (tok.length >= 8 && tok.filter(t => COMMON.has(t)).length / tok.length < 0.08)
    throw new InputError('the input does not look like English sentences (almost no common words found). It may be garbled.');
}
const sideOf = l => !l ? 'unknown' : /client|customer|external/i.test(l) ? 'client' : 'internal';
function parseDateArg(s) {
  const m = /^(\d{4})-(\d{1,2})-(\d{1,2})$/.exec((s || '').trim());
  try { if (!m) throw new RangeError(); return D(+m[1], +m[2], +m[3]); } catch (e) { throw new InputError(`meeting date '${s}' is not a valid YYYY-MM-DD date.`); }
}
const H_DATE = /^\s*meeting date\s*:\s*(\S.*?)\s*$/i, H_TITLE = /^\s*meeting\s*:\s*(\S.*?)\s*$/i;
const SPK = /^\s*([A-Za-z][A-Za-z.'\- ]{0,30}?)\s*(?:\(([^)]{1,30})\))?\s*:\s*(\S.*)$/;
const SILENCE = /^\s*\[(?:silence|no response|no one (?:responds?|answers?)|nobody (?:responds?|answers?))[^\]]*\]\s*$/i;
function parseTranscript(text) {
  const u = [], silence = new Set();
  text.split(/\r?\n/).forEach((raw, i) => {
    if (!raw.trim() || raw.trimStart().startsWith('#') || H_DATE.test(raw) || H_TITLE.test(raw)) return;
    if (SILENCE.test(raw)) { if (u.length) silence.add(u[u.length - 1].line); return; }
    const m = SPK.exec(raw);
    u.push(m ? {line: i + 1, speaker: m[1].trim(), side: sideOf(m[2]), text: m[3].trim()} : {line: i + 1, speaker: null, side: 'unknown', text: raw.trim()});
  });
  return {u, silence};
}
function attendeesOf(u) { const a = new Map();
  for (const x of u) if (x.speaker) { a.set(x.speaker.toLowerCase(), [x.speaker, x.side]);
    const f = x.speaker.toLowerCase().split(' ')[0]; if (!a.has(f)) a.set(f, [x.speaker, x.side]); }
  return a; }
const splitSent = t => t.replace(new RegExp(`\\b(${MON})\\.(\\s+\\d)`, 'gi'), '$1<DOT>$2').trim().split(/(?<=[.!?])\s+/).map(s => s.replace(/<DOT>/g, '.').trim()).filter(Boolean);

// ---- extraction (rules) ----
const ASK_ANY = /\b(?:can|could|will|would|should)\s+(?:someone|anyone|somebody|anybody)\s+.+/i;
const REQUEST = /^\s*(?:([A-Za-z]+),?\s+)?(?:can|could|would|will) you(?: please)?\s+.+/i;
const PLEASE = /^\s*(?:([A-Za-z]+),\s+)?please\s+.+/i;
const SELF = /\b(?:i'll|i will|i'm going to|i am going to|let me)\s+.+/i, SELF_WEAK = /\bi can\s+.+/i;
const TEAM = /\b(?:we'll|we will|we're going to)\s+.+/i, LETS = /\blet's\s+.+/i;
const OBLIG = /\b(we|someone|somebody|anyone|the team|our team)\s+(?:should|need to|needs to|have to|has to|must|ought to|will need to)\s+.+/i;
const WOULD_BE = /\bit would be (?:good|great|nice|helpful|useful) (?:to|if we) .+/i;
const NAME_WILL = /\b([A-Z][a-z]+)\s+(?:will|is going to|'ll)\s+(.+)/;
const cap = n => (n && /^[A-Z][a-z]+$/.test(n)) ? n : null; // vocative names must be capitalised
function classify(s, speaker, side, att) {
  const q = s.endsWith('?'), [dk] = findDue(s); let m;
  if (ASK_ANY.test(s)) return {owner: null, inferred: false, note: "asked of 'someone' - nobody named"};
  if ((m = REQUEST.exec(s)) && (!m[1] || cap(m[1]))) {
    if (cap(m[1])) return {owner: m[1], inferred: false};
    if (!m[1]) { const other = {client: 'internal', internal: 'client'}[side];
      const ppl = [...new Set([...att.values()].filter(v => other && v[1] === other).map(v => v[0]))].sort();
      if (ppl.length === 1) return {owner: ppl[0], inferred: true, note: `owner inferred: ${ppl[0]} is the only ${other} participant - confirm`};
      return {owner: null, inferred: false, note: 'request made to the room without naming who'}; } }
  if (q) return null;
  if ((m = PLEASE.exec(s)) && (!m[1] || cap(m[1]))) return m[1] ? {owner: m[1], inferred: false} : {owner: null, inferred: false, note: 'request without a named owner'};
  const spk = {owner: speaker, inferred: false, note: speaker ? undefined : 'speaker unknown'};
  if (SELF.test(s) || (SELF_WEAK.test(s) && dk)) return spk;
  if (TEAM.test(s) || (LETS.test(s) && dk)) return {owner: 'we', inferred: false, note: "assigned to 'we' (a group), not a person"};
  if ((m = OBLIG.exec(s))) return {owner: m[1].toLowerCase(), inferred: false, note: `stated as an obligation of '${m[1].toLowerCase()}', not a person`};
  if (WOULD_BE.test(s)) return {owner: null, inferred: false, note: 'stated as a wish, no owner named'};
  if ((m = NAME_WILL.exec(s)) && !NOT_NAMES.has(m[1].toLowerCase()) && !/^(be |be\.|been |like |love )/i.test(m[2])) return {owner: m[1], inferred: false};
  return null;
}
function extract(u, silence) {
  const att = attendeesOf(u), out = [];
  for (const x of u) for (const s of splitSent(x.text)) {
    const r = classify(s, x.speaker, x.side, att); if (!r) continue;
    const notes = r.note ? [r.note] : [];
    if (silence.has(x.line) && [null, 'we', 'someone', 'somebody', 'anyone'].includes(r.owner)) notes.push('no one responded in the transcript');
    out.push({line: x.line, speaker: x.speaker, sentence: s, ownerRaw: r.owner, dueRaw: findDue(s)[1], hedged: HEDGE.test(s), notes});
  }
  return out;
}

// ---- validation (deterministic) ----
function ownerOf(raw, att) {
  if (!raw) return [null, null]; const k = raw.trim().toLowerCase(); if (GENERIC.has(k)) return [null, null];
  const v = att.get(k) || att.get(k.split(' ')[0]); return v ? [v[0], v[1]] : [raw.trim(), 'unknown'];
}
function validate(cands, meet, att) {
  return cands.map((c, i) => {
    const issues = [], notes = [...c.notes]; const [owner, side] = ownerOf(c.ownerRaw, att);
    if (!owner) issues.push('MISSING_OWNER');
    let dueText = null, dueDate = null;
    if (c.dueRaw) { const d = resolveDue(c.dueRaw, meet); dueText = d.raw;
      if (d.problem === 'imprecise') { issues.push('MISSING_DATE'); notes.push(d.note); }
      else if (d.problem) { issues.push('BAD_DATE'); notes.push(d.note); }
      else { dueDate = iso(d.date); notes.push(`due ${d.note}`); }
    } else { issues.push('MISSING_DATE'); notes.push('no time expression found'); }
    if (c.hedged) issues.push('VAGUE_ACTION');
    if (issues.includes('MISSING_OWNER') && HARD.test(c.sentence)) issues.push('UNCLAIMED_HARD');
    const top = PRIO.find(k => issues.includes(k));
    return {id: i + 1, line: c.line, speaker: c.speaker, sentence: c.sentence, owner, owner_side: side, due_text: dueText,
      due_date: dueDate, status: top ? STATUS[top] : 'complete', issues, notes, fixes: PRIO.filter(k => issues.includes(k)).map(k => FIX[k])};
  });
}
function summarize(items) {
  const complete = items.filter(i => i.status === 'complete').length, counts = {};
  for (const k of Object.keys(LABEL)) counts[k] = items.filter(i => i.issues.includes(k)).length;
  return {total: items.length, complete, needs_attention: items.length - complete,
    percent_complete: items.length ? Math.round(100 * complete / items.length) : null, issue_counts: counts};
}
function followupDraft(title, meetIso, items) {
  const NEED = {MISSING_OWNER:'an owner',MISSING_DATE:'a due date',BAD_DATE:'a corrected date',VAGUE_ACTION:'a concrete deliverable',UNCLAIMED_HARD:'a lead or a smaller scope'};
  const ok = items.filter(i => i.status === 'complete'), todo = items.filter(i => i.status !== 'complete');
  const L = [`Subject: Follow-up: ${title || 'meeting'} - next steps (${meetIso})`, '', 'Hi all,', '', 'Thanks for the time today. Here is what we captured.', ''];
  if (ok.length) { L.push('Confirmed (owner and date agreed):'); ok.forEach(i => L.push(`- ${i.owner}, due ${i.due_date}: "${i.sentence}"`)); L.push(''); }
  if (todo.length) { L.push('Still open - please reply with what is missing:');
    todo.forEach(i => L.push(`- "${i.sentence}" - needs ${PRIO.filter(k => i.issues.includes(k)).map(k => NEED[k]).join(', ')}`)); L.push(''); }
  if (!items.length) L.push('We did not record any specific commitments.', '');
  return L.concat(['Best,', '[your name]']).join('\n');
}
function analyse(text, dateArg) {
  validateText(text);
  let title = null, hd = null;
  for (const l of text.split(/\r?\n/)) { let m;
    if (hd === null && (m = H_DATE.exec(l))) hd = m[1]; else if (title === null && (m = H_TITLE.exec(l))) title = m[1]; }
  const ds = dateArg || hd;
  if (!ds) throw new InputError("no meeting date. Add a line 'Meeting date: YYYY-MM-DD' to the transcript or fill in the date box.");
  const meet = parseDateArg(ds), {u, silence} = parseTranscript(text);
  const items = validate(extract(u, silence), meet, attendeesOf(u));
  return {title, meeting_date: iso(meet), header_date: hd, weekday: ABBR[wd(meet)], items, summary: summarize(items),
    followup_draft: followupDraft(title, iso(meet), items)};
}
const api = {analyse, resolveDue, InputError, LABEL, PRIO, UTC_D: D};
if (typeof module !== 'undefined' && module.exports) module.exports = api; else root.GapChecker = api;
})(typeof self !== 'undefined' ? self : this);
