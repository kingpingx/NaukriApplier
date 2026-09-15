/* Resume parsing in the browser - a port of screener/resume.py.
 *
 * The file never leaves the page: PDF.js extracts the text here, and the same
 * rules as the Python parser turn it into {skills, years, skill_years, titles,
 * location, text}. Contact details are stripped before any of it is used, and
 * the result has no `contact` block, so it is safe to store as RESUME_JSON.
 * Keep the patterns in step with resume.py.
 */
'use strict';

(() => {
  const PDFJS = 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js';
  const PDFJS_WORKER = 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js';

  const SKILL_HEADINGS = /^\s*(technical\s+)?(skills?|competenc(y|ies)|technolog(y|ies)|tech\s+stack|tools?\s*(&|and)?\s*(technolog(y|ies))?|expertise|proficienc(y|ies))\s*:?\s*$/gim;
  const SECTION_BREAK = /^\s*(?:(?:work|professional|relevant|industry|career|employment|academic|personal|key|selected)\s+)?(experience|employment|education|projects?|certificat|achievements?|summary|profile|objective|awards?|publications?|languages?|interests?|references?|declaration)\b/gim;
  const SKILL_SUBLABEL = /^\s*[A-Za-z][A-Za-z0-9 &/,'+.-]{0,44}:\s*/;
  const SKILL_SPLIT = /[,;|•▪·‣⁃\n\t]+|\s{3,}|(?<=\w)\s+\/\s+(?=\w)/;
  const SKILL_NOISE = new Set(['and', 'or', 'with', 'using', 'etc', 'various', 'other', 'others', 'including',
    'knowledge', 'hands', 'on', 'hands-on', 'experience', 'expertise', 'familiar', 'proficient', 'strong',
    'good', 'excellent', 'basic', 'advanced', 'working', 'years', 'year', 'yrs', 'months', 'level', 'skills',
    'skill', 'tools', 'tool', 'technologies', 'technology', 'frameworks', 'framework', 'languages', 'language']);

  const MON = '(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\\.?\\s*,?\\s*\'?\\d{2,4}';
  const DATE_RANGE_SRC = `(?<from>${MON}|\\d{1,2}/\\d{4}|\\d{4})\\s*(?:-|–|—|to|until|through)\\s*` +
    `(?<to>${MON}|\\d{1,2}/\\d{4}|\\d{4}|present|current|now|till\\s*date|ongoing)`;
  const dateRange = () => new RegExp(DATE_RANGE_SRC, 'gi');
  const MONTHS = { jan: 1, feb: 2, mar: 3, apr: 4, may: 5, jun: 6, jul: 7, aug: 8, sep: 9, oct: 10, nov: 11, dec: 12 };
  const STATED_YEARS = /(?:(?:over|more\s+than|nearly|about|around|approx\w*)\s+)?(\d{1,2}(?:\.\d)?)\s*\+?\s*(?:years?|yrs?)\b(?:[^.\n]{0,40}?experien)?/gi;
  const SKILL_YEARS = /([A-Za-z][A-Za-z0-9+#./ _-]{1,34}?)\s*[\-:–(]{1,2}\s*(\d{1,2}(?:\.\d)?)\s*\+?\s*(?:years?|yrs?)\b/gi;

  const EMAIL = /[\w.+-]+@[\w-]+\.[\w.]{2,}/g;
  const PHONE = /(?:\+\d{1,3}[\s-]?)?(?:\(\d{2,4}\)[\s-]?)?\d{3,5}[\s-]?\d{3,5}(?:[\s-]?\d{2,4})?/g;
  const URL_RE = /(?:https?:\/\/|www\.)[^\s,;)\]]+/gi;

  const CITIES = ['Bengaluru', 'Bangalore', 'Pune', 'Mumbai', 'Hyderabad', 'Chennai', 'Delhi', 'New Delhi',
    'Gurgaon', 'Gurugram', 'Noida', 'Kolkata', 'Ahmedabad', 'Jaipur', 'Indore', 'Chandigarh', 'Kochi',
    'Coimbatore', 'Thiruvananthapuram', 'Nagpur', 'Bhubaneswar', 'Mysuru', 'Mysore', 'Vadodara', 'Surat',
    'Lucknow', 'Remote', 'Hybrid', 'Work From Home'];

  const ROLE_WORDS = /\b(engineer|developer|analyst|lead|manager|architect|consultant|specialist|administrator|designer|scientist|tester|qa|sdet|intern|associate|executive|officer|head|director|principal|staff|senior|junior|sr|jr)\b/i;
  const DATE_REMAINS = /\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s*,?\s*'?\d{2,4}\b|\b(?:present|current|ongoing|till\s*date)\b|\b(19|20)\d{2}\b/gi;
  const TITLE_SEPARATOR = /\s*[—–|·•]\s*|\s+-\s+|\s*,\s*|\s+@\s+|\s+at\s+/i;

  const reEsc = (s) => String(s).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const round2 = (x) => Math.round(x * 100) / 100;

  // Python's str.strip(chars).
  function strip(s, chars) {
    let a = 0, b = s.length;
    while (a < b && chars.includes(s[a])) a++;
    while (b > a && chars.includes(s[b - 1])) b--;
    return s.slice(a, b);
  }

  function cleanSkill(raw) {
    let skill = raw.replace(/\(.*?\)/g, ' ').replace(/[^\w+#./ -]+/g, ' ');
    skill = strip(skill.replace(/\s+/g, ' ').trim(), ' -./');
    if (!skill || skill.length > 40) return null;
    const words = skill.toLowerCase().split(/\s+/).filter(Boolean);
    if (!words.length || words.every((w) => SKILL_NOISE.has(w))) return null;
    if (words.length > 5) return null;
    if (skill.length < 2 || /^\d+$/.test(skill)) return null;
    return skill;
  }

  function sectionStop(block) {
    for (const m of block.matchAll(new RegExp(SECTION_BREAK.source, 'gim'))) {
      const lineStart = block.lastIndexOf('\n', m.index - 1) + 1;
      const lineEnd = block.indexOf('\n', m.index);
      const line = block.slice(lineStart, lineEnd === -1 ? block.length : lineEnd);
      const colon = line.indexOf(':');
      if (colon !== -1 && line.slice(colon + 1).trim()) continue;   // "Languages: C#, Java"
      if (!block.slice(0, m.index).trim()) continue;
      return m.index;
    }
    return null;
  }

  const stripSublabels = (block) => block.split('\n').map((l) => l.replace(SKILL_SUBLABEL, '')).join('\n');

  function extractSkills(text, vocabulary) {
    const found = [];
    const seen = new Set();
    const add = (skill) => {
      if (!skill) return;
      const key = skill.toLowerCase();
      if (!seen.has(key)) { seen.add(key); found.push(skill); }
    };
    for (const heading of text.matchAll(new RegExp(SKILL_HEADINGS.source, 'gim'))) {
      let block = text.slice(heading.index + heading[0].length);
      const stop = sectionStop(block);
      if (stop !== null) block = block.slice(0, stop);
      block = stripSublabels(block.slice(0, 2000)).replace(/\([^)]*\)?/g, ' ');
      for (const piece of block.split(SKILL_SPLIT)) add(cleanSkill(piece || ''));
    }
    const lowered = text.toLowerCase();
    for (const known of vocabulary || []) {
      if (new RegExp('(?<![\\w+#])' + reEsc(String(known).toLowerCase()) + '(?![\\w+#])').test(lowered)) add(known);
    }
    return found;
  }

  function monthYear(token) {
    token = token.trim().toLowerCase().replace(/[.,']/g, '');
    if (/^\d{4}$/.test(token)) return [1, +token];
    const slash = token.match(/^(\d{1,2})\/(\d{4})$/);
    if (slash) return [+slash[1], +slash[2]];
    const named = token.match(/^([a-z]{3})[a-z]*\s*(\d{2,4})/);
    if (named && MONTHS[named[1]]) {
      let year = +named[2];
      if (year < 100) year += year < 70 ? 2000 : 1900;
      return [MONTHS[named[1]], year];
    }
    return null;
  }

  function extractYears(text) {
    for (const m of text.slice(0, 1200).matchAll(new RegExp(STATED_YEARS.source, 'gi'))) {
      const value = parseFloat(m[1]);
      if (value > 0 && value <= 50) return round2(value);
    }
    const now = new Date();
    const today = [now.getMonth() + 1, now.getFullYear()];
    const key = (my) => my[1] * 12 + my[0];
    const spans = [];
    for (const m of text.matchAll(dateRange())) {
      const start = monthYear(m.groups.from);
      const endRaw = m.groups.to.trim().toLowerCase();
      const end = /^(present|current|now|till\s*date|ongoing)/.test(endRaw) ? today : monthYear(endRaw);
      if (!start || !end || key(end) < key(start)) continue;
      if (!(start[1] >= 1970 && start[1] <= today[1] && end[1] >= 1970 && end[1] <= today[1] + 1)) continue;
      spans.push([start, end]);
    }
    if (!spans.length) return null;
    spans.sort((a, b) => key(a[0]) - key(b[0]));
    const merged = [];
    for (const [start, end] of spans) {
      const last = merged[merged.length - 1];
      if (last && key(start) <= key(last[1])) { if (key(end) > key(last[1])) last[1] = end; }
      else merged.push([start, end]);
    }
    const months = merged.reduce((sum, [s, e]) => sum + (e[1] - s[1]) * 12 + (e[0] - s[0]), 0);
    return months > 0 ? round2(months / 12) : null;
  }

  function extractSkillYears(text) {
    const out = {};
    for (const m of text.matchAll(new RegExp(SKILL_YEARS.source, 'gi'))) {
      const skill = cleanSkill(m[1]);
      const years = parseFloat(m[2]);
      if (skill && years > 0 && years <= 50) {
        const k = skill.toLowerCase();
        out[k] = Math.max(out[k] || 0, years);
      }
    }
    return out;
  }

  function titleFromLine(line) {
    let stripped = line.replace(dateRange(), ' ').replace(new RegExp(DATE_REMAINS.source, 'gi'), ' ');
    stripped = strip(stripped.replace(/\s{2,}/g, ' ').trim(), ' \t-–—•|,');
    for (let segment of stripped.split(TITLE_SEPARATOR)) {
      segment = strip(segment || '', ' \t-–—•|.');
      if (segment.length > 3 && segment.length < 60 && ROLE_WORDS.test(segment)) return segment;
    }
    return null;
  }

  function extractHeadline(text) {
    for (let line of text.slice(0, 600).split(/\r?\n/).slice(1, 6)) {
      line = strip(line, ' \t-•|');
      if (!(line.length > 3 && line.length < 80) || dateRange().test(line)) continue;
      const title = titleFromLine(line);
      if (title) return title;
    }
    return null;
  }

  function extractTitles(text) {
    const titles = [];
    const seen = new Set();
    const add = (t) => { if (t && !seen.has(t.toLowerCase())) { seen.add(t.toLowerCase()); titles.push(t); } };
    for (const m of text.matchAll(dateRange())) {
      const window = text.slice(Math.max(0, m.index - 200), m.index + 120);
      for (let line of window.split(/\r?\n/)) {
        line = strip(line, ' \t-•|');
        if (!(line.length > 3 && line.length < 90) || !ROLE_WORDS.test(line)) continue;
        add(titleFromLine(line));
      }
    }
    add(extractHeadline(text));
    return titles.slice(0, 8);
  }

  function extractLocation(text) {
    const header = text.slice(0, 900);
    for (const city of CITIES) if (new RegExp('\\b' + reEsc(city) + '\\b', 'i').test(header)) return city;
    let best = null;
    CITIES.forEach((city, index) => {
      if (['Remote', 'Hybrid', 'Work From Home'].includes(city)) return;
      const hits = (text.match(new RegExp('\\b' + reEsc(city) + '\\b', 'gi')) || []).length;
      if (hits && (!best || hits > best.hits || (hits === best.hits && index < best.index))) best = { hits, index, city };
    });
    return best ? best.city : null;
  }

  const redact = (text) => text.replace(EMAIL, ' ').replace(URL_RE, ' ').replace(PHONE, ' ');

  function parse(text, vocabulary) {
    const body = redact(text);
    return {
      skills: extractSkills(body, vocabulary),
      years: extractYears(body),
      skill_years: extractSkillYears(body),
      titles: extractTitles(body),
      location: extractLocation(body),
      text: body.replace(/\s+/g, ' ').trim().slice(0, 20000),
    };
  }

  // --- reading the file ---------------------------------------------------

  function loadScript(src) {
    return new Promise((resolve, reject) => {
      if (document.querySelector(`script[src="${src}"]`)) return resolve();
      const el = document.createElement('script');
      el.src = src; el.onload = resolve; el.onerror = () => reject(new Error(`could not load ${src}`));
      document.head.appendChild(el);
    });
  }

  async function pdfText(buffer) {
    await loadScript(PDFJS);
    window.pdfjsLib.GlobalWorkerOptions.workerSrc = PDFJS_WORKER;
    const pdf = await window.pdfjsLib.getDocument({ data: buffer }).promise;
    const pages = [];
    for (let n = 1; n <= pdf.numPages; n++) {
      const content = await (await pdf.getPage(n)).getTextContent();
      // Rebuild lines the way pdfplumber does: a new line when the baseline
      // moves, a space when there is a visible gap between two runs.
      let out = '', lastY = null, lastEnd = null;
      for (const item of content.items) {
        if (!('str' in item)) continue;
        const [x, y] = [item.transform[4], item.transform[5]];
        if (lastY !== null && Math.abs(y - lastY) > 2) { out += '\n'; lastEnd = null; }
        else if (lastEnd !== null && x - lastEnd > 1.5 && !out.endsWith(' ') && !item.str.startsWith(' ')) out += ' ';
        out += item.str;
        lastY = y; lastEnd = x + (item.width || 0);
        if (item.hasEOL) { out += '\n'; lastY = null; lastEnd = null; }
      }
      pages.push(out.replace(/[ \t]+\n/g, '\n'));
    }
    return pages.join('\n');
  }

  async function readFile(file) {
    const name = (file.name || '').toLowerCase();
    if (name.endsWith('.pdf') || file.type === 'application/pdf') {
      const text = await pdfText(await file.arrayBuffer());
      if (!text.trim()) throw new Error('This PDF has no text layer (a scanned image?). Export a text-based PDF.');
      return text;
    }
    if (name.endsWith('.txt') || name.endsWith('.md') || file.type.startsWith('text/')) return file.text();
    throw new Error('Use a PDF or a .txt file. For Word, save as PDF first.');
  }

  window.ResumeParser = { parse, readFile, pdfText, redact };
})();
