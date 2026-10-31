const $ = id => document.getElementById(id);

function setStatus(el, text, isError){ el.textContent = text; el.classList.toggle('error', !!isError); }
function safe(value){ const d=document.createElement('div'); d.textContent=String(value ?? ''); return d.innerHTML; }
function number(v){ return Number(v || 0).toLocaleString(); }
function svgWrap(content, height=210){ return `<svg viewBox="0 0 600 ${height}" role="img" aria-label="Analytics chart" preserveAspectRatio="none">${content}</svg>`; }

function renderDaily(items){
  const el=$('dailyChart'); if(!items.length){el.innerHTML='<div class="empty-chart">No query activity in this period.</div>';return;}
  const max=Math.max(1,...items.map(x=>x.total));
  const w=600, h=210, left=34, bottom=30, top=12, plotH=h-top-bottom, step=(w-left-12)/items.length;
  let s='';
  [0,.25,.5,.75,1].forEach(t=>{const y=top+plotH*(1-t);s+=`<line x1="${left}" y1="${y}" x2="590" y2="${y}" class="gridline"/><text x="27" y="${y+4}" class="axis" text-anchor="end">${Math.round(max*t)}</text>`});
  items.forEach((d,i)=>{const x=left+i*step+step*.18,bw=Math.max(3,step*.62),totalH=d.total/max*plotH,failH=d.failed/max*plotH;
    s+=`<rect x="${x}" y="${top+plotH-totalH}" width="${bw}" height="${totalH}" rx="3" class="bar-total"><title>${safe(d.date)}: ${d.total} total</title></rect>`;
    if(d.failed)s+=`<rect x="${x}" y="${top+plotH-failH}" width="${bw}" height="${failH}" rx="3" class="bar-failed"><title>${d.failed} failed</title></rect>`;
    if(items.length<=14 || i%Math.ceil(items.length/10)===0)s+=`<text x="${x+bw/2}" y="${h-8}" class="axis" text-anchor="middle">${safe(d.date.slice(5))}</text>`;
  });
  el.innerHTML=svgWrap(s,h);
}
function renderBars(id, items, labelKey, valueKey){
  const el=$(id); if(!items.length){el.innerHTML='<div class="empty-chart">No data available.</div>';return;}
  const max=Math.max(1,...items.map(x=>Number(x[valueKey]||0)));
  let s='';
  items.slice(0,12).forEach((x,i)=>{const y=12+i*31, width=420*Number(x[valueKey]||0)/max;
    s+=`<text x="8" y="${y+13}" class="bar-label">${safe(x[labelKey])}</text><rect x="190" y="${y}" width="${Math.max(1,width)}" height="18" rx="4" class="bar-total"/><text x="${Math.min(580,198+width)}" y="${y+14}" class="bar-value">${number(x[valueKey])}</text>`;
  });
  el.innerHTML=svgWrap(s,Math.max(90,items.slice(0,12).length*31+18));
}
function renderQueries(items){
  $('topQueries').innerHTML = items.length ? `<table><thead><tr><th>Question</th><th>Failures</th><th>Last seen</th></tr></thead><tbody>${items.map(x=>`<tr><td>${safe(x.query)}</td><td><b class="fail-text">${number(x.count)}</b><small>${safe((x.reasons||[]).map(r=>r.reason).join(', '))}</small></td><td>${safe(new Date(x.last_seen).toLocaleString())}</td></tr>`).join('')}</tbody></table>` : '<div class="empty-chart">No failed questions recorded. New events will appear here.</div>';
}
function renderRecent(items){
  $('recentQueries').innerHTML = items.length ? `<table><thead><tr><th>Time</th><th>Question</th><th>Mode</th><th>Result</th><th>Reason / method</th><th>Latency</th></tr></thead><tbody>${items.map(x=>`<tr><td>${safe(new Date(x.time).toLocaleString())}</td><td>${safe(x.query)}</td><td>${safe(x.mode)}</td><td><span class="${x.success?'ok-text':'fail-text'}">${x.success?'Success':'Failed'}</span></td><td>${safe(x.failure_reason||x.answer_mode||'—')}</td><td>${number(x.latency_ms)} ms</td></tr>`).join('')}</tbody></table>` : '<div class="empty-chart">No queries recorded yet.</div>';
}
async function refreshAnalytics(){
  const status=$('analyticsStatus'), content=$('analyticsContent');
  setStatus(status,'Loading analytics…');
  try{
    const days=$('analyticsDays').value;
    const r=await fetch(`/api/admin/analytics?days=${encodeURIComponent(days)}`);
    const d=await r.json(); if(!r.ok)throw new Error(d.detail||'Could not load analytics');
    const x=d.summary;
    $('kpiTotal').textContent=number(x.total_queries);$('kpiFailed').textContent=number(x.failed_queries);
    $('kpiSuccess').textContent=number(x.successful_queries);$('kpiLatency').textContent=number(x.avg_latency_ms)+' ms';
    $('kpiFailureRate').textContent=x.failure_rate+'% failure rate';$('kpiSuccessRate').textContent=x.success_rate+'% success rate';
    $('kpiPeriod').textContent=`Last ${d.period_days} day(s)`;
    renderDaily(d.daily);renderBars('failureChart',d.failure_reasons,'reason','count');
    renderQueries(d.top_failing_queries);
    renderBars('modeChart',d.modes.map(m=>({...m, label:`${m.mode} · ${m.failed} failed / ${m.total}`})),'label','total');
    renderBars('hourChart',d.hourly,'hour','count');
    renderBars('productChart',d.products.map(p=>({...p,label:`${p.product_id} · ${p.failed} failed / ${p.total}`})),'label','total');
    renderRecent(d.recent_queries);
    content.hidden=false;content.dataset.loaded='true';setStatus(status,`${number(x.total_queries)} queries analysed. ${number(x.failed_queries)} failed.`);
  }catch(e){content.hidden=true;setStatus(status,e.message,true);}
}
async function refresh(){
  const p = $('product').value.trim(), box = $('documents');
  if (!p) { setStatus(box, 'Enter a product ID.'); return; }
  try {
    const r = await fetch(`/api/products/${encodeURIComponent(p)}/documents`);
    const d = await r.json(); if (!r.ok) throw new Error(d.detail || 'Could not load documents');
    if (!d.documents.length) { box.dataset.loaded='true'; setStatus(box, 'No documents uploaded yet.'); return; }
    box.textContent = ''; box.classList.remove('error'); box.dataset.loaded='true';
    for (const x of d.documents) {
      const row = document.createElement('div'); row.className = 'doc';
      const info = document.createElement('div'), name = document.createElement('strong'), meta = document.createElement('div');
      name.textContent = x.filename; meta.className = 'docmeta small';
      meta.textContent = `${x.chunks} text chunks · audience: ${x.audience}${x.pages ? ` · ${x.pages} pages` : ''}`;
      info.append(name, meta);
      const del = document.createElement('button'); del.className = 'del'; del.textContent = 'Delete';
      del.onclick = async () => {
        if (!confirm(`Delete ${x.filename} and remove its indexed knowledge?`)) return;
        const rr = await fetch(`/api/products/${encodeURIComponent(p)}/documents/${encodeURIComponent(x.id)}`, { method: 'DELETE' });
        const out = await rr.json(); if (!rr.ok) alert(out.detail || 'Delete failed');
        await refresh();
      };
      row.append(info, del); box.append(row);
    }
  } catch(e){ setStatus(box, e.message, true); }
}
// Bind only after the page markup is available. A missing optional control must
// never abort the complete admin script (which would also hide the charts).
function bindAdminEvents(){
  const refreshButton=$('refreshButton');
  const productInput=$('product');
  const refreshAnalyticsButton=$('refreshAnalytics');
  const analyticsDays=$('analyticsDays');
  const uploadForm=$('uploadForm');
  const tabButtons=Array.from(document.querySelectorAll('[data-tab]'));

  function activateTab(tabId, moveFocus=false){
    for(const button of tabButtons){
      const active=button.dataset.tab===tabId;
      button.classList.toggle('active',active);
      button.setAttribute('aria-selected',String(active));
      button.tabIndex=active?0:-1;
      const panel=$(button.dataset.tab);
      if(panel) panel.hidden=!active;
      if(active && moveFocus) button.focus();
    }
    if(tabId==='analyticsTab' && $('analyticsContent')?.dataset.loaded!=='true') refreshAnalytics();
    if(tabId==='knowledgeTab' && $('documents') && $('product') && !$('documents').dataset.loaded) refresh();
  }

  tabButtons.forEach((button,index)=>{
    button.addEventListener('click',()=>activateTab(button.dataset.tab));
    button.addEventListener('keydown',event=>{
      if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) return;
      event.preventDefault();
      let next=index;
      if(event.key==='ArrowRight') next=(index+1)%tabButtons.length;
      if(event.key==='ArrowLeft') next=(index-1+tabButtons.length)%tabButtons.length;
      if(event.key==='Home') next=0;
      if(event.key==='End') next=tabButtons.length-1;
      activateTab(tabButtons[next].dataset.tab,true);
    });
  });

  if(refreshButton) refreshButton.addEventListener('click', refresh);
  if(productInput) productInput.addEventListener('change', refresh);
  if(refreshAnalyticsButton) refreshAnalyticsButton.addEventListener('click', refreshAnalytics);
  if(analyticsDays) analyticsDays.addEventListener('change', refreshAnalytics);

  if(uploadForm){
    uploadForm.addEventListener('submit', async e => {
      e.preventDefault();
      const p=$('product')?.value.trim();
      const fileInput=$('file');
      const f=fileInput?.files?.[0];
      const st=$('uploadStatus');
      const uploadButton=$('uploadButton');
      const audience=$('audience');
      if(!p || !f) return;
      if(uploadButton) uploadButton.disabled=true;
      if(st) setStatus(st,'Uploading, extracting text and indexing…');
      try{
        const form=new FormData();
        form.append('file',f);
        form.append('audience',audience?.value || 'both');
        const r=await fetch(`/api/products/${encodeURIComponent(p)}/documents`,{method:'POST',body:form});
        const d=await r.json();
        if(!r.ok) throw new Error(d.detail||'Upload failed');
        if(st) setStatus(st,`Indexed ${d.document.filename}: ${d.document.chunks} chunks. ${d.document.embedded?'Semantic embeddings created.':'Indexed with lexical search.'}`);
        if(fileInput) fileInput.value='';
        await refresh();
      }catch(err){
        if(st) setStatus(st,err.message,true);
      }finally{
        if(uploadButton) uploadButton.disabled=false;
      }
    });
  }

  // Analytics is an independent section. If an older HTML document is cached,
  // report the mismatch instead of throwing and breaking all admin functionality.
  const analyticsStatus=$('analyticsStatus');
  const activeTab=document.querySelector('[data-tab].active')?.dataset.tab || 'analyticsTab';
  if($('analyticsContent') && analyticsStatus){
    if(activeTab==='analyticsTab') refreshAnalytics();
  }else if(analyticsStatus){
    setStatus(analyticsStatus,'Analytics markup is missing. Reload the admin page after clearing its cache.',true);
  }
  if(activeTab==='knowledgeTab' && productInput && $('documents')) refresh();
}

if(document.readyState === 'loading'){
  document.addEventListener('DOMContentLoaded', bindAdminEvents, {once:true});
}else{
  bindAdminEvents();
}
