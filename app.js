const $ = id => document.getElementById(id);
const KEY = 'vallia.chat.v1';
const ARC = 289.03;
const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
const LABELS = {
  idle: 'ociosa',
  thinking: 'consultando o modelo',
  speaking: 'escrevendo',
  error: 'falhou',
  stopped: 'interrompida',
};

let chats = [];
try { chats = JSON.parse(localStorage.getItem(KEY) || '[]'); } catch {}
if (!Array.isArray(chats)) chats = [];
let active = chats[0] || null;
let pending = null;
let ready = false;
let nCtx = 2048;
let maxTokens = 260;
let rate = 0;
let ticker;

function say(text = '') {
  $('error').textContent = text;
  $('error').hidden = !text;
}

function state(name) {
  document.body.dataset.state = name;
  $('pill-state').textContent = LABELS[name];
}

function persist() {
  try { localStorage.setItem(KEY, JSON.stringify(chats)); }
  catch { say('O navegador ficou sem espaço para guardar a conversa.'); }
}

function setArc(fraction) {
  $('arc').style.strokeDashoffset = ARC * (1 - Math.max(0, Math.min(1, fraction)));
}

function beat() {
  if (reduced || !$('beat').animate) return;
  $('beat').animate(
    [{ transform: 'scale(.7)', opacity: .55 }, { transform: 'scale(1.18)', opacity: 0 }],
    { duration: 620, easing: 'cubic-bezier(.2,.7,.3,1)' },
  );
}

/* ---------- texto seguro: parágrafos + blocos de código ---------- */
const FENCE = /```([\w+-]*)\r?\n([\s\S]*?)(?:```|$)/g;

function codeBlock(lang, source) {
  const box = document.createElement('div');
  box.className = 'code';
  const head = document.createElement('div');
  head.className = 'code-head';
  const tag = document.createElement('span');
  tag.textContent = lang || 'código';
  const copy = document.createElement('button');
  copy.type = 'button';
  copy.className = 'code-copy';
  copy.textContent = 'copiar';
  copy.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(source);
      copy.textContent = 'copiado';
    } catch {
      copy.textContent = 'não foi possível';
    }
    setTimeout(() => { copy.textContent = 'copiar'; }, 1600);
  });
  const pre = document.createElement('pre');
  pre.textContent = source.replace(/\n$/, '');
  head.append(tag, copy);
  box.append(head, pre);
  return box;
}

function block(text) {
  const p = document.createElement('p');
  p.textContent = text;
  return p;
}

function paint(target, text) {
  target.replaceChildren();
  let cursor = 0;
  for (const match of text.matchAll(FENCE)) {
    const before = text.slice(cursor, match.index).trim();
    if (before) target.append(block(before));
    target.append(codeBlock(match[1], match[2]));
    cursor = match.index + match[0].length;
  }
  const tail = text.slice(cursor).trim();
  target.append(block(tail));
}

/* ---------- mensagens ---------- */
function statsLine({ ms, gen, tokens }) {
  const box = document.createElement('div');
  box.className = 'meta';
  const values = [
    ['tempo', `${(ms / 1000).toFixed(1)} s`],
    ['≈ tokens', String(tokens)],
    ['tok/s', gen > 0 ? (tokens / (gen / 1000)).toFixed(1) : '0'],
  ];
  for (const [key, value] of values) {
    const item = document.createElement('span');
    item.append(document.createTextNode(`${key} `));
    const b = document.createElement('b');
    b.textContent = value;
    item.append(b);
    box.append(item);
  }
  return box;
}

function node(turn) {
  const li = document.createElement('li');
  li.className = `msg ${turn.role}`;
  const head = document.createElement('div');
  head.className = 'msg-head';
  const who = document.createElement('em');
  who.textContent = turn.role === 'user' ? 'você' : 'vallia';
  const line = document.createElement('span');
  line.className = 'line';
  head.append(who, line);
  const body = document.createElement('div');
  body.className = 'body';
  paint(body, turn.content);
  li.append(head, body);
  if (turn.role === 'assistant' && turn.stats) li.append(statsLine(turn.stats));
  return li;
}

function toBottom(force) {
  const box = $('scroll');
  if (force || box.scrollHeight - box.scrollTop - box.clientHeight < 160) {
    box.scrollTop = box.scrollHeight;
  }
}

function list() {
  const box = $('chats');
  box.replaceChildren();
  if (!chats.length) {
    const p = document.createElement('p');
    p.textContent = 'Nada por aqui ainda — as conversas ficam neste navegador.';
    box.append(p);
  }
  for (const chat of chats) {
    const item = document.createElement('button');
    item.type = 'button';
    item.className = 'chat-item';
    item.textContent = chat.title;
    item.title = chat.title;
    item.disabled = !!pending;
    item.setAttribute('aria-current', String(chat === active));
    item.addEventListener('click', () => {
      active = chat;
      $('prompt').value = '';
      closeRail();
      say();
      draw();
    });
    box.append(item);
  }
}

function draw() {
  list();
  const turns = active?.turns || [];
  $('stage').dataset.mode = turns.length ? 'chat' : 'hero';
  $('log').replaceChildren(...turns.map(node));
  $('new').disabled = !!pending;
  $('send').hidden = !!pending;
  $('stop').hidden = !pending;
  count();
  meter();
  toBottom(!!turns.length);
}

/* ---------- janela de contexto ---------- */
function budgetChars() {
  return Math.max(900, Math.min(4300, (nCtx - maxTokens - 320) * 3));
}

function windowed(turns) {
  const keep = [turns.at(-1)];
  let left = budgetChars() - keep[0].content.length;
  for (let i = turns.length - 2; i > 0 && keep.length < 12; i -= 2) {
    const pair = [turns[i - 1], turns[i]];
    const size = pair[0].content.length + pair[1].content.length;
    if (pair[0].role !== 'user' || pair[1].role !== 'assistant' || size > left) break;
    keep.unshift(...pair);
    left -= size;
  }
  return keep;
}

/* ---------- stream ---------- */
async function stream(response, onToken) {
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = '';
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += value;
      const lines = buffer.split('\n');
      buffer = lines.pop();
      for (const line of lines) {
        if (!line.startsWith('data:')) continue;
        const data = line.slice(5).trim();
        if (data === '[DONE]') return;
        const event = JSON.parse(data);
        if (event.error) throw new Error('O modelo interrompeu a resposta.');
        const token = event.choices?.[0]?.delta?.content;
        if (typeof token === 'string' && token) onToken(token);
      }
    }
    throw new Error('A conexão com o modelo caiu no meio da resposta.');
  } finally {
    await reader.cancel().catch(() => {});
  }
}

/* ---------- enviar ---------- */
$('composer').addEventListener('submit', async event => {
  event.preventDefault();
  const prompt = $('prompt').value.trim();
  if (!ready || pending || !prompt) return;
  say();
  if (!active) {
    active = { title: prompt.replace(/\s+/g, ' ').slice(0, 58), turns: [] };
    chats.unshift(active);
  }
  const chat = active;
  chat.turns.push({ role: 'user', content: prompt });
  $('prompt').value = '';

  // renderiza o histórico e acrescenta o nó vivo por último — draw() não pode
  // rodar aqui, senão substitui o elemento que está recebendo tokens.
  list();
  $('stage').dataset.mode = 'chat';
  $('log').replaceChildren(...chat.turns.map(node));
  const li = document.createElement('li');
  li.className = 'msg assistant';
  li.dataset.live = '1';
  const head = document.createElement('div');
  head.className = 'msg-head';
  const who = document.createElement('em');
  who.textContent = 'vallia';
  const rule = document.createElement('span');
  rule.className = 'line';
  head.append(who, rule);
  const body = document.createElement('div');
  body.className = 'body';
  paint(body, 'aguardando o modelo…');
  li.append(head, body);
  $('log').append(li);
  toBottom(true);

  const controller = new AbortController();
  pending = controller;
  const answer = { role: 'assistant', content: '' };
  const started = performance.now();
  let spoke = false;
  let firstToken = 0;
  rate = 0;
  state('thinking');
  setArc(0);
  $('send').hidden = true;
  $('stop').hidden = false;
  $('new').disabled = true;
  $('activity').textContent = 'aguardando o primeiro token…';
  ticker = setInterval(() => {
    $('elapsed').textContent = `t+${((performance.now() - started) / 1000).toFixed(0)} s`;
  }, 250);

  let outcome = 'ok';
  try {
    const response = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ messages: windowed(chat.turns), max_tokens: maxTokens }),
      signal: controller.signal,
    });
    if (!response.ok) {
      const detail = await response.json().catch(() => ({}));
      throw new Error(detail.erro || 'A VALLIA não conseguiu responder.');
    }
    await stream(response, chunk => {
      if (!spoke) { spoke = true; firstToken = performance.now(); state('speaking'); }
      answer.content += chunk;
      paint(body, answer.content);
      const tokens = answer.content.length / 4;
      const genMs = performance.now() - firstToken;
      if (genMs > 400) {
        const instant = tokens / (genMs / 1000);
        rate = rate ? rate * 0.65 + instant * 0.35 : instant;
      }
      setArc(tokens / maxTokens);
      beat();
      $('t-rate').firstChild.nodeValue = rate >= 0.1 ? rate.toFixed(1) : '—';
      $('t-tokens').textContent = String(Math.round(tokens));
      const waiting = rate >= 0.5 ? (maxTokens - tokens) / rate : 0;
      $('activity').textContent = tokens >= 6 && waiting > 2 && waiting < 400
        ? `escrevendo · ≈ ${Math.round(waiting)} s restantes`
        : 'escrevendo…';
      toBottom();
    });
    if (!answer.content.trim()) throw new Error('O modelo devolveu uma resposta vazia.');
    const ended = performance.now();
    answer.stats = {
      ms: ended - started,
      tokens: Math.round(answer.content.length / 4),
      gen: ended - firstToken,
    };
    li.dataset.live = '0';
    li.append(statsLine(answer.stats));
    chat.turns.push(answer);
    if (rate >= 1) $('hero-eta').textContent = `≈ ${Math.max(4, Math.min(240, Math.round(maxTokens / rate + 6)))} s`;
    state('idle');
    $('activity').textContent = 'pronta';
  } catch (error) {
    li.remove();
    chat.turns.pop();
    if (!chat.turns.length) {
      chats = chats.filter(item => item !== chat);
      active = null;
    }
    if (!$('prompt').value) $('prompt').value = prompt;
    if (error.name === 'AbortError') {
      outcome = 'stopped';
      state('stopped');
      $('activity').textContent = 'resposta interrompida';
    } else {
      outcome = 'error';
      state('error');
      say(error.message || 'Não foi possível falar com o modelo.');
      setTimeout(() => { if (!pending) state('idle'); }, 1800);
    }
  } finally {
    clearInterval(ticker);
    pending = null;
    if (outcome === 'ok') $('elapsed').textContent = `${(answer.stats.ms / 1000).toFixed(1)} s`;
    else $('elapsed').textContent = '';
    setArc(0);
    persist();
    draw();
    $('prompt').focus();
  }
});

/* ---------- controles ---------- */
function closeRail() {
  $('rail').classList.remove('open');
  $('menu').setAttribute('aria-expanded', 'false');
}

$('new').addEventListener('click', () => {
  active = null;
  $('prompt').value = '';
  closeRail();
  say();
  draw();
  $('prompt').focus();
});
$('stop').addEventListener('click', () => pending?.abort());
$('menu').addEventListener('click', () => {
  $('menu').setAttribute('aria-expanded', String($('rail').classList.toggle('open')));
});
$('bottom').addEventListener('click', () => toBottom(true));
$('scroll').addEventListener('scroll', () => {
  const box = $('scroll');
  $('bottom').hidden = !pending || box.scrollHeight - box.scrollTop - box.clientHeight < 220;
});
$('prompt').addEventListener('keydown', event => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    $('composer').requestSubmit();
  }
  if (event.key === 'Escape') pending?.abort();
});
$('prompt').addEventListener('input', () => {
  const box = $('prompt');
  box.style.height = 'auto';
  box.style.height = `${Math.min(box.scrollHeight, 150)}px`;
  count();
});
for (const chip of document.querySelectorAll('[data-prompt]')) {
  chip.addEventListener('click', () => {
    $('prompt').value = chip.dataset.prompt;
    $('prompt').style.height = 'auto';
    $('prompt').style.height = `${Math.min($('prompt').scrollHeight, 150)}px`;
    count();
    $('prompt').focus();
  });
}
for (const button of document.querySelectorAll('.segment button')) {
  button.addEventListener('click', () => {
    for (const other of document.querySelectorAll('.segment button')) {
      other.setAttribute('aria-pressed', 'false');
    }
    button.setAttribute('aria-pressed', 'true');
    maxTokens = Number(button.dataset.tokens);
    meter();
  });
}

function count() {
  const size = $('prompt').value.length;
  $('count').textContent = String(size);
  $('count').parentElement.classList.toggle('over', size > 2800);
  $('send').disabled = !ready || !!pending || !size;
}

function meter() {
  const used = active ? active.turns.reduce((sum, turn) => sum + turn.content.length, 0) : 0;
  const share = Math.min(1, used / budgetChars());
  $('meter-fill').style.width = `${share * 100}%`;
  $('t-ctx').textContent = `janela ${nCtx}`;
  $('t-used').textContent = `${Math.round(share * 100)}%`;
}

/* ---------- motor ---------- */
async function check() {
  try {
    const response = await fetch('/api/status', { signal: AbortSignal.timeout(5000) });
    const data = await response.json();
    ready = response.ok && data.ready === true;
    if (data.n_ctx) nCtx = data.n_ctx;
    if (data.model) {
      $('t-model').textContent = data.model.replace('deepseek-', '');
      $('t-model').title = `${data.model}${data.ftype ? ` · ${data.ftype}` : ''}`;
    }
  } catch {
    ready = false;
  }
  $('railfoot').dataset.ready = ready ? '1' : '0';
  $('status').textContent = ready ? 'modelo pronto · neste pc' : 'modelo desligado';
  if (!pending) count();
  meter();
  setTimeout(check, ready ? 12000 : 5000);
}

state('idle');
draw();
check();
