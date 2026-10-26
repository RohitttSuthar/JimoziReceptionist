const $ = id => document.getElementById(id);
const SPEECH_LANG = 'en-IN';            // microphone language (try 'en-US', 'en-GB' or 'hi-IN')
const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;

let cfg = { product_id: 'jimozi', name: '', ttsLang: 'hi', ttsVoice: null, speechLang: 'hi-IN', browserTtsFastPath: true };
let selectedMode = 'consumer';
let recognition = null, listening = false, busy = false, callActive = false, callToken = 0;
let sessionId = null, currentAudio = null, finalText = '';
let state = 'connecting', audioCtx = null, micStream = null, micAn = null, ttsAn = null;
let timerId = null, startedAt = 0;

const LABELS = { connecting: 'Connecting…', listening: 'Listening', thinking: 'One moment', speaking: 'Speaking', error: 'Something went wrong' };
const sleep = ms => new Promise(r => setTimeout(r, ms));

/* ---------- UI helpers ---------- */
function setState(s){ state = s; $('status').textContent = LABELS[s] || ''; }
function tick(){ const s = Math.floor((Date.now() - startedAt) / 1000); $('timer').textContent = Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0'); }
function openOverlay(){ $('call').classList.add('open'); resize(); startLoop(); $('endBtn').focus(); }
function closeOverlay(){ $('call').classList.remove('open'); setTimeout(() => { if (!callActive) stopLoop(); }, 500); $('callBtn').focus(); }

/* ---------- Config (optional, falls back to defaults) ---------- */
fetch('/api/config').then(r => r.json()).then(c => {
  cfg.product_id = c.default_product_id || cfg.product_id;
  cfg.name = c.default_product_name || '';
  cfg.ttsLang = c.supertonic_language || cfg.ttsLang;
  cfg.ttsVoice = c.supertonic_voice || null;
  cfg.speechLang = c.browser_speech_language || cfg.speechLang;
  cfg.browserTtsFastPath = c.browser_tts_fast_path !== false;
  $('brand').textContent = cfg.name;
  document.title = (cfg.name ? cfg.name + ' ' : '') + 'Customer Support';
}).catch(() => {});

/* ---------- Call lifecycle ---------- */
async function startCall(){
  if (callActive) return;
  callActive = true; callToken++; busy = false; finalText = '';
  $('homeNote').textContent = ''; $('timer').textContent = '0:00';
  openOverlay(); setState('connecting');
  try { audioCtx = new (window.AudioContext || window.webkitAudioContext)(); audioCtx.resume && audioCtx.resume(); } catch(e){ audioCtx = null; }
  try {
    micStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    if (!callActive) { stopMic(); return; }
    if (audioCtx) { micAn = audioCtx.createAnalyser(); micAn.fftSize = 512; audioCtx.createMediaStreamSource(micStream).connect(micAn); }
  } catch(e){
    if (e && e.name === 'NotFoundError') { endCall('No microphone found.'); return; }
    if (e && (e.name === 'NotAllowedError' || e.name === 'SecurityError')) { endCall('Microphone access is needed to talk to support.'); return; }
  }
  if (!callActive) return;
  startRecognition();
}

function stopMic(){ if (micStream) { micStream.getTracks().forEach(t => t.stop()); micStream = null; } }

function endCall(note){
  callActive = false; callToken++; busy = false;
  try { if (recognition && listening) recognition.abort(); } catch(e){}
  if (currentAudio) { currentAudio.pause(); currentAudio = null; }
  if ('speechSynthesis' in window) window.speechSynthesis.cancel();
  stopMic();
  if (audioCtx) { audioCtx.close().catch(() => {}); audioCtx = null; }
  micAn = ttsAn = null;
  clearInterval(timerId); timerId = null;
  closeOverlay();
  $('homeNote').textContent = note || '';
}

/* ---------- Speech recognition ---------- */
function startRecognition(){
  if (!recognition || !callActive || listening || busy) return;
  try { recognition.lang = cfg.speechLang; recognition.start(); }
  catch(e){ setTimeout(() => { if (callActive && !listening && !busy) startRecognition(); }, 500); }
}

function setupRecognition(){
  if (!SR) { $('callBtn').disabled = true; $('homeNote').textContent = 'Voice calls work in Chrome or Edge.'; return; }
  recognition = new SR();
  recognition.continuous = false;
  recognition.interimResults = true;

  recognition.onstart = () => {
    listening = true; finalText = '';
    if (!callActive) return;
    setState('listening');
    if (!timerId) { startedAt = Date.now(); timerId = setInterval(tick, 1000); tick(); }
  };
  recognition.onresult = e => {
    for (let i = e.resultIndex; i < e.results.length; i++) {
      const t = e.results[i][0].transcript;
      if (e.results[i].isFinal) finalText += t;
    }
  };
  recognition.onerror = e => {
    if (e.error === 'not-allowed' || e.error === 'service-not-allowed') endCall('Microphone access is needed to talk to support.');
    else if (e.error === 'network') endCall('Speech service is unavailable. Check your connection and try again.');
    /* "no-speech" and "aborted" are normal: onend restarts listening */
  };
  recognition.onend = async () => {
    listening = false;
    const text = finalText.trim(); finalText = '';
    if (text && callActive) await handle(text);
    if (callActive) startRecognition();
  };
}

/* ---------- Ask support + speak the answer ---------- */
async function handle(text){
  const token = callToken;
  busy = true; setState('thinking');
  try {
    const r = await fetch('/api/chat', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, product_id: cfg.product_id, session_id: sessionId, include_audio: false, mode: selectedMode })
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || 'Request failed');
    if (token !== callToken) return;
    sessionId = d.session_id;
    setState('speaking');
    await speak(d.answer, token);
  } catch(e){
    if (token !== callToken) return;
    setState('error');
    await sleep(2500);
  } finally {
    if (token === callToken) busy = false;
  }
}

async function speakBrowserFast(text, token){
  if (!('speechSynthesis' in window) || !('SpeechSynthesisUtterance' in window)) return false;
  let voices = window.speechSynthesis.getVoices();
  if (!voices.length) {
    await new Promise(resolve => {
      let settled = false;
      const done = () => { if (!settled) { settled = true; window.speechSynthesis.removeEventListener('voiceschanged', done); resolve(); } };
      window.speechSynthesis.addEventListener('voiceschanged', done, { once: true });
      setTimeout(done, 180);
    });
    voices = window.speechSynthesis.getVoices();
  }
  const lang = (cfg.ttsLang || 'hi').toLowerCase().startsWith('hi') ? 'hi' : (cfg.ttsLang || 'hi').toLowerCase();
  const voice = voices.find(v => (v.lang || '').toLowerCase().startsWith(lang));
  if (!voice || token !== callToken) return false;
  return await new Promise(resolve => {
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.voice = voice;
    utterance.lang = voice.lang || 'hi-IN';
    utterance.rate = 1.04;
    utterance.pitch = 1;
    utterance.onend = () => resolve(true);
    utterance.onerror = () => resolve(false);
    window.speechSynthesis.cancel();
    window.speechSynthesis.speak(utterance);
  });
}

async function speak(text, token){
  if (!text) return;
  // Browser-native Hindi speech starts sooner than a second HTTP request plus local TTS
  // synthesis. If a Hindi voice is unavailable, preserve the original Supertonic path.
  if (cfg.browserTtsFastPath && await speakBrowserFast(text, token)) return;
  try {
    if (currentAudio) { currentAudio.pause(); currentAudio = null; }
    const r = await fetch('/api/speak', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, lang: cfg.ttsLang, voice: cfg.ttsVoice })
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || 'TTS failed');
    if (token !== callToken) return;
    const bytes = Uint8Array.from(atob(d.audio_base64), c => c.charCodeAt(0));
    const url = URL.createObjectURL(new Blob([bytes], { type: 'audio/wav' }));
    const audio = new Audio(url);
    currentAudio = audio;
    if (audioCtx && audioCtx.state === 'running') {       // lets the orb react to the real voice
      try { const src = audioCtx.createMediaElementSource(audio); ttsAn = audioCtx.createAnalyser(); ttsAn.fftSize = 512; src.connect(ttsAn); ttsAn.connect(audioCtx.destination); }
      catch(e){ ttsAn = null; }
    }
    const finished = new Promise(res => {
      ['ended', 'error', 'pause'].forEach(ev => audio.addEventListener(ev, res, { once: true }));
    });
    audio.onended = () => { URL.revokeObjectURL(url); if (currentAudio === audio) currentAudio = null; };
    await audio.play();
    await finished;
  } catch(e){
    await sleep(800);
  } finally {
    ttsAn = null;
  }
}

/* ---------- Orb animation (tuned for 60 FPS) ----------
   - no trig per point: ring phases are expanded with angle-addition identities over pre-computed tables
   - glow and core are pre-rendered once and only scaled/faded each frame
   - quality steps down automatically if the device can't hold ~60 FPS */
const cv = $('orb'), g2 = cv.getContext('2d', { desynchronized: true });
const PARAMS = { connecting: { sp: .5, am: .05 }, listening: { sp: .8, am: .06 }, thinking: { sp: 1.8, am: .09 }, speaking: { sp: 1.1, am: .07 }, error: { sp: .4, am: .04 } };
let cur = { sp: .5, am: .05 }, t = 0, lastTs = 0, lvl = 0, raf = 0;
let quality = 1, ema = 1 / 60, cooldown = 0, N = 60, M = 120;
let TB = null, glowCv = null, coreCv = null;

function buildTables(){
  N = Math.max(26, Math.round(60 * quality)); M = Math.max(56, Math.round(120 * quality));
  const n = M + 1; TB = {};
  for (const k of ['c1','s1','c2','s2','c3','s3','c5','s5']) TB[k] = new Float32Array(n);
  for (let j = 0; j < n; j++) {
    const th = j / M * Math.PI * 2;
    TB.c1[j] = Math.cos(th);     TB.s1[j] = Math.sin(th);
    TB.c2[j] = Math.cos(2 * th); TB.s2[j] = Math.sin(2 * th);
    TB.c3[j] = Math.cos(3 * th); TB.s3[j] = Math.sin(3 * th);
    TB.c5[j] = Math.cos(5 * th); TB.s5[j] = Math.sin(5 * th);
  }
}
function radial(size, radius, stops){
  const o = document.createElement('canvas'); o.width = o.height = size;
  const x = o.getContext('2d'), h = size / 2, g = x.createRadialGradient(h, h, 0, h, h, radius);
  stops.forEach(s => g.addColorStop(s[0], s[1])); x.fillStyle = g; x.fillRect(0, 0, size, size); return o;
}
function resize(){
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const size = Math.floor(Math.max(180, Math.min(innerWidth * .88, innerHeight * .42, 440)));
  cv.style.width = cv.style.height = size + 'px';
  cv.width = cv.height = Math.floor(size * dpr);
  const W = cv.width, R = W * .27;
  glowCv = radial(W, R * 2.1, [[0, 'rgba(252,128,25,1)'], [.5, 'rgba(252,128,25,.26)'], [1, 'rgba(252,128,25,0)']]);
  coreCv = radial(W, R * .6, [[0, 'rgba(255,255,255,1)'], [.4, 'rgba(255,176,112,.5)'], [1, 'rgba(252,128,25,0)']]);
  if (!TB) buildTables();
}
function readLevel(an){
  if (!an) return 0;
  const b = an._b || (an._b = new Uint8Array(an.fftSize));
  an.getByteTimeDomainData(b);
  let s = 0; for (let i = 0; i < b.length; i++) { const x = (b[i] - 128) / 128; s += x * x; }
  return Math.min(1, Math.sqrt(s / b.length) * 4);
}
function frame(now){
  raf = requestAnimationFrame(frame);
  const raw = (now - lastTs) / 1000; lastTs = now;
  const dt = Math.min(raw, .05);
  // adaptive quality: only steps down, only after a sustained slow stretch
  if (cooldown > 0) cooldown--;
  else {
    ema += (raw - ema) * .06;
    if (ema > .0215 && quality > .46) { quality *= .82; buildTables(); ema = 1 / 60; cooldown = 90; }
  }
  const p = PARAMS[state] || PARAMS.connecting, k = reduce ? .4 : 1;
  cur.sp += (p.sp - cur.sp) * .05; cur.am += (p.am - cur.am) * .05;
  t += dt * cur.sp * k;
  let target = .08;
  if (state === 'listening') target = micAn ? readLevel(micAn) : .12 + .06 * Math.sin(now / 400);
  else if (state === 'speaking') target = ttsAn ? readLevel(ttsAn) : .3 + .25 * Math.abs(Math.sin(now / 170) * Math.sin(now / 530));
  else if (state === 'thinking') target = .18;
  lvl += (target - lvl) * .2;
  draw(k);
}
function draw(k){
  const W = cv.width, c = W / 2, R = W * .27;
  g2.setTransform(1, 0, 0, 1, 0, 0);
  g2.globalCompositeOperation = 'source-over';
  g2.clearRect(0, 0, W, W);
  g2.globalCompositeOperation = 'lighter';

  const pulse = 1 + lvl * .12 + (state === 'thinking' ? .03 * Math.sin(t * 3) : 0);

  // glow (pre-rendered, scaled + faded)
  let s = W * pulse;
  g2.globalAlpha = Math.min(1, .30 + lvl * .35);
  g2.drawImage(glowCv, c - s / 2, c - s / 2, s, s);

  // threads
  const amp = cur.am + lvl * .32 * k, k5 = .25 * (.4 + lvl * 1.6);
  const { c1, s1, c2, s2, c3, s3, c5, s5 } = TB;
  g2.lineWidth = Math.max(.8, W / 480);
  for (let i = 0; i < N; i++) {
    const f = i / (N - 1), edge = 1 - Math.abs(2 * f - 1);
    const rot = i * .05 + t * .12, cr = Math.cos(rot), sr = Math.sin(rot);
    const p1 = t * .9 + i * .11,  cp1 = Math.cos(p1), sp1 = Math.sin(p1);
    const p2 = -t * 1.2 + i * .17, cp2 = Math.cos(p2), sp2 = Math.sin(p2);
    const p3 = t * 1.7 - i * .23,  cp3 = Math.cos(p3), sp3 = Math.sin(p3);
    const base = R * pulse * (.74 + .4 * f);
    g2.globalAlpha = .09 + .2 * edge + lvl * .1;
    g2.strokeStyle = 'rgb(255,' + ((120 + f * 100) | 0) + ',' + ((30 + f * 130) | 0) + ')';
    g2.beginPath();
    for (let j = 0; j <= M; j++) {
      const w = .5 * (s2[j] * cp1 + c2[j] * sp1) + .35 * (s3[j] * cp2 + c3[j] * sp2) + k5 * (s5[j] * cp3 + c5[j] * sp3);
      const r = base * (1 + amp * w);
      const x = c + (c1[j] * cr - s1[j] * sr) * r, y = c + (s1[j] * cr + c1[j] * sr) * r * .98;
      j ? g2.lineTo(x, y) : g2.moveTo(x, y);
    }
    g2.closePath(); g2.stroke();
  }

  // core (pre-rendered, scaled + faded)
  s = W * (1 + lvl * .4);
  g2.globalAlpha = Math.min(1, .2 + lvl * .25);
  g2.drawImage(coreCv, c - s / 2, c - s / 2, s, s);
  g2.globalAlpha = 1;
}
function startLoop(){ if (!raf) { lastTs = performance.now(); ema = 1 / 60; cooldown = 30; raf = requestAnimationFrame(frame); } }
function stopLoop(){ cancelAnimationFrame(raf); raf = 0; }

/* ---------- Wire up ---------- */
function setMode(mode){
  if (callActive) return;
  selectedMode = mode === 'chef' ? 'chef' : 'consumer';
  // Do not carry a previous mode's conversation into the newly selected mode.
  sessionId = null;
  $('consumerMode').setAttribute('aria-pressed', String(selectedMode === 'consumer'));
  $('chefMode').setAttribute('aria-pressed', String(selectedMode === 'chef'));
  $('who').textContent = selectedMode === 'chef' ? 'Chef Support' : 'Consumer Support';
  $('modeHint').textContent = selectedMode === 'chef'
    ? 'Chef questions: use chef-approved knowledge configured for this product'
    : 'Customer questions: menu, pricing, orders and delivery';
}
$('consumerMode').onclick = () => setMode('consumer');
$('chefMode').onclick = () => setMode('chef');
$('callBtn').onclick = startCall;
$('endBtn').onclick = () => endCall();
addEventListener('keydown', e => { if (e.key === 'Escape' && callActive) endCall(); });
addEventListener('resize', () => { if (callActive) resize(); });
setupRecognition();