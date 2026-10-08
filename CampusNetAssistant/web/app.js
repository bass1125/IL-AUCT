'use strict';

/* ============================================================
   IL AUCT —— 前端逻辑
   后端：app.py（pywebview 暴露的 Api 对象）
   ============================================================ */

const $ = (id) => document.getElementById(id);

let ST = {};            // 后端状态
let BRIDGE = false;     // pywebview 是否就绪
let DIRTY = false;      // 表单是否被改过
let BUSY = false;

/* ------------------------------------------------ 桥接
   两套外壳都支持，前端代码不用分叉：
     · pywebview  → window.pywebview.api[name](...)
     · Edge app   → POST /api/<name>  {args:[...]}                */
async function call(name, ...args) {
  try {
    if (window.pywebview && window.pywebview.api) {
      return await window.pywebview.api[name](...args);
    }
    const res = await fetch('/api/' + name, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ args: args }),
    });
    if (!res.ok) return { ok: false, message: '后端返回 HTTP ' + res.status };
    return await res.json();
  } catch (e) {
    return { ok: false, message: String(e) };
  }
}

/* ------------------------------------------------ Toast */
function toast(msg, kind = '', ms = 3000) {
  const wrap = $('toast-wrap');
  const el = document.createElement('div');
  el.className = 'toast ' + kind;
  el.textContent = msg;
  wrap.appendChild(el);
  setTimeout(() => {
    el.classList.add('out');
    setTimeout(() => el.remove(), 220);
  }, ms);
}

/* ------------------------------------------------ 页面切换 */
function goto(page) {
  document.querySelectorAll('.nav-item').forEach((b) => {
    b.classList.toggle('active', b.dataset.page === page);
  });
  document.querySelectorAll('.page').forEach((p) => {
    p.classList.toggle('active', p.id === 'page-' + page);
  });
  if (page === 'logs') loadLog();
}

/* ------------------------------------------------ 时钟 */
function tickClock() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, '0');
  $('tb-clock').textContent = p(d.getHours()) + ':' + p(d.getMinutes());
  $('sb-time').textContent = p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
}

/* ------------------------------------------------ 渲染 */
function render() {
  const online = !!ST.online;
  const busy = !!ST.busy;
  // 首轮探测还没回来时，"未连接"是假阴性 —— 显示成"检测中"更诚实
  const pending = !busy && !ST.first_check_done;
  const tone = busy || pending ? 'busy' : online ? 'on' : 'off';

  // 标题栏
  const dot = $('tb-dot');
  dot.className = 'tb-dot ' + tone;
  $('tb-status-text').textContent = busy ? '连接中' : pending ? '检测中' : online ? '已连接' : '未连接';
  $('tb-ver').textContent = ST.version ? 'v' + ST.version : '';

  // 状态环
  const ring = $('status-ring');
  ring.className = 'status-ring ' + tone;
  $('ring-text').textContent = busy ? '连接中' : pending ? '检测中' : online ? '已连接' : '未连接';
  $('status-headline').textContent = busy
    ? '正在连接校园网…'
    : pending ? '正在检查网络…'
    : online ? '网络已连接' : '尚未连接校园网';
  $('status-sub').textContent = busy
    ? '正在提交认证，请稍候'
    : pending ? '首次检测，马上就好'
    : online ? '认证正常，可以正常上网'
    : (ST.status_text || '点击右侧「立即连接」试试');

  // 详情
  $('kv-ssid').textContent   = ST.ssid || '—';
  $('kv-ip').textContent     = ST.ip || '—';
  $('kv-server').textContent = ST.server || '—';
  $('kv-last').textContent   = ST.last_action || '—';

  // 按钮
  const bc = $('btn-connect');
  bc.disabled = busy;
  $('btn-connect-text').textContent = busy ? '连接中…' : '立即连接';
  $('p-connect').disabled = busy;

  // 状态栏
  const w = $('sb-watch');
  w.textContent = '守护：' + (ST.watch_running ? '运行中' : '未运行');
  w.className = 'sb-item ' + (ST.watch_running ? 'on' : 'off');
  $('sb-state').textContent = ST.status_text || '就绪';

  // 账号表单（未编辑时才回填，避免打断输入）
  if (!DIRTY && !BUSY) {
    $('in-user').value = ST.username || '';
    $('p-user').value  = ST.username || '';
    $('in-ssid').value = ST.wifi_ssid || '';
    $('p-ssid').value  = ST.wifi_ssid || '';
    $('ck-remember').checked  = !!ST.remember;
    $('p-remember').checked   = !!ST.remember;
    $('ck-autostart').checked = !!ST.autostart;
    $('p-autostart').checked  = !!ST.autostart;
    // 开机加速：后端没给这个字段（旧版）时，跟着「开机自动连接」的意图走；
    // 没开自启就没什么可加速的，勾上也没意义，直接当未启用并置灰
    const fast = (ST.fastboot !== undefined ? !!ST.fastboot : !!ST.autostart)
                 && !!ST.autostart;
    $('ck-fastboot').checked = fast;
    $('p-fastboot').checked  = fast;
    $('ck-fastboot').disabled = !ST.autostart;
    $('p-fastboot').disabled  = !ST.autostart;
    // 默认开：后端没返回这个字段时也当作开启，跟守护那边的默认值保持一致
    const popup = ST.close_popup !== false;
    $('ck-popup').checked = popup;
    $('p-popup').checked  = popup;
    const ph = ST.password_set ? '已保存（留空表示不修改）' : '请输入密码';
    $('in-pass').placeholder = ph;
    $('p-pass').placeholder = ph;
  }
  $('pill-state').textContent = DIRTY ? '有改动未保存'
    : (ST.username ? '已保存' : '未设置');
  $('pill-state').className = 'pill ' + (DIRTY ? 'warn' : ST.username ? 'ok' : '');

  // 关于页
  $('ab-ver').textContent = ST.version || '—';
  $('ab-provider').textContent = ST.provider || '—';
  $('ab-cfgdir').textContent = ST.cfg_dir || '—';
  $('ab-logfile').textContent = ST.log_file || '—';
  $('path-cfg').textContent = ST.cfg_file || 'config.json';

  renderUpdate();
  renderMiniLog();
  checkAutostartHint();
}

/* ------------------------------------------------ 软件更新 */
let UPDATE_TOASTED = '';   // 记下提示过的版本号，免得每 6 秒弹一次

function fmtMB(bytes) {
  return (Number(bytes || 0) / 1048576).toFixed(1);
}

function renderUpdate() {
  const up = ST.update || {};
  const current = up.current || ST.version || '';
  const latest = up.latest || '';
  const hasNew = !!up.available;

  $('up-current').textContent = current ? 'v' + current : '—';
  $('up-latest').textContent = latest
    ? 'v' + latest
    : (up.checked ? '已是最新' : '—');

  $('up-pill').style.display = hasNew ? '' : 'none';

  const notes = $('up-body');
  if (hasNew && up.notes) {
    notes.style.display = '';
    notes.textContent = up.notes;
  } else {
    notes.style.display = 'none';
  }

  // 进度条：只有下载中或已下载才露面
  const bar = $('up-bar');
  const active = up.downloading || up.downloaded;
  bar.style.display = active ? '' : 'none';
  bar.classList.toggle('done', !!up.downloaded);
  if (active) {
    const total = Number(up.progress_total || 0);
    const done = Number(up.progress || 0);
    const pct = up.downloaded ? 100 : (total ? Math.min(100, (done / total) * 100) : 0);
    $('up-fill').style.width = pct.toFixed(1) + '%';
  }

  // 下面那行说明文字
  const note = $('up-note');
  let text = '';
  if (up.downloading) {
    text = '正在下载 v' + latest + '… ' + fmtMB(up.progress) + ' / '
         + (up.progress_total ? fmtMB(up.progress_total) : '?') + ' MB';
  } else if (up.downloaded) {
    text = 'v' + latest + ' 已下载完成，点「重启并完成更新」生效';
  } else if (up.download_error) {
    text = up.download_error;
  } else if (up.checked && hasNew) {
    text = '发现新版本 v' + latest
         + (up.size ? '（' + fmtMB(up.size) + ' MB）' : '');
  } else if (up.checked && up.ok) {
    text = '已是最新版本';
  } else if (up.checked && up.error) {
    text = up.error;
  }
  note.textContent = text;
  note.className = 'hint';

  // 按钮
  const btn = $('btn-do-update');
  if (up.downloaded) {
    btn.style.display = ''; btn.disabled = false; btn.textContent = '重启并完成更新';
  } else if (up.downloading) {
    btn.style.display = ''; btn.disabled = true; btn.textContent = '下载中…';
  } else if (hasNew) {
    btn.style.display = ''; btn.disabled = false; btn.textContent = '立即更新';
  } else {
    btn.style.display = 'none';
  }

  // 第一次发现有新版本，提示一声
  if (hasNew && latest && UPDATE_TOASTED !== latest) {
    UPDATE_TOASTED = latest;
    toast('发现新版本 v' + latest + ' —— 去「关于」页点「立即更新」', 'ok', 6500);
  }
}

function renderMiniLog() {
  const box = $('mini-log');
  const lines = ST.log_tail || [];
  if (!lines.length) { box.innerHTML = '<div class="mini-empty">暂无记录</div>'; return; }
  box.innerHTML = lines.map((ln) => {
    const t = (ln || '').trim();
    let cls = 'mini-row', mark = '·';
    if (t.startsWith('✔')) { cls += ' ok';   mark = '✔'; }
    else if (t.startsWith('!')) { cls += ' warn'; mark = '!'; }
    else if (t.startsWith('✘') || t.startsWith('×')) { cls += ' err'; mark = '✘'; }
    else if (t.startsWith('•')) { mark = '•'; }
    const text = t.replace(/^[✔!•✘×]\s*/, '');
    return `<div class="${cls}"><span class="mini-time">${mark}</span>`
         + `<span class="mini-text">${esc(text)}</span></div>`;
  }).join('');
}

function esc(s) {
  return String(s).replace(/[&<>"]/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

/* ------------------------------------------------ 提醒 */
function checkAutostartHint() {
  const remember = $('ck-remember').checked;
  const auto = $('ck-autostart').checked;
  const msg = (auto && !remember)
    ? '没勾「记住我」，开机时没有密码可用，自动登录会失败。' : '';
  $('autostart-hint').textContent = msg;
  $('p-hint').textContent = msg;
}

/* ------------------------------------------------ 动作 */
async function refresh() {
  const s = await call('get_state');
  if (s && typeof s === 'object' && !('ok' in s && s.ok === false && !s.status)) {
    ST = s;
  }
  render();
}

async function loadLog() {
  const r = await call('read_log', 400);
  const txt = (r && r.text) || '';
  $('log-view').textContent = txt || '暂无日志';
  const n = txt ? txt.split('\n').filter((x) => x.trim()).length : 0;
  $('log-count').textContent = n;
  $('log-badge').textContent = n;
}

async function doConnect() {
  if (BUSY) return;
  BUSY = true; ST.busy = true; render();
  toast('正在连接，请稍候…');
  const r = await call('connect_now');
  BUSY = false; ST.busy = false;
  toast(r.message || (r.ok ? '连接成功' : '连接失败'), r.ok ? 'ok' : 'err', 4200);
  DIRTY = false;
  await refresh();
  await loadLog();
}

async function doSave() {
  const user = $('p-user').value.trim() || $('in-user').value.trim();
  const pass = $('p-pass').value || $('in-pass').value;
  const remember = $('p-remember').checked;
  const autostart = $('p-autostart').checked;
  const ssid = $('p-ssid').value.trim() || $('in-ssid').value.trim();
  const closePopup = $('p-popup').checked;
  const fastboot = $('p-fastboot').checked;

  if (!user) { toast('请先填用户名', 'warn'); return; }
  if (!pass && !ST.password_set) { toast('请先填密码', 'warn'); return; }
  if (autostart && !remember) {
    toast('要开机自动登录，必须先勾上「记住我」', 'warn', 4200); return;
  }

  // 只有加速开关的状态真的变了才会弹 UAC，提前说一声免得用户被吓到
  const willElevate = autostart && (fastboot !== !!ST.fastboot);
  $('panel-note').textContent = willElevate
    ? '正在保存…马上会弹出管理员授权窗，请点「是」'
    : '正在保存…';
  $('panel-note').className = 'panel-note';
  const r = await call('save_settings', user, pass, remember, autostart, ssid, closePopup, fastboot);
  if (r.ok) {
    DIRTY = false;
    $('in-pass').value = ''; $('p-pass').value = '';
    $('panel-note').textContent = '已保存';
    $('panel-note').className = 'panel-note ok';
    setTimeout(() => { $('panel-note').textContent = ''; $('panel-note').className = 'panel-note'; }, 2600);
  } else {
    $('panel-note').textContent = r.message || '保存失败';
    $('panel-note').className = 'panel-note err';
  }
  if (r.ok && r.watch_started) {
    toast('已保存 —— 后台守护已经跑起来了，现在就能关掉这个窗口', 'ok', 5600);
  } else if (r.ok && r.watch_stopped) {
    toast('已保存 —— 正在停止后台守护，开机也不会再自动登录了', 'ok', 5600);
  } else {
    toast(r.message || (r.ok ? '保存成功' : '保存失败'), r.ok ? 'ok' : 'err', 4200);
  }
  await refresh();
}

/* ------------------------------------------------ 更新动作 */
async function doCheckUpdate() {
  const btn = $('btn-check-update');
  btn.disabled = true; btn.textContent = '检查中…';
  const r = await call('check_update');
  btn.disabled = false; btn.textContent = '检查更新';
  await refresh();
  if (!r.ok) {
    toast(r.error || '检查更新失败', 'err', 4600);
  } else if (r.available) {
    toast('发现新版本 v' + r.latest, 'ok', 4600);
  } else {
    toast('已是最新版本 v' + (r.current || ''), 'ok');
  }
}

async function doUpdate() {
  const up = ST.update || {};

  // 已经下好了 —— 这一步会重启程序
  if (up.downloaded) {
    if (!confirm('软件将关闭以完成更新，之后会自动重新打开。\n现在继续吗？')) return;
    toast('正在重启完成更新…');
    await call('apply_update');
    return;
  }

  const btn = $('btn-do-update');
  btn.disabled = true; btn.textContent = '开始下载…';
  const r = await call('download_update');
  if (!r.ok) {
    toast(r.message || '下载失败', 'err', 4600);
    await refresh();
    return;
  }
  toast('正在下载新版本，请稍候…', 'ok');
  await pollDownload();
}

async function pollDownload() {
  // 后端是后台线程在下载，这里每 0.7 秒问一次进度
  for (let i = 0; i < 900; i++) {
    await new Promise((res) => setTimeout(res, 700));
    const p = await call('update_progress');
    if (p && typeof p === 'object') {
      ST.update = p;
      renderUpdate();
      if (!p.downloading) {
        await refresh();
        if (p.downloaded) {
          toast('新版本已下载完成，点「重启并完成更新」生效', 'ok', 6000);
        } else {
          toast(p.download_error || '下载失败', 'err', 5200);
        }
        return;
      }
    }
  }
  toast('下载超时，请重试', 'warn', 4600);
}

/* ------------------------------------------------ 表单绑定 */
function bindPair(a, b, evt) {
  const sync = (from, to) => {
    to.value = from.value;
    DIRTY = true;
    render();
  };
  a.addEventListener(evt, () => sync(a, b));
  b.addEventListener(evt, () => sync(b, a));
}

function bindCheckPairs(a, b) {
  const sync = (from, to) => {
    to.checked = from.checked;
    DIRTY = true;
    render();
  };
  a.addEventListener('change', () => sync(a, b));
  b.addEventListener('change', () => sync(b, a));
}

function toggleEye(btn, inp) {
  btn.addEventListener('click', () => {
    const show = inp.type === 'password';
    inp.type = show ? 'text' : 'password';
    btn.style.color = show ? 'var(--text)' : '';
  });
}

/* ------------------------------------------------ 启动 */
function init() {
  // 导航
  document.querySelectorAll('.nav-item').forEach((b) => {
    b.addEventListener('click', () => goto(b.dataset.page));
  });
  document.querySelectorAll('[data-goto]').forEach((b) => {
    b.addEventListener('click', () => goto(b.dataset.goto));
  });

  // 表单
  bindPair($('in-user'), $('p-user'), 'input');
  bindPair($('in-pass'), $('p-pass'), 'input');
  bindPair($('in-ssid'), $('p-ssid'), 'input');
  bindCheckPairs($('ck-remember'), $('p-remember'));
  bindCheckPairs($('ck-autostart'), $('p-autostart'));
  bindCheckPairs($('ck-fastboot'), $('p-fastboot'));
  bindCheckPairs($('ck-popup'), $('p-popup'));
  toggleEye($('btn-eye'), $('in-pass'));
  toggleEye($('p-eye'), $('p-pass'));

  // 按钮
  $('btn-connect').addEventListener('click', doConnect);
  $('p-connect').addEventListener('click', doConnect);
  $('btn-refresh').addEventListener('click', async () => {
    await call('probe');
    await refresh();
    toast('已重新检测');
  });
  $('btn-save').addEventListener('click', doSave);
  $('p-save').addEventListener('click', doSave);
  $('btn-test').addEventListener('click', doConnect);
  $('btn-log-refresh').addEventListener('click', loadLog);
  $('btn-check-update').addEventListener('click', doCheckUpdate);
  $('btn-do-update').addEventListener('click', doUpdate);
  $('btn-log-file').addEventListener('click', () => call('open_log_file'));
  $('btn-data-dir').addEventListener('click', () => call('open_data_dir'));
  $('btn-log-dir').addEventListener('click', () => call('open_log_file'));
  $('btn-open-cfgdir').addEventListener('click', () => call('open_data_dir'));
  $('btn-open-logfile').addEventListener('click', () => call('open_log_file'));
  $('btn-open-dir').addEventListener('click', () => call('open_config_dir'));

  tickClock();
  setInterval(tickClock, 1000);
}

window.addEventListener('DOMContentLoaded', () => {
  init();
  if (window.pywebview && window.pywebview.api) {
    BRIDGE = true; boot();
    return;
  }
  if (location.protocol === 'http:' || location.protocol === 'https:') {
    // 页面由本地服务发出 —— 这个服务本身就是后端（Edge app 外壳）
    BRIDGE = true; boot();
    return;
  }
  window.addEventListener('pywebviewready', () => { BRIDGE = true; boot(); });
  setTimeout(() => {
    if (!BRIDGE) {
      $('status-headline').textContent = '界面未连接到后端';
      $('status-sub').textContent = '（浏览器预览模式，功能不可用）';
    }
  }, 1200);
});

/* 关窗口时告诉后端可以收摊了（心跳也能兜住，这个只是让退出更利索） */
window.addEventListener('beforeunload', () => {
  try { navigator.sendBeacon('/api/quit'); } catch (e) { /* ignore */ }
});

async function boot() {
  await refresh();
  await loadLog();
  setInterval(refresh, 6000);
}
