/* ===== Foldly – Main application JS ===== */

// --- DOM refs ---
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

const urlInput = $('#url-input');
const btn = $('#summarize-btn');
const loadingEl = $('#loading');
const statusText = $('#status-text');
const etaText = $('#eta-text');
const progressTrack = $('#progress-track');
const progressFill = $('#progress-fill');
const stepTranscript = $('#step-transcript');
const stepAnalyze = $('#step-analyze');
const stepDone = $('#step-done');
const resultEl = $('#result');
const resultHeader = $('#result-header');
const overallSummaryEl = $('#overall-summary');
const sectionsEl = $('#sections');
const playerContainer = $('#player-container');
const keyQuotesEl = $('#key-quotes');
const resultMetaBar = $('#result-meta-bar');
const searchInput = $('#search-input');
const searchCount = $('#search-count');
const toastEl = $('#toast');

let videoMeta = {};
let rawSegments = [];
let summaryData = null;
let allOpen = false;
let streamStartTime = 0;
let currentUser = null;
let ytPlayer = null;

// --- Init ---
document.addEventListener('DOMContentLoaded', () => {
  fetchUser();
  loadTheme();

  urlInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') handleSubmit(); });
  searchInput.addEventListener('input', debounce(handleSearch, 200));
  document.addEventListener('keydown', handleKeyboard);

  // Check for shared fold data
  const sharedData = $('#shared-fold-data');
  if (sharedData) {
    try {
      const fold = JSON.parse(sharedData.textContent);
      loadSharedFold(fold);
    } catch (e) {}
  }
});


// ===== Auth =====

async function fetchUser() {
  try {
    const resp = await fetch('/api/me');
    const data = await resp.json();
    currentUser = data.user;
    updateAuthUI();
  } catch (e) {}
}

function updateAuthUI() {
  const authArea = $('#auth-area');
  if (!authArea) return;

  if (currentUser) {
    let badges = `<span class="user-email">${escapeHtml(currentUser.email)}</span>`;
    if (currentUser.folds_remaining !== null && currentUser.folds_remaining !== undefined) {
      badges += ` <span class="remaining-badge">${currentUser.folds_remaining} folds kvar idag</span>`;
    }
    if (currentUser.is_subscriber) {
      badges += ` <span class="remaining-badge" style="background:var(--sage-light);color:var(--sage)">Pro</span>`;
    }
    authArea.innerHTML = `
      <div class="top-bar-left">
        <div class="user-badge">${badges}</div>
      </div>
      <button class="btn-sm ghost" onclick="openHistory()">Historik</button>
      <button class="btn-sm ghost" onclick="doLogout()">Logga ut</button>
    `;
  } else {
    authArea.innerHTML = `
      <div class="top-bar-left"></div>
      <button class="btn-sm" onclick="openModal('login')">Logga in</button>
      <button class="btn-sm primary" onclick="openModal('register')">Skapa konto</button>
    `;
  }
}

function openModal(type) {
  $$('.modal-overlay').forEach(m => m.classList.remove('visible'));
  const modal = $(`#${type}-modal`);
  if (modal) modal.classList.add('visible');
}
function closeModal(type) {
  const modal = $(`#${type}-modal`);
  if (modal) modal.classList.remove('visible');
}

async function doRegister(e) {
  e.preventDefault();
  const email = $('#reg-email').value.trim();
  const password = $('#reg-password').value;
  const name = $('#reg-name').value.trim();
  const errEl = $('#reg-error');
  errEl.textContent = '';

  try {
    const resp = await fetch('/api/register', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password, name }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error);
    currentUser = data.user;
    updateAuthUI();
    closeModal('register');
  } catch (err) {
    errEl.textContent = err.message;
  }
}

async function doLogin(e) {
  e.preventDefault();
  const email = $('#login-email').value.trim();
  const password = $('#login-password').value;
  const errEl = $('#login-error');
  errEl.textContent = '';

  try {
    const resp = await fetch('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error);
    currentUser = data.user;
    updateAuthUI();
    closeModal('login');
  } catch (err) {
    errEl.textContent = err.message;
  }
}

async function doLogout() {
  await fetch('/api/logout', { method: 'POST' });
  currentUser = null;
  updateAuthUI();
}


// ===== History =====

async function openHistory() {
  const panel = $('#history-panel');
  const list = $('#history-list');
  panel.classList.add('visible');
  list.innerHTML = '<div class="history-empty">Laddar...</div>';

  try {
    const resp = await fetch('/api/history');
    const data = await resp.json();
    if (!data.folds || data.folds.length === 0) {
      list.innerHTML = '<div class="history-empty">Inga sparade folds ännu.</div>';
      return;
    }
    list.innerHTML = data.folds.map(f => `
      <div class="history-item" onclick="loadFold(${f.id})">
        <div class="hi-title">${escapeHtml(f.video_title || 'Utan titel')}</div>
        <div class="hi-meta">
          <span>${f.created_at ? f.created_at.substring(0, 16).replace('T', ' ') : ''}</span>
          <span>${f.cost_sek ? f.cost_sek.toFixed(2) + ' kr' : ''}</span>
        </div>
      </div>
    `).join('');
  } catch (e) {
    list.innerHTML = '<div class="history-empty">Kunde inte ladda historik.</div>';
  }
}

function closeHistory() {
  $('#history-panel').classList.remove('visible');
}

async function loadFold(foldId) {
  try {
    const resp = await fetch(`/api/fold/${foldId}`);
    const fold = await resp.json();
    if (fold.error) return;

    videoMeta = { video_id: fold.video_id, video_title: fold.video_title, duration: 0 };
    rawSegments = fold.segments_json ? JSON.parse(fold.segments_json) : [];
    renderResult(fold.summary_json, {
      input_tokens: fold.input_tokens,
      output_tokens: fold.output_tokens,
      cost_sek: fold.cost_sek,
      share_token: fold.share_token,
    });
    closeHistory();
  } catch (e) {}
}

function loadSharedFold(fold) {
  videoMeta = { video_id: fold.video_id, video_title: fold.video_title, duration: 0 };
  rawSegments = fold.segments_json ? JSON.parse(fold.segments_json) : [];
  renderResult(fold.summary_json, {
    input_tokens: fold.input_tokens,
    output_tokens: fold.output_tokens,
    cost_sek: fold.cost_sek,
    share_token: fold.share_token,
  });
}


// ===== Theme =====

function loadTheme() {
  const saved = localStorage.getItem('foldly-theme');
  if (saved === 'dark') document.body.classList.add('dark');
  updateThemeIcon();
}

function toggleTheme() {
  document.body.classList.toggle('dark');
  localStorage.setItem('foldly-theme', document.body.classList.contains('dark') ? 'dark' : 'light');
  updateThemeIcon();
}

function updateThemeIcon() {
  const btn = $('#theme-btn');
  if (btn) btn.textContent = document.body.classList.contains('dark') ? '☀️' : '🌙';
}


// ===== Origami crane animation =====

const CRANE_STAGES = 7;
function setCraneStage(progress) {
  const stage = Math.min(CRANE_STAGES, Math.max(1, Math.ceil((progress / 100) * CRANE_STAGES)));
  for (let i = 1; i <= CRANE_STAGES; i++) {
    const el = document.getElementById('crane-' + i);
    if (el) el.classList.toggle('active', i === stage);
  }
}


// ===== Loading state =====

function showLoading(msg, isError = false) {
  loadingEl.classList.add('visible');
  loadingEl.classList.toggle('error', isError);
  statusText.textContent = msg;
  statusText.classList.toggle('error-text', isError);
  if (isError) {
    etaText.textContent = '';
    progressTrack.style.display = 'none';
    $('#progress-steps').style.display = 'none';
    for (let i = 1; i <= CRANE_STAGES; i++) {
      const el = document.getElementById('crane-' + i);
      if (el) el.classList.remove('active');
    }
  } else {
    progressTrack.style.display = '';
    $('#progress-steps').style.display = '';
  }
}

function hideLoading() {
  loadingEl.classList.remove('visible', 'error');
  progressTrack.classList.remove('indeterminate');
  etaText.textContent = '';
  [stepTranscript, stepAnalyze, stepDone].forEach(s => s.classList.remove('active', 'done'));
}

function setStep(step) {
  if (step === 'transcript') {
    progressTrack.classList.add('indeterminate');
    progressFill.style.width = '0%';
    stepTranscript.classList.add('active');
    stepAnalyze.classList.remove('active', 'done');
    stepDone.classList.remove('active', 'done');
    setCraneStage(5);
  } else if (step === 'analyze') {
    progressTrack.classList.remove('indeterminate');
    progressFill.style.width = '15%';
    stepTranscript.classList.remove('active');
    stepTranscript.classList.add('done');
    stepAnalyze.classList.add('active');
    stepDone.classList.remove('active', 'done');
    streamStartTime = Date.now();
    setCraneStage(15);
  } else if (step === 'done') {
    progressTrack.classList.remove('indeterminate');
    progressFill.style.width = '100%';
    stepTranscript.classList.remove('active');
    stepTranscript.classList.add('done');
    stepAnalyze.classList.remove('active');
    stepAnalyze.classList.add('done');
    stepDone.classList.add('done');
    setCraneStage(100);
    etaText.textContent = '';
  }
}

function updateStreamProgress(charCount) {
  const durationMin = videoMeta.duration || 30;
  const estimatedChars = Math.max(4000, durationMin * 200);
  const progress = Math.min(95, 15 + (charCount / estimatedChars) * 80);
  progressFill.style.width = progress + '%';
  setCraneStage(progress);

  if (streamStartTime && charCount > 200) {
    const elapsed = (Date.now() - streamStartTime) / 1000;
    const rate = charCount / elapsed;
    const remaining = Math.max(0, estimatedChars - charCount);
    const etaSeconds = Math.round(remaining / rate);
    if (etaSeconds > 2) {
      const min = Math.floor(etaSeconds / 60);
      const sec = etaSeconds % 60;
      etaText.textContent = min > 0
        ? `Beräknad tid kvar: ~${min} min ${sec} s`
        : `Beräknad tid kvar: ~${sec} s`;
    } else {
      etaText.textContent = 'Nästan klar...';
    }
  }
}


// ===== Main submit =====

async function handleSubmit() {
  const url = urlInput.value.trim();
  if (!url) return;

  if (!currentUser) {
    openModal('login');
    return;
  }

  btn.disabled = true;
  resultEl.classList.remove('visible');
  playerContainer.classList.remove('visible');
  sectionsEl.innerHTML = '';
  rawSegments = [];
  summaryData = null;
  showLoading('Hämtar transkription från YouTube...');
  setStep('transcript');

  const language = $('#lang-select')?.value || 'svenska';
  const detailLevel = $('#detail-select')?.value || 'medium';

  try {
    const resp = await fetch('/api/summarize', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url, language, detail_level: detailLevel }),
    });

    if (!resp.ok) {
      const err = await resp.json();
      throw new Error(err.error || 'Något gick fel');
    }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let fullText = '';
    let doneInfo = null;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop();

      for (const line of lines) {
        if (!line.trim()) continue;
        try {
          const msg = JSON.parse(line);
          if (msg.type === 'meta') {
            videoMeta = msg;
            rawSegments = msg.segments || [];
            showLoading('Claude viker ihop transkriptionen...');
            setStep('analyze');
          } else if (msg.type === 'chunk') {
            fullText += msg.text;
            updateStreamProgress(fullText.length);
          } else if (msg.type === 'done') {
            doneInfo = msg;
            setStep('done');
            renderResult(fullText, doneInfo);
            fetchUser(); // Refresh remaining count
          } else if (msg.type === 'error') {
            throw new Error(msg.error);
          }
        } catch (e) {
          if (e instanceof SyntaxError) continue;
          throw e;
        }
      }
    }

    // Process remaining buffer
    if (buffer.trim()) {
      for (const part of buffer.split('\n')) {
        if (!part.trim()) continue;
        try {
          const msg = JSON.parse(part);
          if (msg.type === 'chunk') fullText += msg.text;
          if (msg.type === 'done') { doneInfo = msg; setStep('done'); renderResult(fullText, doneInfo); }
          if (msg.type === 'error') showLoading(msg.error, true);
        } catch (e) {}
      }
    }

    if (fullText && !resultEl.classList.contains('visible')) {
      renderResult(fullText, doneInfo);
    }

  } catch (err) {
    showLoading(err.message, true);
  } finally {
    btn.disabled = false;
  }
}


// ===== Time utilities =====

function parseTimeToSeconds(str) {
  if (!str) return 0;
  const parts = str.trim().split(':').map(Number);
  if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2];
  if (parts.length === 2) return parts[0] * 60 + parts[1];
  return parts[0] || 0;
}

function getOriginalTextForChapter(timeStr) {
  if (!timeStr || !rawSegments.length) return '';
  const parts = timeStr.split(/[–\-—]/);
  if (parts.length < 2) return '';
  const start = parseTimeToSeconds(parts[0]);
  const end = parseTimeToSeconds(parts[1]);
  return rawSegments
    .filter(s => s.start >= start && s.start < end)
    .map(s => s.text)
    .join(' ');
}


// ===== YouTube Player =====

function showPlayer(videoId, startSeconds) {
  playerContainer.classList.add('visible');
  const iframe = playerContainer.querySelector('iframe');
  const src = `https://www.youtube-nocookie.com/embed/${videoId}?start=${Math.floor(startSeconds || 0)}&autoplay=1`;
  iframe.src = src;
  playerContainer.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function seekTo(timeStr) {
  const seconds = parseTimeToSeconds(timeStr);
  if (videoMeta.video_id) {
    showPlayer(videoMeta.video_id, seconds);
  }
}


// ===== Render result =====

function renderResult(jsonText, doneInfo) {
  try {
    let cleaned = jsonText.trim();
    if (cleaned.startsWith('```')) {
      cleaned = cleaned.replace(/^```(?:json)?\s*/, '').replace(/```\s*$/, '');
    }
    const data = JSON.parse(cleaned);
    summaryData = data;

    const ytUrl = `https://www.youtube.com/watch?v=${videoMeta.video_id}`;
    resultHeader.innerHTML = `
      <h2>${escapeHtml(data.title || videoMeta.video_title)}</h2>
      <p class="subtitle">${escapeHtml(videoMeta.video_title)} · <a href="${ytUrl}" target="_blank">Se på YouTube</a></p>
    `;

    // Meta bar (cost, tokens, share)
    let metaHtml = '';
    if (doneInfo) {
      if (doneInfo.cost_sek !== undefined) {
        metaHtml += `<span class="meta-chip cost">Kostnad: ${doneInfo.cost_sek.toFixed(2)} kr</span>`;
      }
      if (doneInfo.input_tokens) {
        const totalTok = ((doneInfo.input_tokens + doneInfo.output_tokens) / 1000).toFixed(1);
        metaHtml += `<span class="meta-chip tokens">${totalTok}k tokens</span>`;
      }
      if (doneInfo.share_token) {
        const shareUrl = `${window.location.origin}/fold/${doneInfo.share_token}`;
        metaHtml += `<span class="meta-chip share" onclick="copyShareLink('${shareUrl}')" title="Kopiera delningslänk">Dela ↗</span>`;
      }
    }
    resultMetaBar.innerHTML = metaHtml;

    overallSummaryEl.textContent = data.summary;

    // Key quotes
    if (data.key_quotes && data.key_quotes.length > 0) {
      keyQuotesEl.innerHTML = `
        <div class="key-quotes-title">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 21c3 0 7-1 7-8V5c0-1.25-.756-2.017-2-2H4c-1.25 0-2 .75-2 1.972V11c0 1.25.75 2 2 2 1 0 1 0 1 1v1c0 1-1 2-2 2s-1 .008-1 1.031V21z"/><path d="M15 21c3 0 7-1 7-8V5c0-1.25-.756-2.017-2-2h-4c-1.25 0-2 .75-2 1.972V11c0 1.25.75 2 2 2 1 0 1 0 1 1v1c0 1-1 2-2 2s-1 .008-1 1.031V21z"/></svg>
          Nyckelcitat
        </div>
        ${data.key_quotes.map(q => `
          <div class="quote-card">
            <div class="quote-text">"${escapeHtml(q.text)}"</div>
            <div class="quote-meta">
              ${escapeHtml(q.context || '')}
              ${q.time ? ` · <span class="quote-time" onclick="seekTo('${escapeHtml(q.time)}')">${escapeHtml(q.time)}</span>` : ''}
            </div>
          </div>
        `).join('')}
      `;
      keyQuotesEl.style.display = 'block';
    } else {
      keyQuotesEl.style.display = 'none';
    }

    // Sections
    sectionsEl.innerHTML = '';
    allOpen = false;

    data.chapters.forEach((ch, idx) => {
      const transcriptHtml = ch.transcript_html || '';
      const originalText = getOriginalTextForChapter(ch.time || ch.timestamp || '');
      const timeStr = ch.time || ch.timestamp || '';

      const section = document.createElement('div');
      section.className = 'section';
      section.dataset.idx = idx;

      let originalBlock = '';
      if (originalText) {
        originalBlock = `
          <button class="original-toggle" onclick="toggleOriginal(event, ${idx})">
            <span class="toggle-arrow">▶</span> Visa originaltext
          </button>
          <div class="original-text" id="original-${idx}">${escapeHtml(originalText)}</div>
        `;
      }

      section.innerHTML = `
        <div class="section-header" onclick="toggleSection(this)">
          <svg class="arrow" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
            <path d="M9 6l6 6-6 6"/>
          </svg>
          <div class="section-info">
            <div class="section-title">${escapeHtml(ch.title)}</div>
            <div class="section-summary">${escapeHtml(ch.summary)}</div>
            <div class="section-meta">
              ${timeStr ? `<span class="timestamp" onclick="event.stopPropagation(); seekTo('${escapeHtml(timeStr.split(/[–—-]/)[0])}')">${escapeHtml(timeStr)}</span>` : ''}
            </div>
          </div>
        </div>
        <div class="section-body">
          <div class="transcript-text">${transcriptHtml}</div>
          ${originalBlock}
        </div>
      `;
      sectionsEl.appendChild(section);
    });

    resultEl.classList.add('visible');
    hideLoading();
    resultEl.scrollIntoView({ behavior: 'smooth', block: 'start' });

  } catch (e) {
    console.error('Parse error:', e, jsonText.substring(0, 500));
    showLoading('Kunde inte tolka svaret. Försök igen.', true);
  }
}


// ===== Section interactions =====

function toggleSection(header) {
  const section = header.parentElement;
  section.classList.toggle('open');
  // Focus for keyboard nav
  section.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function toggleOriginal(event, idx) {
  event.stopPropagation();
  const textEl = document.getElementById('original-' + idx);
  const isOpen = textEl.classList.toggle('visible');
  const btn = event.currentTarget;
  btn.classList.toggle('open', isOpen);
  const label = isOpen ? 'Dölj originaltext' : 'Visa originaltext';
  btn.innerHTML = `<span class="toggle-arrow">${isOpen ? '▼' : '▶'}</span> ${label}`;
}

function toggleAll() {
  allOpen = !allOpen;
  $$('.section').forEach(s => s.classList.toggle('open', allOpen));
}


// ===== Search =====

function handleSearch() {
  const query = searchInput.value.trim().toLowerCase();
  const sections = $$('.section');

  if (!query) {
    sections.forEach(s => {
      s.classList.remove('search-highlight');
      s.style.display = '';
    });
    // Remove highlights
    $$('.transcript-text').forEach(el => {
      el.innerHTML = el.innerHTML.replace(/<mark>(.*?)<\/mark>/gi, '$1');
    });
    searchCount.textContent = '';
    return;
  }

  let matchCount = 0;
  sections.forEach(s => {
    const text = s.textContent.toLowerCase();
    const matches = text.includes(query);
    s.style.display = matches ? '' : 'none';
    s.classList.toggle('search-highlight', matches);
    if (matches) matchCount++;
  });
  searchCount.textContent = `${matchCount} träff${matchCount !== 1 ? 'ar' : ''}`;
}


// ===== Keyboard navigation =====

function handleKeyboard(e) {
  // Don't capture if typing in input
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
  if (!resultEl.classList.contains('visible')) return;

  const sections = [...$$('.section')].filter(s => s.style.display !== 'none');
  const current = sections.findIndex(s => s.classList.contains('kb-focus'));

  if (e.key === 'j' || e.key === 'ArrowDown') {
    e.preventDefault();
    const next = current < sections.length - 1 ? current + 1 : 0;
    focusSection(sections, next);
  } else if (e.key === 'k' || e.key === 'ArrowUp') {
    e.preventDefault();
    const prev = current > 0 ? current - 1 : sections.length - 1;
    focusSection(sections, prev);
  } else if (e.key === 'Enter' || e.key === ' ') {
    if (current >= 0) {
      e.preventDefault();
      sections[current].classList.toggle('open');
    }
  } else if (e.key === '/' && !e.ctrlKey && !e.metaKey) {
    e.preventDefault();
    searchInput.focus();
  }
}

function focusSection(sections, idx) {
  sections.forEach(s => s.classList.remove('kb-focus'));
  if (sections[idx]) {
    sections[idx].classList.add('kb-focus');
    sections[idx].scrollIntoView({ behavior: 'smooth', block: 'center' });
    // Brief visual indicator
    sections[idx].style.outline = '2px solid var(--accent)';
    setTimeout(() => { sections[idx].style.outline = ''; }, 800);
  }
}


// ===== Copy & Export =====

function copyAll() {
  if (!summaryData) return;
  const md = generateMarkdown();
  navigator.clipboard.writeText(md).then(() => showToast('Kopierat till urklipp!'));
}

function exportMarkdown() {
  if (!summaryData) return;
  const md = generateMarkdown();
  const blob = new Blob([md], { type: 'text/markdown' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `${(summaryData.title || 'fold').replace(/[^a-zåäö0-9 ]/gi, '')}.md`;
  a.click();
  URL.revokeObjectURL(url);
  showToast('Markdown-fil laddas ner!');
}

function generateMarkdown() {
  if (!summaryData) return '';
  let md = `# ${summaryData.title}\n\n`;
  md += `${summaryData.summary}\n\n`;

  if (videoMeta.video_id) {
    md += `[Se på YouTube](https://www.youtube.com/watch?v=${videoMeta.video_id})\n\n---\n\n`;
  }

  if (summaryData.key_quotes && summaryData.key_quotes.length > 0) {
    md += `## Nyckelcitat\n\n`;
    summaryData.key_quotes.forEach(q => {
      md += `> "${q.text}"\n> — ${q.context || ''}${q.time ? ' (' + q.time + ')' : ''}\n\n`;
    });
    md += `---\n\n`;
  }

  summaryData.chapters.forEach(ch => {
    md += `## ${ch.title}\n`;
    md += `*${ch.time || ''}*\n\n`;
    md += `${ch.summary}\n\n`;
    // Strip HTML from transcript_html for markdown
    if (ch.transcript_html) {
      const tmp = document.createElement('div');
      tmp.innerHTML = ch.transcript_html;
      md += tmp.textContent + '\n\n';
    }
    md += `---\n\n`;
  });

  return md;
}

function copyShareLink(url) {
  navigator.clipboard.writeText(url).then(() => showToast('Delningslänk kopierad!'));
}


// ===== Toast =====

let toastTimeout;
function showToast(msg) {
  toastEl.textContent = msg;
  toastEl.classList.add('visible');
  clearTimeout(toastTimeout);
  toastTimeout = setTimeout(() => toastEl.classList.remove('visible'), 2500);
}


// ===== Utilities =====

function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

function debounce(fn, ms) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}
