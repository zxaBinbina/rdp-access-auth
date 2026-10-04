'use strict';
const $ = (id) => document.getElementById(id);
const requestedView = new URLSearchParams(location.search).get('view');
let token = new URLSearchParams(location.hash.slice(1)).get('token');
try {
  if (token) sessionStorage.setItem('rdp-management-token', token);
  else token = sessionStorage.getItem('rdp-management-token');
} catch (_) { /* A fragment token still works when session storage is unavailable. */ }
history.replaceState(null, '', location.pathname);
let current = null;
let formRevision = null;
let initialFormValues = null;
let formCredentials = {};
let dirty = false;
let busy = false;
let refreshing = false;
let migrationBusy = false;
let migrationPlan = null;
let migrationTimer;
function setMenu(open, restoreFocus = false) {
  $('mobile-nav').hidden = !open;
  $('mobile-nav').inert = !open;
  $('menu-toggle').setAttribute('aria-expanded', String(open));
  $('menu-toggle').setAttribute('aria-label', open ? '关闭菜单' : '打开菜单');
  if (restoreFocus) $('menu-toggle').focus();
}
$('menu-toggle').addEventListener('click', () => setMenu($('mobile-nav').hidden));
$('mobile-nav').addEventListener('click', (event) => {
  if (event.target.closest('a')) setMenu(false);
});
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && !$('mobile-nav').hidden) setMenu(false, true);
});
document.addEventListener('click', (event) => {
  if (!$('site-header').contains(event.target)) setMenu(false);
});
matchMedia('(min-width: 761px)').addEventListener('change', (event) => {
  if (event.matches) setMenu(false);
});
const sections = ['overview', 'configuration', 'maintenance'].map($);
const sectionLinks = document.querySelectorAll('[data-section-link]');
sections.forEach((section) => section.setAttribute('tabindex', '-1'));
let scrollFrame;
function updateNavigation() {
  const atBottom = Math.ceil(scrollY + innerHeight) >= document.documentElement.scrollHeight - 2;
  const section = atBottom ? sections.at(-1) : sections.filter((item) => item.getBoundingClientRect().top <= 160).at(-1) || sections[0];
  sectionLinks.forEach((link) => {
    if (link.dataset.sectionLink === section.id) link.setAttribute('aria-current', 'location');
    else link.removeAttribute('aria-current');
  });
  scrollFrame = null;
}
window.addEventListener('scroll', () => {
  if (!scrollFrame) scrollFrame = requestAnimationFrame(updateNavigation);
}, {passive: true});
updateNavigation();
if ('IntersectionObserver' in window) {
  const observer = new IntersectionObserver((entries) => entries.forEach((entry) => {
    if (entry.isIntersecting) {
      entry.target.classList.add('section-arrival');
      observer.unobserve(entry.target);
    }
  }), {threshold: 0.05});
  document.querySelectorAll('.content-section').forEach((section) => observer.observe(section));
}
function renderReveal(button, show) {
  button.querySelector('use').setAttribute('href', `/icons.svg#${show ? 'eye-off' : 'eye'}`);
  button.setAttribute('aria-pressed', String(show));
  const fieldLabel = document.querySelector(`label[for="${button.dataset.reveal}"]`).childNodes[0].textContent.trim();
  button.setAttribute('aria-label', `${show ? '隐藏' : '显示'}${fieldLabel}`);
  button.title = button.getAttribute('aria-label');
}

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
  $('turnstile_secret_key').required = enabled;
  $('turnstile_site_key').disabled = !enabled;
  $('turnstile_secret_key').disabled = !enabled;
}
function formValues() {
  return JSON.stringify(['hostname', 'rdp_address', 'tunnel_id', 'wordlist_path', 'sakura_token',
    'password', 'password-confirm', 'turnstile_site_key', 'turnstile_secret_key'].map((key) => $(key).value)
    .concat($('turnstile-enabled').checked));
}
function updateDirty() {
  dirty = initialFormValues !== null && formValues() !== initialFormValues;
  $('save-hint').textContent = dirty ? '有尚未保存的修改' : '保存时保留会话密钥和现有凭据';
}
function setPasswordEditing(editing) {
  $('password-saved').hidden = editing;
  $('password-editor').hidden = !editing;
  $('password-cancel').hidden = !editing || !current?.config?.has_password;
  for (const key of ['password', 'password-confirm']) {
    $(key).disabled = !editing;
    $(key).required = editing;
  }
}
$('password-change').addEventListener('click', () => {
  setPasswordEditing(true);
  $('password').focus();
});
$('password-cancel').addEventListener('click', () => {
  for (const key of ['password', 'password-confirm']) { $(key).value = ''; $(key).type = 'password'; }
  renderReveal(document.querySelector('[data-reveal=password]'), false);
  setPasswordEditing(false);
  updateDirty();
  $('password-change').focus();
});
function controls() {
  $('config-fields').disabled = busy || migrationBusy || refreshing || !current || !!current.config_error;
  $('refresh').disabled = busy || refreshing;
  $('refresh').classList.toggle('is-refreshing', refreshing);
  $('refresh').setAttribute('aria-busy', String(refreshing));
  $('loading-status').hidden = !refreshing;
  $('runtime-stats').setAttribute('aria-busy', String(refreshing));
  const serviceAvailable = current?.target.profile !== 'local' && current?.service?.LoadState === 'loaded';
  document.querySelectorAll('[data-service]').forEach((button) => { button.disabled = busy || migrationBusy || !serviceAvailable; });
  $('show-logs').disabled = busy || !serviceAvailable;
  $('unlock').disabled = busy || migrationBusy || !current?.state;
}
function renderDeployment() {
  if (!current) return;
  const local = current.target.profile === 'local';
  const workdir = current.service?.WorkingDirectory;
  const packaged = current.service?.LoadState === 'loaded' && workdir === '/usr/lib/rdp-access-auth';
  // A migrated service keeps its original profile, paths and name.
  const legacy = !packaged && current.service?.LoadState === 'loaded' && workdir === current.target.runtime;
  $('profile-label').textContent = local ? '项目配置' : packaged ? '软件包部署' : legacy ? '旧版部署' : '系统部署';
  $('migration-card').hidden = local || !(legacy || migrationBusy);
}
function render(value, populate) {
  current = value;
  $('config-path').textContent = value.target.config;
  $('state-path').textContent = value.target.state;
  $('service-name').textContent = value.target.service + (value.target.scope === 'user' ? '（用户服务）' : '');
  const local = value.target.profile === 'local';
  $('local-instructions').hidden = !local;
  renderDeployment();
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
    for (const key of ['password', 'password-confirm']) { $(key).value = ''; $(key).type = 'password'; }
    formCredentials = {};
    for (const key of ['sakura_token', 'turnstile_secret_key']) {
      formCredentials[key] = value.config[key] || '';
      $(key).value = formCredentials[key];
      $(key).type = 'password';
    }
    document.querySelectorAll('[data-reveal]').forEach((button) => renderReveal(button, false));
    setPasswordEditing(!value.config.has_password);
    $('sakura_token').required = true;
    $('password-state').textContent = value.config.has_password ? '已配置' : '首次设置';
    $('token-state').textContent = value.config.has_sakura_token ? '已配置' : '首次设置';
    $('turnstile-secret-state').textContent = value.config.has_turnstile_secret ? '已配置' : '';
    $('turnstile-enabled').checked = !!value.config.turnstile_site_key;
    turnstileFields();
    initialFormValues = formValues();
    dirty = false;
    $('save-hint').textContent = '保存时保留会话密钥和现有凭据';
  }
  controls();
}
async function refresh(populate = false) {
  refreshing = true;
  controls();
  try {
    const value = await api(populate ? '/api/config' : '/api/status');
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
$('config-form').addEventListener('input', updateDirty);
$('turnstile-enabled').addEventListener('change', turnstileFields);
document.querySelectorAll('[data-reveal]').forEach((button) => button.addEventListener('click', () => {
  const field = $(button.dataset.reveal); const show = field.type === 'password';
  field.type = show ? 'text' : 'password'; renderReveal(button, show);
}));
$('config-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (busy || !current?.config) return;
  if ($('password').value !== $('password-confirm').value) { notice('两次输入的固定密码不一致。', true); $('password-confirm').focus(); return; }
  const changes = {};
  for (const key of ['hostname', 'rdp_address', 'wordlist_path']) changes[key] = $(key).value.trim();
  changes.tunnel_id = Number($('tunnel_id').value);
  if (!$('password').disabled && $('password').value) changes.password = $('password').value;
  if ($('sakura_token').value !== formCredentials.sakura_token) changes.sakura_token = $('sakura_token').value;
  changes.disable_turnstile = !$('turnstile-enabled').checked;
  if (!changes.disable_turnstile) {
    changes.turnstile_site_key = $('turnstile_site_key').value.trim();
    if ($('turnstile_secret_key').value !== formCredentials.turnstile_secret_key) changes.turnstile_secret_key = $('turnstile_secret_key').value;
  }
  const savedCredentials = {sakura_token: $('sakura_token').value,
    turnstile_secret_key: changes.disable_turnstile ? '' : $('turnstile_secret_key').value};
  busy = true; controls();
  try {
    const result = await api('/api/config', {revision: formRevision, changes});
    // Restore the saved view and hide the new password even if the follow-up read fails.
    render({...current, config: {...result.config, ...savedCredentials}, revision: result.revision, exists: true}, true);
    await refresh(true);
    notice(result.message + (result.hostname_changed ? ' 认证域名已改变，请同步 Tunnel 配置并重新绑定通行密钥。' : ''));
  } catch (error) { notice(error.message, true); }
  finally { busy = false; controls(); }
});
$('refresh').addEventListener('click', async () => {
  if (dirty && !await confirmAction('重新载入配置', '当前有尚未保存的修改，重新载入将丢弃这些修改。')) return;
  notice('');
  try { await refresh(true); await loadMigration(); } catch (error) { notice(error.message, true); }
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
function renderMigration(value) {
  migrationBusy = value.phase === 'running' || !!value.recovery_required;
  migrationPlan = value.plan || {};
  renderDeployment();
  $('migration-description').textContent = value.message || migrationPlan.message || (value.phase === 'running' ? '正在备份并切换程序，请保持终端运行。' : '');
  $('migration-plan').hidden = !migrationPlan.available;
  $('migration-source').textContent = migrationPlan.current_program || '';
  $('migration-service').textContent = migrationPlan.service ? `${migrationPlan.service} · ${migrationPlan.port}` : '';
  $('migration-backup').textContent = value.backup || migrationPlan.backup_directory || '';
  $('migration-apply').hidden = !migrationPlan.available || migrationBusy;
  $('migration-check').disabled = busy || migrationBusy;
  $('migration-recover').hidden = !value.recovery_required;
  $('migration-recover').disabled = value.phase === 'running';
  $('migration-events').replaceChildren(...(value.events || []).map((message) => { const li = document.createElement('li'); li.textContent = message; return li; }));
  $('migration-error').hidden = !value.error;
  $('migration-error').textContent = value.error || '';
  controls();
  clearTimeout(migrationTimer);
  if (value.phase === 'running') migrationTimer = setTimeout(loadMigration, 1200);
}
async function loadMigration() {
  try {
    const wasBusy = migrationBusy;
    renderMigration(await api('/api/migration'));
    if (wasBusy && !migrationBusy) await refresh(false);
  } catch (error) {
    notice(error.message, true);
    if (migrationBusy) migrationTimer = setTimeout(loadMigration, 3000);
  }
}
$('migration-check').addEventListener('click', loadMigration);
async function migrate(recover = false) {
  if (busy) return;
  if (dirty) return notice('请先保存或重新载入当前配置，再迁移。', true);
  const accepted = await confirmAction(recover ? '恢复原服务' : '迁移现有部署', recover
    ? '恢复迁移前的程序和数据库。原先运行的服务会重新启动。'
    : '将备份当前凭据和数据库，切换到已安装的软件包程序并进行健康检查。认证页面会短暂中断；检查失败时恢复原服务。');
  if (!accepted) return;
  busy = true; controls();
  try {
    renderMigration(await api(recover ? '/api/migration/recover' : '/api/migration/apply', {revision:migrationPlan?.revision}));
    if (!migrationBusy) await refresh(false);
  }
  catch (error) { notice(error.message, true); await loadMigration(); }
  finally { busy = false; controls(); }
}
$('migration-apply').addEventListener('click', () => migrate());
$('migration-recover').addEventListener('click', () => migrate(true));
refresh(true).then(loadMigration).then(() => {
  if (requestedView === 'migrate') {
    if (!$('migration-card').hidden) {
      $('migration-card').scrollIntoView({block:'start'});
      const action = !$('migration-recover').hidden ? $('migration-recover') : $('migration-check');
      if (!action.disabled) action.focus({preventScroll:true});
    } else if (current?.service?.WorkingDirectory === '/usr/lib/rdp-access-auth') {
      notice('当前已使用软件包部署，无需迁移。');
    } else {
      notice('当前未检测到需要迁移的旧版部署。');
    }
  } else if (requestedView === 'logs') {
    $('show-logs').scrollIntoView({block:'center'});
    $('show-logs').focus({preventScroll:true});
    if (!$('show-logs').disabled) $('show-logs').click();
  } else if (requestedView === 'deploy') {
    notice('已检测到现有部署，已为你打开管理页面。');
  }
}).catch((error) => { $('config-state').textContent = '未连接'; $('config-detail').textContent = '请检查终端中的管理服务'; notice(error.message, true); });
