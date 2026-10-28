const $ = id => document.getElementById(id);
const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;

let cfg = { product_id: 'jimozi', name: '', ttsLang: 'hi', ttsVoice: null, speechLang: 'hi-IN', browserTtsFastPath: true };
let selectedMode = 'consumer';
let listening = false, busy = false, transcribing = false, callActive = false, callToken = 0;
let sessionId = null, currentAudio = null, chatOpen = false, chatBusy = false;
let state = 'connecting', audioCtx = null, micStream = null, micAn = null, ttsAn = null;
let micSource = null, micProcessor = null, silentGain = null;
let timerId = null, startedAt = 0, speechFrames = [], preRollFrames = [], speechStartedAt = 0, lastVoiceAt = 0, speechFrameCount = 0;
let speechFinalizeTimer = null;

const LABELS = { connecting: 'Connecting…', listening: 'Listening', thinking: 'One moment', speaking: 'Speaking', error: 'Something went wrong' };
const sleep = ms => new Promise(r => setTimeout(r, ms));

/* ---------- UI helpers ---------- */
function setState(s){ state = s; $('status').textContent = LABELS[s] || ''; }
function tick(){ const s = Math.floor((Date.now() - startedAt) / 1000); $('timer').textContent = Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0'); }
function openOverlay(){ $('call').classList.add('open'); resize(); startLoop(); $('endBtn').focus(); }
function closeOverlay(){ $('call').classList.remove('open'); setTimeout(() => { if (!callActive) stopLoop(); }, 500); if (chatOpen) $('chatInput').focus(); else $('callBtn').focus(); }

/* ---------- Config (optional, falls back to defaults) ---------- */
fetch('/api/config').then(r => r.json()).then(c => {
  cfg.product_id = c.default_product_id || cfg.product_id;
  cfg.name = c.default_product_name || '';
  cfg.ttsLang = c.supertonic_language || cfg.ttsLang;
  cfg.ttsVoice = c.supertonic_voice || null;
  cfg.browserTtsFastPath = c.browser_tts_fast_path !== false;
  $('brand').textContent = cfg.name;
  document.title = (cfg.name ? cfg.name + ' ' : '') + 'Customer Support';
}).catch(() => {});


/* ---------- Chat UI: uses the same backend and session as voice ---------- */
function openChat(){
  if (callActive) return;
  chatOpen = true;
  $('chatPanel').classList.add('open');
  $('chatPanel').setAttribute('aria-hidden','false');
  $('chatTitle').textContent = selectedMode === 'chef' ? 'Chef Support' : 'Consumer Support';
  $('chatBrand').textContent = cfg.name || '';
  $('chatWelcome').textContent = selectedMode === 'chef'
    ? 'नमस्ते! Chef सहायता के लिए अपना सवाल लिखें।'
    : 'नमस्ते! Jimozi सहायता के लिए अपना सवाल लिखें।';
  $('chatInput').focus();
}
function closeChat(){
  if (callActive) return;
  chatOpen = false;
  $('chatPanel').classList.remove('open');
  $('chatPanel').setAttribute('aria-hidden','true');
  $('openChatBtn').focus();
}
function addChatMessage(role, text, extraClass=''){
  const bubble = document.createElement('div');
  bubble.className = 'chat-message ' + (role === 'user' ? 'from-user' : 'from-assistant') + (extraClass ? ' ' + extraClass : '');
  bubble.textContent = text;
  $('chatMessages').appendChild(bubble);
  $('chatMessages').scrollTop = $('chatMessages').scrollHeight;
  return bubble;
}
function clearChatMessages(){
  $('chatMessages').querySelectorAll('.chat-message').forEach(node => node.remove());
}
function autoResizeChatInput(){
  const input = $('chatInput');
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, 140) + 'px';
}
async function sendChatMessage(text){
  const clean = text.trim();
  if (!clean || chatBusy) return;
  chatBusy = true;
  $('chatSendBtn').disabled = true;
  $('chatVoiceBtn').disabled = true;
  $('chatInput').disabled = true;
  $('chatStatus').textContent = 'जवाब तैयार किया जा रहा है…';
  addChatMessage('user', clean);
  const pending = addChatMessage('assistant', 'एक क्षण…', 'pending');
  try {
    const r = await fetch('/api/chat', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({text:clean, product_id:cfg.product_id, session_id:sessionId, include_audio:false, mode:selectedMode})
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || 'Request failed');
    sessionId = data.session_id || sessionId;
    pending.textContent = data.answer || 'माफ़ कीजिए, अभी जवाब उपलब्ध नहीं है।';
    pending.classList.remove('pending');
    $('chatStatus').textContent = 'आप चैट और वॉइस कॉल के बीच स्विच कर सकते हैं।';
  } catch(error) {
    pending.textContent = 'कनेक्शन में समस्या हुई। कृपया दोबारा कोशिश करें।';
    pending.classList.remove('pending');
    $('chatStatus').textContent = error.message || 'संदेश भेजा नहीं जा सका।';
  } finally {
    chatBusy = false;
    $('chatSendBtn').disabled = false;
    $('chatVoiceBtn').disabled = false;
    $('chatInput').disabled = false;
    autoResizeChatInput();
    $('chatInput').focus();
  }
}

/* ---------- Call lifecycle ---------- */
const IS_MOBILE = /Android|iPhone|iPad|iPod/i.test(navigator.userAgent);

async function startCall(){
  if (callActive) return;
  if (chatOpen) { chatOpen = false; $('chatPanel').classList.remove('open'); $('chatPanel').setAttribute('aria-hidden','true'); }
  callActive = true; callToken++; busy = false; transcribing = false; resetSpeechBuffer();
  $('homeNote').textContent = ''; $('timer').textContent = '0:00';
  openOverlay(); setState('connecting');
  try { audioCtx = new (window.AudioContext || window.webkitAudioContext)(); audioCtx.resume && audioCtx.resume(); } catch(e){ audioCtx = null; }
  try {
    micStream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
    if (!callActive) { stopMic(); return; }
    if (audioCtx) {
      micAn = audioCtx.createAnalyser(); micAn.fftSize = 512;
      micSource = audioCtx.createMediaStreamSource(micStream);
      micSource.connect(micAn);
      startShrutiCapture(micSource, audioCtx);
    } else {
      endCall('Audio capture is not supported by this browser.'); return;
    }
  } catch(e){
    console.error('[JIMOZI] microphone error:', e);
    if (e && e.name === 'NotFoundError') { endCall('No microphone found.'); return; }
    if (e && (e.name === 'NotAllowedError' || e.name === 'SecurityError')) { endCall('Microphone access is needed to talk to support.'); return; }
    endCall('Could not access the microphone: ' + ((e && e.message) || 'unknown error')); return;
  }
  if (!callActive) return;
  listening = true; setState('listening');
  if (!timerId) { startedAt = Date.now(); timerId = setInterval(tick, 1000); tick(); }
}

function stopMic(){
  clearTimeout(speechFinalizeTimer); speechFinalizeTimer = null;
  try { if (micProcessor) micProcessor.disconnect(); } catch(e) {}
  try { if (silentGain) silentGain.disconnect(); } catch(e) {}
  try { if (micSource) micSource.disconnect(); } catch(e) {}
  micProcessor = silentGain = micSource = null;
  if (micStream) { micStream.getTracks().forEach(t => t.stop()); micStream = null; }
  micAn = null; listening = false; resetSpeechBuffer();
}

function endCall(note){
  callActive = false; callToken++; busy = false; transcribing = false;
  clearTimeout(speechFinalizeTimer); speechFinalizeTimer = null;
  if (currentAudio) { currentAudio.pause(); currentAudio = null; }
  if ('speechSynthesis' in window) window.speechSynthesis.cancel();
  stopMic();
  if (audioCtx) { audioCtx.close().catch(() => {}); audioCtx = null; }
  micAn = ttsAn = null;
  clearInterval(timerId); timerId = null;
  closeOverlay();
  $('homeNote').textContent = note || '';
}

/* ---------- Local Shruti STT audio capture ---------- */
// Capture microphone PCM directly. This avoids browser SpeechRecognition, which can
// produce system beeps and relies on a browser/vendor recognition service.
function resetSpeechBuffer(){ speechFrames = []; preRollFrames = []; speechStartedAt = 0; lastVoiceAt = 0; speechFrameCount = 0; }

function startShrutiCapture(source, ctx){
  if (!ctx.createScriptProcessor) { endCall('This browser does not support the audio capture required for voice support.'); return; }
  const blockSize = 4096;
  micProcessor = ctx.createScriptProcessor(blockSize, 1, 1);
  silentGain = ctx.createGain(); silentGain.gain.value = 0;
  micProcessor.onaudioprocess = event => {
    if (!callActive || busy || transcribing) { resetSpeechBuffer(); return; }
    const input = event.inputBuffer.getChannelData(0);
    const frame = new Float32Array(input);
    let energy = 0;
    for (let i = 0; i < frame.length; i++) energy += frame[i] * frame[i];
    const rms = Math.sqrt(energy / frame.length);
    const now = performance.now();
    if (!speechStartedAt) {
      preRollFrames.push(frame);
      if (preRollFrames.length > Math.ceil(ctx.sampleRate * 0.22 / frame.length)) preRollFrames.shift();
      if (rms >= 0.012) {
        speechStartedAt = now; lastVoiceAt = now;
        speechFrames = preRollFrames.slice();
        speechFrameCount = speechFrames.reduce((n, f) => n + f.length, 0);
        preRollFrames = [];
        setState('listening');
      }
      return;
    }
    speechFrames.push(frame); speechFrameCount += frame.length;
    if (rms >= 0.010) lastVoiceAt = now;
    // Auto-finalize after 850 ms of silence or a 15-second utterance cap.
    if ((now - lastVoiceAt >= 650 && now - speechStartedAt >= 300) || now - speechStartedAt >= 15000) {
      const frames = speechFrames; const count = speechFrameCount;
      resetSpeechBuffer();
      if (count >= ctx.sampleRate * 0.30) void transcribeFrames(frames, ctx.sampleRate);
    }
  };
  source.connect(micProcessor); micProcessor.connect(silentGain); silentGain.connect(ctx.destination);
}

function encodeWav16k(frames, sourceRate){
  const length = frames.reduce((n, f) => n + f.length, 0);
  const input = new Float32Array(length); let offset = 0;
  for (const frame of frames) { input.set(frame, offset); offset += frame.length; }
  const ratio = sourceRate / 16000;
  const outLength = Math.max(1, Math.floor(input.length / ratio));
  const pcm = new DataView(new ArrayBuffer(44 + outLength * 2));
  const write = (o, v) => { for (let i=0;i<v.length;i++) pcm.setUint8(o+i, v.charCodeAt(i)); };
  write(0,'RIFF'); pcm.setUint32(4,36+outLength*2,true); write(8,'WAVE'); write(12,'fmt ');
  pcm.setUint32(16,16,true); pcm.setUint16(20,1,true); pcm.setUint16(22,1,true);
  pcm.setUint32(24,16000,true); pcm.setUint32(28,32000,true); pcm.setUint16(32,2,true); pcm.setUint16(34,16,true);
  write(36,'data'); pcm.setUint32(40,outLength*2,true);
  for(let i=0;i<outLength;i++){
    const start=Math.floor(i*ratio), end=Math.min(input.length,Math.max(start+1,Math.floor((i+1)*ratio)));
    let sum=0; for(let j=start;j<end;j++) sum+=input[j];
    const sample=Math.max(-1,Math.min(1,sum/(end-start)));
    pcm.setInt16(44+i*2, sample < 0 ? sample*32768 : sample*32767, true);
  }
  return new Blob([pcm.buffer],{type:'audio/wav'});
}

async function transcribeFrames(frames, sourceRate){
  if (!callActive || busy || transcribing) return;
  transcribing = true; setState('thinking');
  const token = callToken;
  try {
    const audio = encodeWav16k(frames, sourceRate);
    const body = new FormData(); body.append('audio', audio, 'utterance.wav');
    const response = await fetch('/api/transcribe',{method:'POST',body});
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || 'Speech recognition failed');
    if (token !== callToken || !callActive) return;
    const text = (result.text || '').trim();
    if (text) { transcribing = false; await handle(text); }
    else { transcribing = false; setState('listening'); }
  } catch (e) {
    if (token === callToken && callActive) {
      console.warn('[JIMOZI] local Shruti STT error:', e);
      setState('error');
      await sleep(1200);
      if (callActive && token === callToken) setState('listening');
    }
    transcribing = false;
  }
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
    await speak(d.answer, token, d.answer_language);
  } catch(e){
    if (token !== callToken) return;
    setState('error');
    await sleep(2500);
  } finally {
    if (token === callToken) { busy = false; if (callActive) setState('listening'); }
  }
}

async function speakBrowserFast(text, token, langOverride){
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
  const wantLang = (langOverride || cfg.ttsLang || 'hi').toLowerCase();   // follows the language of the answer (en / hi)
  const lang = wantLang.startsWith('hi') ? 'hi' : wantLang;
  const voice = voices.find(v => (v.lang || '').toLowerCase().startsWith(lang));
  if (!voice || token !== callToken) return false;
  return await new Promise(resolve => {
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.voice = voice;
    utterance.lang = voice.lang || (lang === 'hi' ? 'hi-IN' : 'en-IN');
    utterance.rate = 1.04;
    utterance.pitch = 1;
    const t0 = performance.now();
    let started = false, finished = false, startTimer = null, stallTimer = null;
    const finish = ok => {
      if (finished) return;
      finished = true; clearTimeout(startTimer); clearTimeout(stallTimer);
      resolve(ok);
    };
    // If the browser never starts speaking, hand over to the server (Supertonic) voice instead of staying silent.
    startTimer = setTimeout(() => { if (!started) { try { window.speechSynthesis.cancel(); } catch(e){} finish(false); } }, 1500);
    // Safety net: some browsers never fire "end" on long text.
    stallTimer = setTimeout(() => { try { window.speechSynthesis.cancel(); } catch(e){} finish(true); }, Math.max(8000, text.length * 160));
    utterance.onstart = () => { started = true; };
    utterance.onend = () => {
      // An "end" that arrives almost instantly means nothing was actually spoken (silent-voice bug): fall back.
      const spoken = performance.now() - t0;
      finish(started && spoken > Math.min(700, text.length * 15));
    };
    utterance.onerror = () => finish(false);
    window.speechSynthesis.cancel();
    setTimeout(() => window.speechSynthesis.speak(utterance), 60);  // avoids the cancel()+speak() race that drops the utterance
  });
}

async function speak(text, token, langOverride){
  if (!text) return;
  // Browser-native Hindi speech starts sooner than a second HTTP request plus local TTS
  // synthesis. If a Hindi voice is unavailable, preserve the original Supertonic path.
  if (cfg.browserTtsFastPath && await speakBrowserFast(text, token, langOverride)) return;
  try {
    if (currentAudio) { currentAudio.pause(); currentAudio = null; }
    const r = await fetch('/api/speak', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, lang: langOverride || cfg.ttsLang, voice: cfg.ttsVoice })
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
  $('chatTitle').textContent = selectedMode === 'chef' ? 'Chef Support' : 'Consumer Support';
  $('modeHint').textContent = selectedMode === 'chef'
    ? 'Chef questions: use chef-approved knowledge configured for this product'
    : 'Customer questions: menu, pricing, orders and delivery';
  clearChatMessages();
  $('chatWelcome').textContent = selectedMode === 'chef'
    ? 'नमस्ते! Chef सहायता के लिए अपना सवाल लिखें।'
    : 'नमस्ते! Jimozi सहायता के लिए अपना सवाल लिखें।';
}
$('consumerMode').onclick = () => setMode('consumer');
$('chefMode').onclick = () => setMode('chef');
$('callBtn').onclick = startCall;
$('openChatBtn').onclick = openChat;
$('closeChatBtn').onclick = closeChat;
$('chatVoiceBtn').onclick = startCall;
$('endBtn').onclick = () => endCall();
$('chatForm').addEventListener('submit', event => {
  event.preventDefault();
  const input = $('chatInput');
  const text = input.value;
  if (!text.trim() || chatBusy) return;
  input.value = '';
  autoResizeChatInput();
  sendChatMessage(text);
});
$('chatInput').addEventListener('input', autoResizeChatInput);
addEventListener('keydown', e => { if (e.key === 'Escape' && callActive) endCall(); else if (e.key === 'Escape' && chatOpen) closeChat(); });
addEventListener('resize', () => { if (callActive) resize(); });
// Speech recognition is handled by local Shruti STT; browser SpeechRecognition is not used.