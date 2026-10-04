'use strict';
const $ = (id) => document.getElementById(id);
let token = new URLSearchParams(location.hash.slice(1)).get('token');
try {
  if (token) sessionStorage.setItem('rdp-management-token', token);
  else token = sessionStorage.getItem('rdp-management-token');
} catch (_) {}
history.replaceState(null, '', location.pathname);
let working = false;
let timer;
function notice(message, error = false) {
  $('notice').hidden = !message;
  $('notice').textContent = message;
  $('notice').classList.toggle('error', error);
}
async function api(path, data) {
  if (!token) throw new Error('请使用终端输出的完整向导链接打开此页面。');
  let response;
  try {
    response = await fetch(path, {method: data === undefined ? 'GET' : 'POST', cache: 'no-store',
      headers: {Authorization: `Bearer ${token}`, 'Content-Type': 'application/json'},
      body: data === undefined ? undefined : JSON.stringify(data), signal: AbortSignal.timeout(15000)});
  } catch (_) { throw new Error('无法连接向导。请保持终端运行；任务可能仍在执行，刷新页面查看结果。'); }
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || '操作失败。');
  return value;
}
function render(value) {
  working = ['preparing', 'installing'].includes(value.phase);
  $('deploy-form').hidden = ['ready', 'installing', 'complete'].includes(value.phase);
  $('fields').disabled = working || ['ready', 'complete'].includes(value.phase);
  $('phase').textContent = {idle:'等待填写信息', preparing:'正在准备', ready:'等待确认', installing:'正在安装', complete:'部署完成', failed:'需要处理'}[value.phase];
  $('events').replaceChildren(...value.events.map((message) => { const li = document.createElement('li'); li.textContent = message; return li; }));
  $('task-error').hidden = !value.error;
  $('task-error').textContent = value.error || '';
  $('review').hidden = !['ready', 'installing'].includes(value.phase);
  $('install').disabled = value.phase !== 'ready';
  $('complete').hidden = value.phase !== 'complete';
  const plan = value.plan;
  $('plan').textContent = [`认证入口：https://${value.hostname}`, `RDP 地址：${value.configuration.rdp_address || ''}`, `SakuraFrp 隧道：${value.configuration.tunnel_id || ''}`, `本机端口：${plan.port}`, `配置：${plan.config}`, `词库：${plan.wordlist}`, `连接器：${plan.connector}`, ...plan.units.map((unit) => `服务：${unit}`)].join('\n');
  if (value.phase === 'complete') $('site-url').textContent = 'https://' + value.hostname;
  if (value.error) notice(value.error, true);
  if (['idle', 'failed'].includes(value.phase) && plan.conflicts.length) {
    if (!value.error) notice('检测到已有部署，向导不会覆盖：' + plan.conflicts.join('、'), true);
    $('fields').disabled = true;
  }
  clearTimeout(timer);
  if (working) timer = setTimeout(poll, 1000);
}
async function poll() {
  try { render(await api('/api/deployment')); }
  catch (error) { notice(error.message, true); if (working) timer = setTimeout(poll, 3000); }
}
$('listen-port').addEventListener('input', () => { $('route').textContent = 'http://127.0.0.1:' + $('listen-port').value; });
$('deploy-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (working) return;
  if ($('password').value !== $('password-confirm').value) { notice('两次密码输入不一致。', true); return; }
  const changes = {hostname:$('hostname').value.trim(), rdp_address:$('rdp-address').value.trim(), tunnel_id:Number($('tunnel-id').value), password:$('password').value, sakura_token:$('sakura-token').value.trim(), turnstile_site_key:$('site-key').value.trim(), turnstile_secret_key:$('secret-key').value.trim()};
  changes.disable_turnstile = !changes.turnstile_site_key && !changes.turnstile_secret_key;
  if ($('words-path').value.trim()) changes.wordlist_path = $('words-path').value.trim();
  working = true; $('fields').disabled = true; notice('正在校验并准备部署文件…');
  try {
    render(await api('/api/deploy/prepare', {changes, tunnel_token:$('cloudflare-token').value.trim(), port:Number($('listen-port').value)}));
    // The server owns the prepared secrets; never persist them in browser storage.
    for (const key of ['password', 'password-confirm', 'sakura-token', 'cloudflare-token', 'secret-key']) $(key).value = '';
  } catch (error) { working = false; $('fields').disabled = false; notice(error.message, true); }
});
$('install').addEventListener('click', async () => {
  if (working) return;
  working = true; $('install').disabled = true; notice('正在部署，请保持终端运行。');
  try { render(await api('/api/deploy/apply', {})); }
  catch (error) { notice(error.message, true); await poll(); }
});
poll();
