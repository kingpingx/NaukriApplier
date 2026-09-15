/* Job Scout - the browser half of the screener.
 *
 * Reads the job boards that allow browser requests, scores every listing with
 * a port of screener/score.py, and ranks them. Settings live in this browser's
 * localStorage and nowhere else. Keep scoring in step with score.py: the
 * scheduled scan uses the Python one, and the two should agree.
 */
'use strict';

const $ = (s) => document.querySelector(s);
const STORE_KEY = 'jobscout.settings.v1';
const APPLIED_KEY = 'jobscout.applied';
const DEFAULT_REGIONS = ['India', 'APAC', 'Asia', 'Worldwide', 'Anywhere', 'Global'];
const SHORTLIST_AT = 72;
const REVIEW_AT = 55;
const TIMEOUT_MS = 30000;

// --- storage (never required - private windows can refuse it) ----------

function load(key, fallback) {
  try { const v = localStorage.getItem(key); return v ? JSON.parse(v) : fallback; }
  catch { return fallback; }
}
function save(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* ignore */ }
}

// --- text helpers -------------------------------------------------------

const decoder = document.createElement('textarea');
function decode(text) { decoder.innerHTML = text; return decoder.value; }

function stripHtml(text) {
  if (!text) return '';
  // Greenhouse double-escapes its HTML, so decode before and after the strip.
  let t = decode(String(text)).replace(/<[^>]+>/g, ' ');
  return decode(t).replace(/\s+/g, ' ').trim();
}

function esc(text) {
  return String(text ?? '').replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function reEsc(text) { return String(text).replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }

function list(text) {
  return String(text || '').split(/[,\n]/).map((s) => s.trim()).filter(Boolean);
}

function asList(value) {
  if (!value) return [];
  if (typeof value === 'string') value = value.split(',');
  return value.map((v) => String(v).trim()).filter(Boolean);
}

function openTo(where, regions) {
  const text = (where || '').trim().toLowerCase();
  if (!text) return true;
  return regions.some((r) => {
    const term = String(r).trim().toLowerCase();
    return term && new RegExp('(?<![a-z])' + reEsc(term) + '(?![a-z])').test(text);
  });
}

function postedLabel(ms) {
  if (!ms) return '';
  const days = Math.floor((Date.now() - ms) / 86400000);
  return days <= 0 ? 'Today' : days === 1 ? '1 day ago' : `${days} days ago`;
}

function ms(value) {
  if (value == null || value === '') return null;
  const t = typeof value === 'number' ? value : Date.parse(value);
  return Number.isFinite(t) ? t : null;
}

async function getJSON(url) { return JSON.parse(await getText(url)); }

async function getText(url) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), TIMEOUT_MS);
  try {
    const res = await fetch(url, { signal: ctrl.signal });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.text();
  } finally { clearTimeout(timer); }
}

function job(board, key, fields) {
  return { id: `${board}:${key}`, source: board, skills: [], description: '',
           minExp: null, maxExp: null, createdMs: null, ...fields };
}

const remote = (where) => (where ? `Remote - ${where}` : 'Remote');

// --- boards -------------------------------------------------------------
// Himalayas and Working Nomads send no CORS headers, so a browser cannot read
// them; the scheduled scan covers those two.

const READERS = {
  async remotive(s) {
    const data = await getJSON('https://remotive.com/api/remote-jobs');
    return (data.jobs || []).filter((r) => r.url && openTo(r.candidate_required_location, s.regions))
      .map((r) => job('remotive', r.id, {
        title: (r.title || '').trim(), company: (r.company_name || '').trim(), url: r.url,
        skills: asList(r.tags), location: remote(r.candidate_required_location),
        salary: (r.salary || '').trim(), description: stripHtml(r.description),
        createdMs: ms(r.publication_date && r.publication_date + 'Z'),
      }));
  },

  async remoteok(s) {
    const data = await getJSON('https://remoteok.com/api');
    return (data || []).slice(1).filter((r) => r.position && (r.url || r.apply_url)
        && openTo(r.location, s.regions))
      .map((r) => job('remoteok', r.id, {
        title: r.position.trim(), company: (r.company || '').trim(), url: r.url || r.apply_url,
        skills: asList(r.tags), location: remote((r.location || '').trim()),
        salary: r.salary_min && r.salary_max
          ? `USD ${r.salary_min.toLocaleString()}-${r.salary_max.toLocaleString()} annual` : '',
        description: stripHtml(r.description),
        createdMs: r.epoch ? r.epoch * 1000 : ms(r.date),
      }));
  },

  async jobicy(s) {
    const data = await getJSON('https://jobicy.com/api/v2/remote-jobs?count=100&industry=dev');
    return (data.jobs || []).map((r) => ({ r, where: (r.jobGeo || '').replace(/\s*,\s*/g, ', ').trim() }))
      .filter(({ r, where }) => r.url && openTo(where, s.regions))
      .map(({ r, where }) => job('jobicy', r.id, {
        title: stripHtml(r.jobTitle), company: (r.companyName || '').trim(), url: r.url,
        skills: asList(r.jobIndustry), location: remote(where), experience: r.jobLevel || '',
        description: stripHtml(r.jobDescription || r.jobExcerpt), createdMs: ms(r.pubDate),
      }));
  },

  async weworkremotely(s) {
    const feeds = ['remote-full-stack-programming-jobs', 'remote-back-end-programming-jobs',
                   'remote-front-end-programming-jobs', 'remote-devops-sysadmin-jobs'];
    const docs = await Promise.all(feeds.map((f) =>
      getText(`https://weworkremotely.com/categories/${f}.rss`)
        .then((xml) => new DOMParser().parseFromString(xml, 'application/xml'))));
    const jobs = [];
    for (const doc of docs) {
      for (const item of doc.querySelectorAll('item')) {
        const get = (tag) => (item.getElementsByTagName(tag)[0]?.textContent || '').trim();
        const link = get('link') || get('guid');
        const heading = get('title');
        const region = get('region');
        if (!link || !heading || !openTo(region, s.regions)) continue;
        const cut = heading.indexOf(': ');
        jobs.push(job('weworkremotely', link.replace(/\/$/, '').split('/').pop(), {
          title: cut > 0 ? heading.slice(cut + 2).trim() : heading,
          company: cut > 0 ? heading.slice(0, cut).trim() : '',
          url: link, location: remote(region),
          skills: get('skills').split(/,|\band\b/).map((x) => x.trim()).filter(Boolean),
          description: stripHtml(get('description')), createdMs: ms(get('pubDate')),
        }));
      }
    }
    return jobs;
  },

  async hn(s) {
    const search = await getJSON('https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring&hitsPerPage=10');
    const story = (search.hits || []).find((h) => /who is hiring/i.test(h.title || ''));
    if (!story) throw new Error("no 'Who is hiring?' thread");
    const thread = await getJSON(`https://hn.algolia.com/api/v1/items/${story.objectID}`);
    return (thread.children || []).map((c) => hnComment(c, s.regions)).filter(Boolean);
  },

  async greenhouse(s) {
    const jobs = [];
    for (const co of s.companies.greenhouse) {
      const data = await getJSON(`https://boards-api.greenhouse.io/v1/boards/${encodeURIComponent(co)}/jobs?content=true`);
      for (const r of data.jobs || []) {
        const where = (r.location?.name || '').trim();
        if (!r.absolute_url || !atsOpen(where, s)) continue;
        jobs.push(job('greenhouse', `${co}-${r.id}`, {
          title: (r.title || '').trim(), company: (r.company_name || co).trim(), url: r.absolute_url,
          skills: (r.departments || []).map((d) => d.name).filter(Boolean), location: where,
          description: stripHtml(r.content), createdMs: ms(r.first_published || r.updated_at),
        }));
      }
    }
    return jobs;
  },

  async lever(s) {
    const jobs = [];
    for (const co of s.companies.lever) {
      const data = await getJSON(`https://api.lever.co/v0/postings/${encodeURIComponent(co)}?mode=json`);
      for (const r of data || []) {
        const cats = r.categories || {};
        let where = (cats.allLocations || [cats.location || '']).filter(Boolean).join(', ');
        if ((r.workplaceType || '').toLowerCase() === 'remote' && !/remote/i.test(where)) where = remote(where);
        if (!r.hostedUrl || !atsOpen(where, s)) continue;
        const lists = (r.lists || []).map((b) => `${b.text || ''} ${stripHtml(b.content)}`).join(' ');
        jobs.push(job('lever', r.id, {
          title: (r.text || '').trim(), company: co, url: r.hostedUrl,
          skills: cats.team ? [cats.team] : [], location: where,
          description: `${r.descriptionPlain || ''} ${lists}`.trim(), createdMs: r.createdAt || null,
        }));
      }
    }
    return jobs;
  },

  async ashby(s) {
    const jobs = [];
    for (const co of s.companies.ashby) {
      const data = await getJSON(`https://api.ashbyhq.com/posting-api/job-board/${encodeURIComponent(co)}?includeCompensation=true`);
      for (const r of data.jobs || []) {
        if (r.isListed === false || !r.jobUrl) continue;
        let where = [r.location, ...(r.secondaryLocations || []).map((x) => x.location)]
          .filter(Boolean).join(', ');
        if (r.isRemote && !/remote/i.test(where)) where = remote(where);
        if (!atsOpen(where, s)) continue;
        jobs.push(job('ashby', r.id, {
          title: (r.title || '').trim(), company: co, url: r.jobUrl,
          skills: [r.department, r.team].filter(Boolean), location: where,
          salary: r.compensation?.compensationTierSummary || '',
          description: (r.descriptionPlain || '').trim(), createdMs: ms(r.publishedAt),
        }));
      }
    }
    return jobs;
  },
};

const LABELS = {
  remotive: 'Remotive', remoteok: 'Remote OK', jobicy: 'Jobicy', weworkremotely: 'We Work Remotely',
  hn: 'Hacker News', greenhouse: 'Greenhouse', lever: 'Lever', ashby: 'Ashby',
  himalayas: 'Himalayas', workingnomads: 'Working Nomads', apify: 'Naukri', '': 'Naukri',
};
const KEYLESS = ['remotive', 'remoteok', 'jobicy', 'weworkremotely', 'hn'];

const HN_RESTRICTED = ['us', 'usa', 'u.s', 'united states', 'north america', 'americas', 'canada',
  'europe', 'eu', 'uk', 'emea', 'latam', 'cet', 'est', 'pst', 'pt', 'et', 'us-only', 'us only'];
const ROLE_WORDS = /engineer|developer|swe|programmer|architect|devops|sre|scientist|analyst|lead|manager|designer|full[- ]?stack|back[- ]?end|front[- ]?end/i;

function hnComment(c, regions) {
  const text = c.text || '';
  if (!text || !c.id) return null;
  const header = stripHtml(text.split(/<p>/)[0]);
  const parts = header.split('|').map((p) => p.trim()).filter(Boolean);
  if (parts.length < 2) return null;
  const lower = header.toLowerCase();
  if (!lower.includes('remote')) return null;
  if (!openTo(header, regions) &&
      HN_RESTRICTED.some((w) => new RegExp('(?<![a-z])' + reEsc(w) + '(?![a-z])').test(lower))) return null;
  const where = parts.slice(1).find((p) => /remote/i.test(p)) || 'Remote';
  return job('hn', c.id, {
    title: (parts.slice(1).find((p) => ROLE_WORDS.test(p)) || parts[1]).slice(0, 120),
    company: parts[0].replace(/\s*\(?https?:\/\/\S+\)?/g, '').trim().slice(0, 80),
    url: `https://news.ycombinator.com/item?id=${c.id}`, location: where,
    description: stripHtml(text), createdMs: c.created_at_i ? c.created_at_i * 1000 : null,
  });
}

function atsOpen(where, s) {
  const places = s.regions.concat(s.cities.filter((p) =>
    !['remote', 'hybrid', 'work from home'].includes(p.toLowerCase())));
  return openTo(where, places);
}

// --- scoring: a port of screener/score.py -------------------------------

const DEFAULT_WEIGHTS = { skills: 45, title: 25, experience: 15, location: 10, freshness: 5 };
const BASE_SYNONYMS = {
  'ci/cd': 'ci cd', cicd: 'ci cd', js: 'javascript', ts: 'typescript', py: 'python',
  k8s: 'kubernetes', gcp: 'google cloud', aws: 'amazon web services', ml: 'machine learning',
  ai: 'artificial intelligence', db: 'database', oop: 'object oriented programming',
};
const SYMBOL_LANGUAGES = { 'c#': 'csharp', 'c++': 'cplusplus', 'f#': 'fsharp' };
const DEFAULT_SENIORITY = ['lead', 'senior', 'sr', 'principal', 'staff', 'architect', 'manager', 'head'];
const GENERIC_TAGS = new Set(['development', 'software', 'software development', 'software engineering',
  'engineering', 'cloud', 'core', 'cd', 'coding', 'programming', 'technology', 'it', 'microsoft']);

const round1 = (x) => Math.round(x * 10) / 10;

function compileSynonyms(map) {
  return Object.entries({ ...map, ...SYMBOL_LANGUAGES }).map(([term, repl]) =>
    [new RegExp('(?<![\\w+#])' + reEsc(term) + '(?![\\w+#])', 'g'), repl]);
}

function norm(text, cfg) {
  let t = String(text || '').toLowerCase();
  for (const [re, repl] of cfg._syn) t = t.replace(re, repl);
  return t.replace(/[^a-z0-9 ]+/g, ' ');
}
const tokens = (text, cfg) => new Set(norm(text, cfg).split(/\s+/).filter((t) => t.length > 1));
const skillKey = (skill, cfg) => norm(skill, cfg).split(/\s+/).filter(Boolean).join(' ');

function buildConfig(s, pack) {
  const synonyms = { ...BASE_SYNONYMS };
  for (const [k, v] of Object.entries(pack.synonyms || {})) synonyms[String(k).toLowerCase()] = String(v).toLowerCase();
  const cfg = {
    synonyms,
    must_have_any: pack.must_have_any || [],
    exclude_title_keywords: [...(pack.exclude_title_keywords || []), ...s.exclude],
    exclude_companies: s.excludeCompanies,
    vocabulary: pack.vocabulary || [],
    seniority_terms: pack.seniority_terms || DEFAULT_SENIORITY,
    profile_skills: s.skills,
    profile_years: s.years,
    titles: s.titles,
    profile_text: '',
    profile_evidence: s.resume.slice(0, 20000),
    preferred_locations: s.cities,
    max_experience_gap_years: 2,
  };
  cfg._syn = compileSynonyms(synonyms);
  cfg._have = new Set(cfg.profile_skills.map((x) => skillKey(x, cfg)));
  cfg._haveBlob = [...cfg._have].join(' ') + ' ' + norm(cfg.profile_evidence, cfg);
  return cfg;
}

function gateHit(term, haystack, cfg) {
  const key = skillKey(term, cfg);
  return !!key && new RegExp('(?<![a-z0-9])' + reEsc(key) + '(?![a-z0-9])').test(haystack);
}

function hardReject(j, cfg) {
  const title = (j.title || '').toLowerCase();
  const company = (j.company || '').toLowerCase();
  for (const b of cfg.exclude_companies) if (b && company.includes(b.toLowerCase())) return `company excluded (${b})`;
  for (const w of cfg.exclude_title_keywords) if (w && title.includes(String(w).toLowerCase())) return `title contains '${w}'`;
  if (cfg.must_have_any.length) {
    const hay = norm([j.title, j.skills.join(' '), j.description].join(' '), cfg);
    if (!cfg.must_have_any.some((t) => String(t).trim() && gateHit(t, hay, cfg))) return 'not your field (matches none of the role gate)';
  }
  const years = cfg.profile_years;
  if (years != null && j.minExp != null && j.minExp - years > cfg.max_experience_gap_years) {
    return `needs ${j.minExp}y, you have ${years}y`;
  }
  return null;
}

function matchedSkills(wanted, cfg) {
  return wanted.filter((skill) => {
    const key = skillKey(skill, cfg);
    return key && (cfg._have.has(key) || new RegExp('\\b' + reEsc(key) + '\\b').test(cfg._haveBlob));
  });
}

function descriptionSkills(j, cfg) {
  const vocab = cfg.vocabulary.length ? cfg.vocabulary : cfg.profile_skills;
  const text = (j.description || '').toLowerCase();
  if (!text) return [];
  return vocab.filter((t) => new RegExp('(?<![\\w+#])' + reEsc(String(t).toLowerCase()) + '(?![\\w+#])').test(text)).slice(0, 12);
}

function skillScore(j, cfg, cap) {
  let wanted = j.skills.filter((x) => x.trim() && !GENERIC_TAGS.has(x.trim().toLowerCase()));
  if (!wanted.length) wanted = descriptionSkills(j, cfg);
  if (!wanted.length) return [round1(cap * 0.4), []];
  const matched = matchedSkills(wanted, cfg);
  const ratio = matched.length / wanted.length;
  const depth = Math.min(matched.length / 8, 1);
  return [round1(cap * (0.65 * ratio + 0.35 * depth)), matched];
}

function titleScore(j, cfg, cap) {
  const titleTokens = tokens(j.title, cfg);
  if (!titleTokens.size) return 0;
  const target = new Set();
  for (const t of [...cfg.must_have_any, ...cfg.titles, cfg.profile_text]) for (const x of tokens(t, cfg)) target.add(x);
  if (!target.size) return round1(cap * 0.4);
  const overlap = [...titleTokens].filter((x) => target.has(x)).length / titleTokens.size;
  let score = cap * 0.8 * overlap;
  if (cfg.seniority_terms.some((w) => titleTokens.has(String(w).toLowerCase()))) score += cap * 0.2;
  return round1(Math.min(score, cap));
}

function experienceScore(j, cfg, cap) {
  const years = cfg.profile_years;
  if (years == null || j.minExp == null) return round1(cap * 0.6);
  const top = j.maxExp != null ? j.maxExp : j.minExp + 3;
  if (j.minExp <= years && years <= top) return cap;
  if (years < j.minExp) return round1(Math.max(0, cap - (j.minExp - years) * cap * 0.4));
  return round1(Math.max(0, cap - (years - top) * (cap / 6)));
}

function locationScore(j, cfg, cap) {
  const text = (j.location || '').toLowerCase();
  if (!text) return round1(cap * 0.5);
  if (text.includes('remote') || text.includes('work from home')) return cap;
  if (cfg.preferred_locations.some((p) => p && text.includes(p.toLowerCase()))) return cap;
  return round1(cap * 0.2);
}

function freshnessScore(j, cap) {
  if (!j.createdMs) return round1(cap * 0.5);
  const age = (Date.now() - j.createdMs) / 86400000;
  if (age <= 2) return cap;
  if (age <= 7) return round1(cap * 0.6);
  if (age <= 30) return round1(cap * 0.2);
  return 0;
}

function score(j, cfg) {
  const reason = hardReject(j, cfg);
  if (reason) return { ...j, score: 0, rejected: reason, matched: [], breakdown: {} };
  const w = DEFAULT_WEIGHTS;
  const [skills, matched] = skillScore(j, cfg, w.skills);
  const breakdown = {
    skills, title: titleScore(j, cfg, w.title), experience: experienceScore(j, cfg, w.experience),
    location: locationScore(j, cfg, w.location), freshness: freshnessScore(j, w.freshness),
  };
  return { ...j, score: round1(Object.values(breakdown).reduce((a, b) => a + b, 0)), matched, breakdown };
}

function dedupe(jobs) {
  const seen = new Set();
  const key = (t) => (t || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
  return jobs.filter((j) => {
    const k = key(j.company) + '|' + key(j.title);
    if (key(j.company) && key(j.title) && seen.has(k)) return false;
    seen.add(k);
    return true;
  });
}

// --- settings form ------------------------------------------------------

let PACKS = {};
let lastResults = [];

function readForm() {
  const years = parseFloat($('#years').value);
  const days = parseFloat($('#days').value);
  return {
    pack: $('#pack').value,
    years: Number.isFinite(years) ? years : null,
    skills: list($('#skills').value),
    resume: $('#resume').value || '',
    titles: list($('#titles').value),
    cities: list($('#cities').value),
    regions: list($('#regions').value).length ? list($('#regions').value) : DEFAULT_REGIONS,
    regionsText: $('#regions').value,
    days: Number.isFinite(days) ? days : null,
    exclude: list($('#exclude').value),
    excludeCompanies: list($('#excludeCompanies').value),
    boards: [...document.querySelectorAll('#boards input:checked')].map((i) => i.value),
    companies: { greenhouse: list($('#greenhouse').value), lever: list($('#lever').value), ashby: list($('#ashby').value) },
  };
}

function fillForm(s) {
  $('#pack').value = s.pack && PACKS[s.pack] ? s.pack : ($('#pack').value || '');
  $('#years').value = s.years ?? '';
  $('#skills').value = (s.skills || []).join(', ');
  $('#resume').value = s.resume || '';
  $('#titles').value = (s.titles || []).join(', ');
  $('#cities').value = (s.cities || []).join(', ');
  $('#regions').value = s.regionsText || '';
  $('#days').value = s.days ?? '';
  $('#exclude').value = (s.exclude || []).join(', ');
  $('#excludeCompanies').value = (s.excludeCompanies || []).join(', ');
  for (const board of ['greenhouse', 'lever', 'ashby']) $('#' + board).value = (s.companies?.[board] || []).join(', ');
  const chosen = new Set(s.boards || KEYLESS);
  for (const input of document.querySelectorAll('#boards input')) input.checked = chosen.has(input.value);
}

function profileLine(s) {
  const boards = s.boards.length + ['greenhouse', 'lever', 'ashby'].filter((b) => s.companies[b].length).length;
  return `${s.pack || 'no pack'} · ${s.skills.length} skills · ${boards} sources`;
}

function persist() {
  const s = readForm();
  save(STORE_KEY, s);
  $('#profile-line').textContent = profileLine(s);
}

// --- search -------------------------------------------------------------

async function search() {
  const s = readForm();
  save(STORE_KEY, s);
  if (!s.skills.length) {
    $('#settings').open = true;
    status('<span class="chip bad">Add a few skills first - they are what jobs are scored against.</span>');
    $('#skills').focus();
    return;
  }
  const pack = PACKS[s.pack] || {};
  const cfg = buildConfig(s, pack);
  const sources = [...s.boards, ...['greenhouse', 'lever', 'ashby'].filter((b) => s.companies[b].length)];
  const state = Object.fromEntries(sources.map((b) => [b, 'loading']));
  const draw = () => status(sources.map((b) => {
    const v = state[b];
    const cls = v === 'loading' ? 'wait' : typeof v === 'number' ? 'ok' : 'bad';
    const text = v === 'loading' ? '…' : typeof v === 'number' ? v : 'failed';
    return `<span class="chip ${cls}" title="${esc(typeof v === 'string' && v !== 'loading' ? v : '')}">${esc(LABELS[b])} ${esc(text)}</span>`;
  }).join(''));
  draw();
  $('#go').disabled = true;

  const found = await Promise.all(sources.map(async (b) => {
    try {
      const jobs = await READERS[b](s);
      state[b] = jobs.length;
      return jobs;
    } catch (err) {
      state[b] = String(err.message || err);
      return [];
    } finally { draw(); }
  }));
  $('#go').disabled = false;

  let jobs = dedupe(found.flat().filter((j) => j.title));
  if (s.days) jobs = jobs.filter((j) => !j.createdMs || (Date.now() - j.createdMs) / 86400000 <= s.days);
  const scored = jobs.map((j) => score(j, cfg));
  lastResults = scored.filter((j) => !j.rejected).sort((a, b) => b.score - a.score);
  const rejected = scored.length - lastResults.length;
  const good = lastResults.filter((j) => j.score >= REVIEW_AT).length;
  $('#status').insertAdjacentHTML('beforeend',
    `<p class="summary">${scored.length} listings open to you · ${rejected} outside your field · <strong>${good}</strong> worth a read</p>`);
  render();
}

function status(html) { $('#status').innerHTML = html; }

// --- results ------------------------------------------------------------

function band(scoreValue) {
  return scoreValue >= SHORTLIST_AT ? 'hi' : scoreValue >= REVIEW_AT ? 'mid' : 'lo';
}

function safeUrl(url) { return /^https?:\/\//i.test(url || '') ? url : '#'; }

function card(j, applied) {
  const meta = [LABELS[j.source] || j.source, j.location, j.experience, j.salary,
                j.posted || postedLabel(j.createdMs)].filter(Boolean);
  const why = j.why || Object.entries(j.breakdown || {}).map(([k, v]) => `${k} ${v}`).join(' · ');
  const matched = (j.matched || []).slice(0, 8);
  return `<article class="job ${applied ? 'applied' : ''}">
    <div class="score ${band(j.score)}">${esc(j.score)}</div>
    <div class="body">
      <h3><a href="${esc(safeUrl(j.url))}" target="_blank" rel="noopener">${esc(j.title)}</a></h3>
      <p class="co">${esc(j.company)}</p>
      <p class="meta">${meta.map(esc).join(' · ')}</p>
      ${matched.length ? `<p class="tags">${matched.map((m) => `<span>${esc(m)}</span>`).join('')}</p>` : ''}
      <p class="why">${esc(why)}</p>
    </div>
    <label class="done" title="Mark as applied"><input type="checkbox" data-id="${esc(j.id)}" ${applied ? 'checked' : ''}> Applied</label>
  </article>`;
}

function render() {
  const q = $('#filter').value.trim().toLowerCase();
  const showLow = $('#showLow').checked;
  const applied = load(APPLIED_KEY, {});
  const rows = lastResults.filter((j) => (showLow || j.score >= REVIEW_AT) &&
    (!q || `${j.title} ${j.company} ${j.location} ${(j.matched || []).join(' ')}`.toLowerCase().includes(q)));
  $('#results').innerHTML = rows.length ? rows.map((j) => card(j, applied[j.id])).join('')
    : lastResults.length ? '<p class="empty">Nothing matches. Tick <em>Show low scores</em> or clear the filter.</p>' : '';
}

function onApplied(e) {
  const box = e.target.closest('input[data-id]');
  if (!box) return;
  const applied = load(APPLIED_KEY, {});
  if (box.checked) applied[box.dataset.id] = new Date().toISOString().slice(0, 10);
  else delete applied[box.dataset.id];
  save(APPLIED_KEY, applied);
  box.closest('.job').classList.toggle('applied', box.checked);
}

// --- scheduled scan tab -------------------------------------------------

async function loadScan() {
  const head = $('#scan-head');
  try {
    const data = await getJSON('scan/latest.json');
    const rows = [...(data.shortlist || []), ...(data.review || [])].map((r) => ({
      id: r.job_id, title: r.title, company: r.company, url: r.url, location: r.location,
      salary: r.salary_label, experience: r.experience_label, posted: r.posted_label,
      source: r.source || '', score: r.score, why: r.why, matched: [],
    }));
    const when = data.at ? new Date(data.at).toLocaleString() : 'unknown';
    head.innerHTML = `<p><strong>${rows.length}</strong> ranked from ${esc(data.collected)} listings · scanned ${esc(when)}.
      Includes Himalayas and Working Nomads. <a href="scan/">Open the full tracker page</a></p>`;
    const applied = load(APPLIED_KEY, {});
    $('#scan-results').innerHTML = rows.map((j) => card(j, applied[j.id])).join('');
  } catch {
    head.innerHTML = '<p>No scheduled scan has been published yet. It runs every 4 hours once the repo secrets are set - see the README.</p>';
  }
}

function showTab(name) {
  for (const tab of document.querySelectorAll('.tab')) tab.setAttribute('aria-selected', String(tab.dataset.tab === name));
  $('#tab-search').hidden = name !== 'search';
  $('#tab-scan').hidden = name !== 'scan';
  if (name === 'scan') loadScan();
}

// --- resume upload and the RESUME_JSON secret ---------------------------

const GH_KEY = 'jobscout.github';
let parsedResume = null;

function applyResume(r) {
  $('#skills').value = r.skills.join(', ');
  if (r.years != null) $('#years').value = r.years;
  if (r.titles.length) $('#titles').value = r.titles.join(', ');
  if (r.location && !$('#cities').value.trim()) $('#cities').value = r.location;
  $('#resume').value = r.text;
  persist();
  $('#resumeOut').hidden = false;
  $('#resumeSummary').textContent =
    `${r.skills.length} skills · ${r.years != null ? r.years + ' years' : 'years not found'} · ` +
    `${r.titles[0] || 'no title found'} · ${r.location || 'no city found'}. Contact details removed.`;
}

function ghStatus(text, bad) {
  $('#ghStatus').textContent = text;
  $('#ghStatus').classList.toggle('bad-text', !!bad);
}

function wireResume() {
  const saved = load(GH_KEY, {});
  $('#ghRepo').value = saved.repo || GitHubSecrets.guessRepo();
  if (saved.token) { $('#ghToken').value = saved.token; $('#ghRemember').checked = true; }
  const repo = $('#ghRepo').value.trim();
  if (repo) {
    $('#secretsLink').href = `https://github.com/${repo}/settings/secrets/actions`;
    $('#secretsLink').hidden = false;
  }

  $('#resumeFile').addEventListener('change', async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    $('#resumeOut').hidden = false;
    $('#resumeSummary').textContent = 'Reading…';
    try {
      const text = await ResumeParser.readFile(file);
      const pack = PACKS[$('#pack').value] || {};
      parsedResume = ResumeParser.parse(text, pack.vocabulary || []);
      applyResume(parsedResume);
    } catch (err) {
      parsedResume = null;
      $('#resumeSummary').textContent = `Could not read it: ${err.message || err}`;
    }
  });

  $('#copyJson').addEventListener('click', async () => {
    if (!parsedResume) return;
    try {
      await navigator.clipboard.writeText(JSON.stringify(parsedResume));
      ghStatus('Copied. Paste it as the RESUME_JSON secret.');
    } catch {
      ghStatus('Your browser blocked the clipboard - use Save to GitHub instead.', true);
    }
  });

  $('#ghSave').addEventListener('click', async () => {
    const repoName = $('#ghRepo').value.trim();
    const token = $('#ghToken').value.trim();
    if (!parsedResume) return ghStatus('Choose your resume file first.', true);
    if (!/^[\w.-]+\/[\w.-]+$/.test(repoName)) return ghStatus('Repository should look like owner/repo.', true);
    if (!token) return ghStatus('Paste an access token first.', true);
    save(GH_KEY, { repo: repoName, ...($('#ghRemember').checked ? { token } : {}) });
    $('#ghSave').disabled = true;
    try {
      ghStatus('Encrypting and saving RESUME_JSON…');
      await GitHubSecrets.saveSecret(repoName, token, 'RESUME_JSON', JSON.stringify(parsedResume));
      ghStatus('Saved. Starting a scan…');
      try {
        await GitHubSecrets.runScan(repoName, token);
        ghStatus('Saved, and a scan has started. The Scheduled scan tab updates in about 3 minutes.');
      } catch (err) {
        ghStatus(`Saved, but the scan could not start (${err.message}). It will use the new resume on its next run.`, true);
      }
    } catch (err) {
      ghStatus(err.message || String(err), true);
    } finally { $('#ghSave').disabled = false; }
  });
}

// --- boot ---------------------------------------------------------------

async function boot() {
  $('#boards').innerHTML = KEYLESS.map((b) =>
    `<label><input type="checkbox" value="${b}"> ${esc(LABELS[b])}</label>`).join('');
  try { PACKS = await getJSON('packs.json'); } catch { PACKS = {}; }
  $('#pack').innerHTML = Object.entries(PACKS).map(([k, p]) =>
    `<option value="${esc(k)}">${esc(k)}${p.description ? ' - ' + esc(p.description.slice(0, 60)) : ''}</option>`).join('')
    || '<option value="">(no packs found)</option>';

  const saved = load(STORE_KEY, null);
  if (saved) fillForm(saved);
  else {
    fillForm({ pack: PACKS['dotnet-fullstack'] ? 'dotnet-fullstack' : Object.keys(PACKS)[0], boards: KEYLESS });
    $('#settings').open = true;
  }
  $('#profile-line').textContent = profileLine(readForm());

  wireResume();
  $('#form').addEventListener('input', (e) => { if (!e.target.closest('.gh, #resumeFile')) persist(); });
  $('#go').addEventListener('click', search);
  $('#filter').addEventListener('input', render);
  $('#showLow').addEventListener('change', render);
  $('#results').addEventListener('change', onApplied);
  $('#scan-results').addEventListener('change', onApplied);
  $('#pull').addEventListener('click', () => {
    const s = readForm();
    const pack = PACKS[s.pack] || {};
    const text = s.resume.toLowerCase();
    const found = (pack.vocabulary || []).filter((t) =>
      new RegExp('(?<![\\w+#])' + reEsc(String(t).toLowerCase()) + '(?![\\w+#])').test(text));
    const merged = [...new Set([...s.skills, ...found])];
    $('#skills').value = merged.join(', ');
    persist();
    status(`<span class="chip ok">Pulled ${found.length} skills from your resume</span>`);
  });
  for (const tab of document.querySelectorAll('.tab')) {
    tab.addEventListener('click', () => { history.replaceState(null, '', '#' + tab.dataset.tab); showTab(tab.dataset.tab); });
  }
  showTab(location.hash === '#scan' ? 'scan' : 'search');
}

// Exposed for the parity test against score.py.
window.JobScout = { buildConfig, score, openTo, hnComment, READERS };

boot();
