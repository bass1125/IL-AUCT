/* IL AUCT 卸载器 —— 前端逻辑
   后端是同一套 /api/<方法名> 约定，直接用 fetch 打过去。 */

const $ = (id) => document.getElementById(id);

let armed = false;          // 「确认卸载」是否已进入待确认状态
let armTimer = null;
let finished = false;       // 已经卸完了，接下来的点击都变成「关闭窗口」

/* ---------------------------------------------------------- 通用 */
async function call(name, args = []) {
  const resp = await fetch('/api/' + name, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ args }),
  });
  return resp.json();
}

function toast(msg, kind = '', ms = 4000) {
  const el = document.createElement('div');
  el.className = 'toast ' + kind;
  el.textContent = msg;
  $('toast-wrap').appendChild(el);
  setTimeout(() => {
    el.classList.add('out');
    setTimeout(() => el.remove(), 220);
  }, ms);
}

function esc(text) {
  return String(text == null ? '' : text)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function escAttr(text) {
  return esc(text).replace(/"/g, '&quot;');
}

/* 一行：左边小标记 + 中间标题/说明 + 右边状态 */
function row(mark, title, sub, side, sideClass, markClass) {
  const sideHtml = side
    ? `<div class="row-side ${sideClass || ''}">${esc(side)}</div>` : '';
  const subHtml = sub ? `<div class="row-sub">${esc(sub)}</div>` : '';
  return `<div class="row">
    <span class="row-mark ${markClass || ''}">${mark}</span>
    <div class="row-main">
      <div class="row-title">${esc(title)}</div>
      ${subHtml}
    </div>
    ${sideHtml}
  </div>`;
}

/* ---------------------------------------------------------- 渲染清单 */
function renderList(s) {
  const items = [];

  items.push(row('', '后台抢网进程',
    '开机后自动登录校园网的那个后台程序，连上就退出',
    s.watch_running ? '正在抢网' : '已退出',
    s.watch_running ? 'on' : ''));

  if (s.autostart) {
    items.push(row('', '开机自启项',
      s.autostart_disabled
        ? '注册表里还在，只是被任务管理器禁用了'
        : '注册表启动项 campusnet',
      '已注册', 'warn'));
  } else {
    items.push(row('', '开机自启项', '注册表里没有这条记录', '无'));
  }

  if (s.program_count > 0) {
    items.push(row('', '程序文件', s.program_dir,
      `${s.program_count} 项 · ${s.program_size_text}`));
  } else {
    items.push(row('', '程序文件', s.program_dir,
      '没找到', 'warn', 'warn'));
  }

  if (s.temp_leftovers.length) {
    items.push(row('', '残留的临时解压目录',
      s.temp_leftovers.join('、'),
      s.temp_size_text, 'warn'));
  } else {
    items.push(row('', '残留的临时解压目录', '干净，没有残留', '—'));
  }

  if (s.legacy_files.length) {
    items.push(row('', '旧版启动脚本', s.legacy_files.join('、'),
      '待清理', 'warn'));
  }

  if (s.local_exists) {
    items.push(row('', '界面缓存', 'Edge 的私有配置目录', s.local_size_text));
  }

  items.push(row('', '卸载器自身', '关窗后自动删掉', '最后一步'));

  $('rows-remove').innerHTML = items.join('');
  $('pill-total').textContent = `可腾出 ${s.total_size_text}`;
  $('brand-sub').textContent = s.program_dir;

  /* 可选删除项 */
  $('opt-cfg-sub').textContent = s.config_exists
    ? `config.json · ${s.config_size_text} —— 密码是明文存的`
    : 'config.json · 不存在，没什么可删';
  $('ck-config').disabled = !s.config_exists;

  $('opt-log-sub').textContent = s.log_exists
    ? `${s.log_files.join('、')} · ${s.log_size_text}`
    : 'watch.log · 没有日志文件';
  $('ck-logs').disabled = !s.log_exists;

  /* 底部说明：目录里不属于本程序的东西，明确告诉用户不会动 */
  const foot = $('foot');
  const keep = s.keep_files || [];
  if (s.is_install_dir === false) {
    foot.className = 'uni-foot warn';
    foot.textContent = '这个目录里没有 IL AUCT.exe，像把卸载器单独拿出来了 —— '
      + '点确认只会清掉后台进程和开机自启，不会删任何文件。';
    $('btn-go').textContent = '仅清理自启与后台';
    $('pill-total').textContent = '不动文件';
  } else if (keep.length) {
    foot.className = 'uni-foot';
    foot.textContent = `目录里这些不是本程序的文件，会原样保留：${keep.join('、')}`;
  } else {
    foot.className = 'uni-foot';
    foot.textContent = '只删本程序自己的文件，别的东西一律不动。';
  }

  $('tb-status').textContent = '准备就绪';
}

/* ---------------------------------------------------------- 渲染结果 */
function renderResult(r) {
  const marks = { ok: '✔', bad: '✘', warn: '!' };

  $('rows-remove').innerHTML = r.steps.map((s) => {
    const cls = s.ok ? (s.level === 'warn' ? 'warn' : 'ok') : 'bad';
    return row(marks[cls], s.title, s.detail, '', '', cls);
  }).join('');

  const failed = r.failed || 0;
  if (failed) {
    $('pill-total').textContent = `${r.steps.length - failed} / ${r.steps.length} 完成`;
    $('pill-total').className = 'pill warn';
  } else {
    $('pill-total').textContent = '全部完成';
    $('pill-total').className = 'pill ok';
  }

  $('card-optional').style.display = 'none';
  $('btn-cancel').style.display = 'none';
  $('tb-status').textContent = '卸载完成';

  const btn = $('btn-go');
  btn.className = 'btn btn-primary';
  btn.disabled = false;
  btn.textContent = '关闭窗口';

  /* 倒数几秒自动关 —— 关掉之后那个批处理才能把 exe 删掉 */
  let left = 6;
  const foot = $('foot');
  foot.className = 'uni-foot ok';
  const tick = setInterval(() => {
    if (left <= 0) {
      clearInterval(tick);
      quit();
      return;
    }
    foot.textContent = `${left} 秒后自动关闭，然后完成最后清理…`;
    left -= 1;
  }, 1000);
  foot.textContent = `${left} 秒后自动关闭，然后完成最后清理…`;
}

/* ---------------------------------------------------------- 动作 */
function disarm() {
  armed = false;
  clearTimeout(armTimer);
  const btn = $('btn-go');
  btn.classList.remove('arming');
  btn.textContent = '确认卸载';
}

function arm() {
  armed = true;
  const btn = $('btn-go');
  btn.classList.add('arming');
  btn.textContent = '真的要卸载？再点一次';
  $('tb-status').textContent = '等待确认';
  armTimer = setTimeout(disarm, 5000);   // 5 秒不点就自动撤销
}

function quit() {
  fetch('/api/quit', { method: 'POST' }).catch(() => {});
}

async function doUninstall() {
  const btn = $('btn-go');
  btn.disabled = true;
  $('btn-cancel').disabled = true;
  btn.textContent = '正在卸载…';
  $('tb-status').textContent = '卸载中…';
  $('foot').className = 'uni-foot';
  $('foot').textContent = '正在停后台抢网、清自启、删文件，请稍候…';

  let r;
  try {
    r = await call('run', [$('ck-config').checked, $('ck-logs').checked]);
  } catch (err) {
    btn.disabled = false;
    $('btn-cancel').disabled = false;
    disarm();
    toast('和卸载器后台断了联系：' + err.message, 'err', 8000);
    return;
  }

  if (!r.ok) {
    btn.disabled = false;
    $('btn-cancel').disabled = false;
    disarm();
    toast(r.message || '卸载失败', 'err', 8000);
    return;
  }

  finished = true;
  renderResult(r);
}

/* ---------------------------------------------------------- 初始化 */
$('btn-go').addEventListener('click', () => {
  if (finished) { quit(); return; }
  if (!armed) { arm(); return; }
  disarm();
  doUninstall();
});

$('btn-cancel').addEventListener('click', quit);

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    if (armed) { disarm(); } else { quit(); }
  }
});

(async () => {
  try {
    const s = await call('scan');
    if (!s.ok) {
      toast(s.message || '读取现状失败', 'err', 8000);
      $('tb-status').textContent = '读取失败';
      $('foot').textContent = s.message || '';
      return;
    }
    renderList(s);
  } catch (err) {
    $('tb-status').textContent = '读取失败';
    $('foot').textContent = '读取现状失败：' + err.message;
  }
})();
