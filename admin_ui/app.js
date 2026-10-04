'use strict';
const $ = (id) => document.getElementById(id);
let token = new URLSearchParams(location.hash.slice(1)).get('token');
try {
  if (token) sessionStorage.setItem('rdp-management-token', token);
  else token = sessionStorage.getItem('rdp-management-token');
} catch (_) { /* A fragment token still works when session storage is unavailable. */ }
history.replaceState(null, '', location.pathname);
let current = null;
let formRevision = null;
let dirty = false;
let busy = false;
let refreshing = false;
try { const theme = localStorage.getItem('rdp-management-theme'); if (['dark', 'light'].includes(theme)) document.documentElement.dataset.theme = theme; } catch (_) {}
$('theme').addEventListener('click', () => {
  const dark = document.documentElement.dataset.theme ? document.documentElement.dataset.theme === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches;
  const theme = dark ? 'light' : 'dark';
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem('rdp-management-theme', theme); } catch (_) {}
});

function notice(message, error = false) {
  $('notice').textContent = message;
  $('notice').classList.toggle('error', error);
  $('notice').hidden = !message;
}
async function api(path, data) {
  if (!token) throw new Error('请使用终端输出的完整管理链接打开此页面。');
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 55000);
  try {
    const response = await fetch(path, {method: data === undefined ? 'GET' : 'POST', cache: 'no-store',
      headers: {'Authorization': `Bearer ${token}`, ...(data === undefined ? {} : {'Content-Type': 'application/json'})},
      body: data === undefined ? undefined : JSON.stringify(data), signal: controller.signal});
    const value = await response.json();
    if (!response.ok) throw new Error(value.error || '操作失败，请重试。');
    return value;
  } catch (error) {
    if (error.name === 'AbortError') throw new Error('操作超时，请刷新状态确认结果。');
    if (error instanceof TypeError) throw new Error('无法连接管理服务，请确认终端中的 ./rdp-auth gui 仍在运行。');
    throw error;
  } finally { clearTimeout(timeout); }
}
function turnstileFields() {
  const enabled = $('turnstile-enabled').checked;
  $('turnstile-fields').hidden = !enabled;
  $('turnstile-off').hidden = enabled;
  $('turnstile_site_key').required = enabled;
  $('turnstile_secret_key').required = enabled && !current?.config?.has_turnstile_secret;
  $('turnstile_site_key').disabled = !enabled;
  $('turnstile_secret_key').disabled = !enabled;
}
function controls() {
  $('config-fields').disabled = busy || refreshing || !current || !!current.config_error;
  $('refresh').disabled = busy || refreshing;
  const serviceAvailable = current?.target.profile !== 'local' && current?.service?.LoadState === 'loaded';
  document.querySelectorAll('[data-service]').forEach((button) => { button.disabled = busy || !serviceAvailable; });
  $('show-logs').disabled = busy || !serviceAvailable;
  $('unlock').disabled = busy || !current?.state;
}
function render(value, populate) {
  current = value;
  $('config-path').textContent = value.target.config;
  $('state-path').textContent = value.target.state;
  $('service-name').textContent = value.target.service + (value.target.scope === 'user' ? '（用户服务）' : '');
  const local = value.target.profile === 'local';
  $('profile-label').textContent = local ? '项目配置' : value.target.profile === 'legacy' ? '旧版部署' : '系统部署';
  $('target-description').textContent = local ? '当前只管理项目内的配置。此模式不控制已部署的系统服务。' : '当前管理显式选定的系统部署，请核对以下路径。';
  $('config-state').textContent = value.config_error ? '无法读取' : value.validation_error ? '待修复' : value.exists ? '已配置' : '待配置';
  $('config-detail').textContent = value.exists ? '可在下方修改并保存' : '填写连接信息和访问凭据';
  const active = value.service?.ActiveState;
  $('service-state').textContent = local ? '本地模式' : ({active:'运行中', inactive:'已停止', failed:'运行异常', activating:'启动中', deactivating:'停止中'}[active] || '不可用');
  $('service-detail').textContent = local ? '使用 serve 命令运行本项目' : value.target.service;
  $('service-help').textContent = local ? '在终端运行 ./rdp-auth serve 启动本项目。下方系统服务按钮仅在显式选定部署后可用。' : '保存配置后，点击重启使其生效。';
  $('runtime-dot').classList.toggle('good', active === 'active');
  $('service-error').hidden = !value.service_error;
  $('service-error').textContent = value.service_error || '';
  const state = value.state;
  $('guard-state').textContent = state ? (state.global_lock_seconds ? `锁定 ${state.global_lock_seconds} 秒` : state.banned_ips ? `${state.banned_ips} 个 IP 封禁` : '保护正常') : '尚未就绪';
  $('guard-detail').textContent = state ? '固定密码 / 临时密码 / 通行密钥' : '启动认证服务后生成状态';
  $('passkey-count').textContent = state?.passkeys ?? '—';
  $('temporary-generation').textContent = state?.temporary_generation ?? '—';
  $('state-error').textContent = value.state_error || '';
  $('state-error').hidden = !value.state_error;
  $('wordlist-state').textContent = value.wordlist?.ok ? `${value.wordlist.count.toLocaleString()} 个词 · 已就绪` : '词库待准备';
  $('wordlist-dot').classList.toggle('good', !!value.wordlist?.ok);
  $('wordlist-path').textContent = value.wordlist?.path || '配置可读后显示词库路径';
  if (populate && value.config) {
    formRevision = value.revision;
    for (const key of ['hostname', 'rdp_address', 'tunnel_id', 'wordlist_path', 'turnstile_site_key']) $(key).value = value.config[key] ?? '';
    for (const key of ['password', 'sakura_token', 'turnstile_secret_key', 'password-confirm']) { $(key).value = ''; $(key).type = 'password'; }
    document.querySelectorAll('[data-reveal]').forEach((button) => { button.textContent = '显示'; button.setAttribute('aria-pressed', 'false'); });
    $('password').required = !value.config.has_password;
    $('password-confirm').required = !value.config.has_password;
    $('sakura_token').required = !value.config.has_sakura_token;
    $('password-state').textContent = value.config.has_password ? '已配置' : '首次设置';
    $('token-state').textContent = value.config.has_sakura_token ? '已配置' : '首次设置';
    $('turnstile-secret-state').textContent = value.config.has_turnstile_secret ? '已配置' : '';
    $('turnstile-enabled').checked = !!value.config.turnstile_site_key;
    turnstileFields();
    dirty = false;
    $('save-hint').textContent = '保存时保留会话密钥和现有凭据';
  }
  controls();
}
async function refresh(populate = false) {
  refreshing = true;
  controls();
  try {
    const value = await api('/api/status');
    render(value, populate);
    if (value.config_error || value.validation_error) notice(value.config_error || value.validation_error, true);
  } finally { refreshing = false; controls(); }
}
function confirmAction(title, message) {
  return new Promise((resolve) => {
    const dialog = $('confirm-dialog');
    $('confirm-title').textContent = title;
    $('confirm-text').textContent = message;
    const finish = (value) => { dialog.close(); resolve(value); };
    $('confirm-ok').onclick = () => finish(true);
    $('confirm-cancel').onclick = () => finish(false);
    dialog.oncancel = (event) => { event.preventDefault(); finish(false); };
    dialog.showModal();
    $('confirm-cancel').focus();
  });
}
$('config-form').addEventListener('input', () => { dirty = true; $('save-hint').textContent = '有尚未保存的修改'; });
$('turnstile-enabled').addEventListener('change', turnstileFields);
document.querySelectorAll('[data-reveal]').forEach((button) => button.addEventListener('click', () => {
  const field = $(button.dataset.reveal); const show = field.type === 'password';
  field.type = show ? 'text' : 'password'; button.textContent = show ? '隐藏' : '显示'; button.setAttribute('aria-pressed', String(show));
}));
$('config-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (busy || !current?.config) return;
  if ($('password').value !== $('password-confirm').value) { notice('两次输入的固定密码不一致。', true); $('password-confirm').focus(); return; }
  const changes = {};
  for (const key of ['hostname', 'rdp_address', 'wordlist_path']) changes[key] = $(key).value.trim();
  changes.tunnel_id = Number($('tunnel_id').value);
  for (const key of ['password', 'sakura_token']) if ($(key).value) changes[key] = $(key).value;
  changes.disable_turnstile = !$('turnstile-enabled').checked;
  if (!changes.disable_turnstile) {
    changes.turnstile_site_key = $('turnstile_site_key').value.trim();
    if ($('turnstile_secret_key').value) changes.turnstile_secret_key = $('turnstile_secret_key').value;
  }
  busy = true; controls();
  try {
    const result = await api('/api/config', {revision: formRevision, changes});
    // Clear secrets immediately after a confirmed save, even if the next status request fails.
    for (const key of ['password', 'password-confirm', 'sakura_token', 'turnstile_secret_key']) $(key).value = '';
    dirty = false;
    await refresh(true);
    notice(result.message + (result.hostname_changed ? ' 认证域名已改变，请同步 Tunnel 配置并重新绑定通行密钥。' : ''));
  } catch (error) { notice(error.message, true); }
  finally { busy = false; controls(); }
});
$('refresh').addEventListener('click', async () => {
  if (dirty && !await confirmAction('重新载入配置', '当前有尚未保存的修改，重新载入将丢弃这些修改。')) return;
  notice('');
  try { await refresh(true); } catch (error) { notice(error.message, true); }
});
async function runAction(path, data, title, message) {
  if (busy || !await confirmAction(title, message)) return;
  busy = true; controls();
  try { const result = await api(path, data); await refresh(false); notice(result.message); }
  catch (error) { notice(error.message, true); }
  finally { busy = false; controls(); }
}
document.querySelectorAll('[data-service]').forEach((button) => button.addEventListener('click', () => {
  const action = button.dataset.service; const label = {start:'启动', stop:'停止', restart:'重启'}[action];
  runAction('/api/service', {action}, label + '认证服务', `将${label} ${current.target.service}。${action === 'start' ? '' : '这会暂时中断认证页面的访问。'}${dirty ? '当前表单尚未保存，服务只会使用已保存的配置。' : ''}`);
}));
$('unlock').addEventListener('click', () => runAction('/api/unlock', {}, '解除认证封禁', '将清除当前数据库中的 IP 封禁和全站锁定，保留固定密码、临时密码和通行密钥。'));
$('show-logs').addEventListener('click', async () => {
  busy = true; controls();
  try { const result = await api('/api/logs'); $('logs').textContent = result.output || '暂无日志。'; $('logs').hidden = false; }
  catch (error) { notice(error.message, true); }
  finally { busy = false; controls(); }
});
window.addEventListener('beforeunload', (event) => { if (dirty) { event.preventDefault(); event.returnValue = ''; } });
refresh(true).catch((error) => { $('config-state').textContent = '未连接'; $('config-detail').textContent = '请检查终端中的管理服务'; notice(error.message, true); });
