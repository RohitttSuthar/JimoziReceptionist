const $ = id => document.getElementById(id);

function setStatus(el, text, isError){ el.textContent = text; el.classList.toggle('error', !!isError); }

async function refresh(){
  const p = $('product').value.trim();
  const box = $('documents');
  if (!p) { setStatus(box, 'Enter a product ID.'); return; }
  try {
    const r = await fetch(`/api/products/${encodeURIComponent(p)}/documents`);
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || 'Could not load documents');
    if (!d.documents.length) { setStatus(box, 'No documents uploaded yet.'); return; }
    box.textContent = ''; box.classList.remove('error');
    for (const x of d.documents) {
      const row = document.createElement('div'); row.className = 'doc';
      const info = document.createElement('div');
      const name = document.createElement('strong'); name.textContent = x.filename;
      const meta = document.createElement('div'); meta.className = 'docmeta small';
      meta.textContent = `${x.chunks} text chunks · audience: ${x.audience}${x.pages ? ` · ${x.pages} pages` : ''}`;
      info.append(name, meta);
      const del = document.createElement('button'); del.className = 'del'; del.textContent = 'Delete';
      del.onclick = async () => {
        if (!confirm(`Delete ${x.filename} and remove its indexed knowledge?`)) return;
        const rr = await fetch(`/api/products/${encodeURIComponent(p)}/documents/${encodeURIComponent(x.id)}`, { method: 'DELETE' });
        const out = await rr.json();
        if (!rr.ok) alert(out.detail || 'Delete failed');
        await refresh();
      };
      row.append(info, del); box.append(row);
    }
  } catch(e){ setStatus(box, e.message, true); }
}

$('uploadForm').addEventListener('submit', async e => {
  e.preventDefault();
  const p = $('product').value.trim(), f = $('file').files[0];
  if (!f) return;
  const st = $('uploadStatus');
  $('uploadButton').disabled = true;
  setStatus(st, 'Uploading, extracting text and indexing…');
  try {
    const form = new FormData();
    form.append('file', f); form.append('audience', $('audience').value);
    const r = await fetch(`/api/products/${encodeURIComponent(p)}/documents`, { method: 'POST', body: form });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || 'Upload failed');
    setStatus(st, `Indexed ${d.document.filename}: ${d.document.chunks} chunks. ${d.document.embedded ? 'Semantic embeddings created.' : 'Indexed with lexical search (configure OpenAI embeddings for semantic search).'}`);
    $('file').value = '';
    await refresh();
  } catch(err){ setStatus(st, err.message, true); }
  finally { $('uploadButton').disabled = false; }
});

$('refreshButton').onclick = refresh;
$('product').addEventListener('change', refresh);
refresh();