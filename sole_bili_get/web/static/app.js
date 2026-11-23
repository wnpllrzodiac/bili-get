'use strict';
const app = document.getElementById('app');
const bar = document.getElementById('bar');
let pollTimer = null;

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (v === true) el.setAttribute(k, '');
    else if (v !== false && v != null) el.setAttribute(k, v);
  }
  for (const kid of kids.flat()) if (kid != null) el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  return el;
}
// all urls are relative so the app works under any reverse proxy prefix
async function api(method, url, body) {
  const res = await fetch(url, {
    method, headers: Object.assign({'X-Requested-With': 'bili-web'}, body ? {'Content-Type': 'application/json'} : {}),
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch (e) { /* no body */ }
  if (!res.ok) {
    const err = new Error((data && data.detail) || res.statusText);
    err.status = res.status;
    throw err;
  }
  return data;
}
function show(...nodes) { clearInterval(pollTimer); app.replaceChildren(...nodes); }

// ---------- login ----------
function loginView() {
  bar.hidden = true;
  const err = h('div', {class: 'err'});
  const user = h('input', {type: 'text', value: 'admin', autocomplete: 'username'});
  const pw = h('input', {type: 'password', autocomplete: 'current-password'});
  const submit = async () => {
    err.textContent = '';
    try {
      const r = await api('POST', 'api/login', {username: user.value, password: pw.value});
      r.must_change_password ? passwordView(true, pw.value) : mainView();
    } catch (e) { err.textContent = e.message; }
  };
  pw.addEventListener('keydown', e => { if (e.key === 'Enter') submit(); });
  show(h('div', {class: 'card'}, h('h2', {}, 'Log in'),
    h('label', {}, 'Username'), user, h('label', {}, 'Password'), pw,
    h('div', {class: 'row', style: 'margin-top:12px'}, h('button', {onclick: submit}, 'Log in')), err));
  pw.focus();
}

// ---------- change password (forced after the first login) ----------
function passwordView(forced, oldPw) {
  bar.hidden = forced;
  const err = h('div', {class: 'err'});
  const old = h('input', {type: 'password', value: oldPw || '', autocomplete: 'current-password'});
  const n1 = h('input', {type: 'password', autocomplete: 'new-password'});
  const n2 = h('input', {type: 'password', autocomplete: 'new-password'});
  show(h('div', {class: 'card'}, h('h2', {}, forced ? 'Please set a new password' : 'Change password'),
    forced ? h('p', {class: 'muted'}, 'You are using the default password. Choose a new one (at least 8 characters) to continue.') : null,
    h('label', {}, 'Current password'), old, h('label', {}, 'New password'), n1, h('label', {}, 'Repeat new password'), n2,
    h('div', {class: 'row', style: 'margin-top:12px'},
      h('button', {onclick: async () => {
        err.textContent = '';
        if (n1.value !== n2.value) { err.textContent = 'passwords do not match'; return; }
        try { await api('POST', 'api/password', {old_password: old.value, new_password: n1.value}); loginView(); }
        catch (e) { err.textContent = e.message; }
      }}, 'Save'),
      forced ? null : h('button', {class: 'secondary', onclick: settingsView}, 'Back')), err));
}

// ---------- settings ----------
async function settingsView() {
  bar.hidden = false;
  const s = await api('GET', 'api/settings');
  const err = h('div', {class: 'err'}), ok = h('div', {class: 'muted'});
  const ck = h('input', {type: 'password', placeholder: s.cookie_set ? 'cookie is set (' + s.cookie_tail + '), enter a new one to replace' : 'SESSDATA=...; bili_jct=...', autocomplete: 'off'});
  const dir = h('input', {type: 'text', value: s.save_dir, placeholder: s.default_save_dir, autocomplete: 'off'});
  const free = s.free_bytes != null ? ` Free space: ${(s.free_bytes / 1073741824).toFixed(1)} GB.` : '';
  const save = async (body, msg) => {
    err.textContent = ''; ok.textContent = '';
    try { await api('PUT', 'api/settings', body); await settingsView(); app.querySelector('.msg').textContent = msg; }
    catch (e) { err.textContent = e.message; }
  };
  show(h('div', {class: 'card'}, h('h2', {}, 'Global settings'),
    h('label', {}, 'Save folder (absolute path on the server)'), dir,
    h('p', {class: 'muted'}, 'Finished downloads are moved here. Leave it empty to keep them in the job folder (', s.default_save_dir, '). ',
      'The folder is created if needed and must be writable by the server. A file that already exists is never overwritten (a " (1)" suffix is added). ',
      'Deleting a job never deletes files in this folder.', free),
    h('div', {class: 'row'}, h('button', {onclick: () => save({save_dir: dir.value}, 'Save folder updated.')}, 'Save folder'),
      s.save_dir ? h('button', {class: 'secondary', onclick: () => save({save_dir: ''}, 'Save folder reset to the default.')}, 'Use default') : null),
    h('label', {style: 'margin-top:18px'}, 'Bilibili cookie (optional, needed for 1080P and above)'), ck,
    h('p', {class: 'muted'}, 'Stored on the server only, it is never sent back to the browser.'),
    h('div', {class: 'row'},
      h('button', {onclick: () => save({cookie: ck.value}, 'Cookie updated.')}, 'Save cookie'),
      s.cookie_set ? h('button', {class: 'secondary', onclick: () => save({cookie: ''}, 'Cookie removed.')}, 'Remove cookie') : null),
    h('div', {class: 'msg muted', style: 'margin-top:8px'}), err, ok,
    h('div', {class: 'row', style: 'margin-top:16px'},
      h('button', {class: 'secondary', onclick: () => passwordView(false)}, 'Change password'),
      h('button', {class: 'secondary', onclick: mainView}, 'Back'))));
}

// ---------- main: analyse -> options -> download, plus job list ----------
const STAGE = {analyzing: 'analysing', video: 'downloading video', audio: 'downloading audio', cover: 'cover', merging: 'merging'};
function mainView() {
  bar.hidden = false;
  const url = h('input', {type: 'text', placeholder: 'https://www.bilibili.com/video/BV...'});
  const err = h('div', {class: 'err'});
  const detail = h('div');
  const jobsBox = h('div', {class: 'card'});
  const openLogs = new Map(); //job id -> log text of the jobs whose log is expanded
  const goBtn = h('button', {onclick: analyze}, 'Analyse');
  url.addEventListener('keydown', e => { if (e.key === 'Enter') analyze(); });

  async function analyze() {
    err.textContent = ''; detail.replaceChildren(); goBtn.disabled = true; goBtn.textContent = 'Analysing…';
    try { detail.replaceChildren(optionsCard(await api('POST', 'api/analyze', {url: url.value}))); }
    catch (e) { err.textContent = e.message; }
    goBtn.disabled = false; goBtn.textContent = 'Analyse';
  }
  function optionsCard(info) {
    const q = h('select', {}, h('option', {value: 'MAX'}, 'Best available'), h('option', {value: 'MIN'}, 'Lowest'),
      info.qualities.map(x => h('option', {value: x.id}, `${x.name} (${x.width}x${x.height})`)));
    const CODEC_NAME = {avc1: 'AVC / H.264', hev1: 'HEVC / H.265', hvc1: 'HEVC / H.265', av01: 'AV1'};
    const codecs = [...new Set(info.qualities.flatMap(x => x.codecs))]; //only what this video really offers
    const codec = h('select', {}, h('option', {value: ''}, 'Default (AVC first)'),
      codecs.map(c => h('option', {value: c}, CODEC_NAME[c] || c)));
    const mode = h('select', {}, h('option', {value: 'merged'}, 'Video + audio (merged MP4)'), h('option', {value: 'video'}, 'Video only'), h('option', {value: 'audio'}, 'Audio only'));
    const playlist = h('input', {type: 'checkbox'}), danmu = h('input', {type: 'checkbox'}), cover = h('input', {type: 'checkbox'});
    const derr = h('div', {class: 'err'});
    return h('div', {class: 'card'},
      h('div', {class: 'video'}, h('img', {src: info.cover, referrerpolicy: 'no-referrer', alt: ''}),
        h('div', {}, h('h2', {}, info.title), h('div', {class: 'muted'}, info.author, ' · ', info.episodes, ' episode(s)'))),
      h('div', {class: 'opts'},
        h('div', {}, h('label', {}, 'Quality'), q), h('div', {}, h('label', {}, 'Video codec'), codec), h('div', {}, h('label', {}, 'Streams'), mode)),
      info.episodes > 1 ? h('label', {class: 'check'}, playlist, 'Download all ', info.episodes, ' episodes (otherwise only the one in the url)') : null,
      h('label', {class: 'check'}, danmu, 'Also download danmu (XML)'), h('label', {class: 'check'}, cover, 'Also download cover'),
      h('button', {onclick: async () => {
        derr.textContent = '';
        try {
          await api('POST', 'api/jobs', {url: info.url, quality: q.value, codec: codec.value || null, mode: mode.value,
                                         playlist: playlist.checked, danmu: danmu.checked, cover: cover.checked, title: info.title});
          detail.replaceChildren(); loadJobs();
        } catch (e) { derr.textContent = e.message; }
      }}, 'Download'), derr);
  }
  async function toggleLog(id) {
    if (openLogs.has(id)) { openLogs.delete(id); return loadJobs(); }
    try { const r = await fetch(`api/jobs/${id}/log`, {headers: {'X-Requested-With': 'bili-web'}}); openLogs.set(id, await r.text()); }
    catch (e) { openLogs.set(id, 'failed to load the log: ' + e.message); }
    loadJobs();
  }
  async function loadJobs() {
    let jobs;
    try { jobs = await api('GET', 'api/jobs'); } catch (e) { if (e.status === 401) loginView(); return; }
    //replaceChildren() would turn null and arrays into text, so go through h() which skips null and flattens arrays
    jobsBox.replaceChildren(...h('div', {}, h('h2', {}, 'Downloads'),
      jobs.length ? null : h('div', {class: 'muted'}, 'No downloads yet.'), jobs.map(jobRow)).childNodes);
  }
  function jobRow(j) {
    const active = j.state === 'queued' || j.state === 'running';
    return h('div', {class: 'job'},
      h('div', {class: 'row'}, h('strong', {}, j.title || j.url), h('span', {class: 'tag ' + j.state}, j.state),
        h('span', {class: 'spacer'}),
        active ? h('button', {class: 'secondary', onclick: async () => { await api('POST', `api/jobs/${j.id}/cancel`); loadJobs(); }}, 'Cancel')
               : h('button', {class: 'secondary', onclick: async () => { await api('DELETE', `api/jobs/${j.id}`); loadJobs(); }}, 'Delete')),
      j.title ? h('div', {class: 'muted'}, j.url) : null,
      j.state === 'running' ? [h('div', {class: 'bar'}, h('div', {style: `width:${j.percent || 0}%`})),
        h('div', {class: 'muted'}, (STAGE[j.stage] || j.stage), ' ', Math.round(j.percent || 0), '%')] : null,
      j.error ? h('div', {class: 'err'}, j.error) : null,
      j.state !== 'queued' ? h('div', {}, h('button', {class: 'link', onclick: () => toggleLog(j.id)}, openLogs.has(j.id) ? 'Hide log' : 'View log'),
        j.state === 'failed' ? h('span', {class: 'muted'}, ' log file on the server: ', j.log_file) : null) : null,
      openLogs.has(j.id) ? h('pre', {class: 'log'}, openLogs.get(j.id)) : null,
      j.state === 'done' && j.out_dir ? h('div', {class: 'muted'}, 'Saved in ', j.out_dir) : null,
      j.state === 'done' ? h('div', {}, j.files.map(f => h('div', {}, h('a', {href: `api/jobs/${j.id}/files/${encodeURIComponent(f)}`}, f)))) : null);
  }
  show(h('div', {class: 'card'}, h('h2', {}, 'Download a Bilibili video'), h('div', {class: 'row'}, url, goBtn), err), detail, jobsBox);
  loadJobs();
  pollTimer = setInterval(loadJobs, 2000);
}

document.getElementById('btn-settings').addEventListener('click', settingsView);
document.getElementById('btn-logout').addEventListener('click', async () => { try { await api('POST', 'api/logout'); } catch (e) { /* ignore */ } loginView(); });

(async () => {
  try {
    const me = await api('GET', 'api/me');
    me.must_change_password ? passwordView(true) : mainView();
  } catch (e) { loginView(); }
})();
