/* The window has no HTTP management endpoint. Every mutation crosses the native bridge. */
(() => {
  const root = document.getElementById('app');
  const dialog = document.getElementById('dialog');
  const state = {view: 'overview', step: 0, unlocked: false, status: {}, settings: null, agents: null,
    inventory: null, guide: null, draft: {mode: 'automatic', rdp_port: 3389, admission_port: 13389, portal_port: 18089}, exported: null, logs: '', error: ''};
  const waiting = new Map();
  let bridge, nextId = 1, toastTimer, unlocking;
  const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
  const icon = name => `<svg class="icon" aria-hidden="true"><use href="icons.svg#${esc(name)}"></use></svg>`;
  const button = (label, action, style = '', extra = '') => `<button type="button" class="button ${style}" data-action="${action}" ${extra}>${label}</button>`;
  const submit = (label, style = 'primary') => `<button class="button ${style}" type="submit">${label}${icon('arrow-right')}</button>`;
  const field = (label, name, value = '', options = {}) => `<div class="field ${options.full ? 'full' : ''}"><label for="field-${name}">${label}</label>${options.type === 'password' ? '<div class="secret-field">' : ''}<input id="field-${name}" name="${name}" type="${options.type || 'text'}" value="${esc(value)}" ${options.required ? 'required' : ''} ${options.type === 'number' ? `min="1" max="${name.includes('tunnel')?2147483647:name==='duration_seconds'?86400:65535}"` : ''} ${options.type === 'password' ? 'autocomplete="new-password"' : ''} placeholder="${esc(options.placeholder || '')}">${options.type === 'password' ? `<button class="icon-button" type="button" data-eye="field-${name}" aria-label="显示或隐藏${esc(label)}">${icon('eye')}</button></div>` : ''}${options.hint ? `<small>${options.hint}</small>` : ''}</div>`;
  const select = (label, name, value, choices, full = false) => `<div class="field ${full ? 'full' : ''}"><label for="field-${name}">${label}</label><select id="field-${name}" name="${name}">${choices.map(item => `<option value="${esc(item.value)}" ${String(value ?? '') === String(item.value) ? 'selected' : ''} ${item.disabled ? 'disabled' : ''}>${esc(item.label)}</option>`).join('')}</select></div>`;
  const check = (name, label, checked = false) => `<label class="check"><input type="checkbox" name="${name}" ${checked ? 'checked' : ''}><span>${label}</span></label>`;
  const card = (title, body, subtitle = '') => `<section class="card"><div class="card-head"><div><h2>${title}</h2>${subtitle ? `<p>${subtitle}</p>` : ''}</div></div>${body}</section>`;
  const title = (name, text, eyebrow) => `<p class="eyebrow">${eyebrow}</p><h1>${name}</h1><p class="lead">${text}</p>`;
  const form = (name, body) => `<form data-form="${name}">${body}<p class="inline-error" role="alert"></p></form>`;
  const data = form => {
    const output = {};
    for (const input of form.elements) {
      if (!input.name || input.disabled || input.type === 'radio' && !input.checked) continue;
      if (input.type === 'checkbox') output[input.name] = input.checked;
      else if (input.type === 'number') {if (input.value !== '') output[input.name] = Number(input.value);}
      else output[input.name] = input.type === 'password' ? input.value : input.value.trim();
    }
    return output;
  };
  const choices = (items, label = '请选择') => [{value:'', label}, ...items];
  const when = time => time ? new Date(time * 1000).toLocaleString('zh-CN', {hour12: false}) : '尚未授权';
  function toast(message) {
    const target = document.getElementById('toast');
    target.textContent = message; target.hidden = false;
    clearTimeout(toastTimer); toastTimer = setTimeout(() => {target.hidden = true;}, 5000);
  }
  function rpc(method, params = {}, confirmation) {
    return new Promise((resolve, reject) => {
      const id = nextId++;
      const timeout = setTimeout(() => {waiting.delete(id); reject({message: '操作响应超时，请刷新状态后确认结果。', code: 504});}, 200000);
      waiting.set(id, {resolve, reject, timeout});
      bridge.request(JSON.stringify({id, method, params, confirmation}));
    });
  }
  function ask(options) {
    if (dialog.open) return Promise.resolve(null);
    return new Promise(resolve => {
      let answer = null;
      dialog.innerHTML = `<form id="dialog-form"><p class="eyebrow">${options.danger ? 'CONFIRM ACTION' : 'RDP ACCESS AUTH'}</p><h2 id="dialog-title">${esc(options.title)}</h2><p>${esc(options.text)}</p>${options.password ? '<input type="password" name="password" aria-label="管理密码" placeholder="输入管理密码" autocomplete="current-password" required>' : ''}${options.checkbox ? check('checked', options.checkbox, options.checked) : ''}<p class="inline-error" role="alert"></p><div class="actions"><button class="button quiet" type="button" id="dialog-cancel">取消</button><button class="button ${options.danger ? 'danger' : 'primary'}" type="submit">${esc(options.confirm || '确认')}</button></div></form>`;
      dialog.onclose = () => {dialog.innerHTML = ''; resolve(answer);};
      document.getElementById('dialog-cancel').onclick = () => dialog.close();
      document.getElementById('dialog-form').onsubmit = async event => {
        event.preventDefault();
        const formElement = event.currentTarget;
        const submitButton = formElement.querySelector('[type=submit]'); submitButton.disabled = true;
        try {
          const values = data(formElement);
          answer = options.validate ? await options.validate(values) : values;
          dialog.close();
        } catch (error) {formElement.querySelector('.inline-error').textContent = error.message || '操作失败，请重试。';}
        finally {submitButton.disabled = false;}
      };
      dialog.showModal();
    });
  }
  async function unlock() {
    if (state.unlocked) return true;
    if (unlocking) return unlocking;
    unlocking = ask({title: '解锁管理设置', text: '输入管理密码后，可以在当前窗口会话中修改普通设置。', password: true, confirm: '解锁',
      validate: async values => {await rpc('security.login', {password: values.password}); state.unlocked = true; updateLock(); return true;}});
    const result = await unlocking; unlocking = null;
    return !!result;
  }
  async function api(method, params = {}, confirmation) {
    try {return await rpc(method, params, confirmation);}
    catch (error) {
      if (error.code === 401 && !method.startsWith('security.')) {
        state.unlocked = false;
        if (await unlock()) return rpc(method, params, confirmation);
        throw {message: '已取消解锁。', code: 499};
      }
      throw error;
    }
  }
  async function dangerous(method, params, description) {
    if (!await unlock()) return null;
    return ask({title: '确认危险操作', text: description, password: true, danger: true, confirm: '确认执行',
      validate: async values => {
        const permission = await rpc('security.confirm', {password: values.password, method, params});
        return rpc(method, params, permission.confirmation);
      }});
  }
  function updateLock() {
    const target = document.getElementById('lock-label');
    if (target) target.textContent = state.unlocked ? '设置已解锁' : '设置已锁定';
  }
  function shell(content) {
    const entries = [['overview','activity','运行概览'],['wizard','arrow-right','配置向导'],['settings','sliders-horizontal','认证与连接'],['credentials','key-round','访问凭据'],['agents','terminal','Agent 管理'],['maintenance','server','运行维护'],['appearance','sun','外观与启动']];
    return `<header class="topbar"><div class="brand"><img src="logo.png" alt="">RDP Access Auth <span class="version">2.0</span></div><div class="top-actions"><span class="lock-state" id="lock-label">${state.unlocked ? '设置已解锁' : '设置已锁定'}</span><button class="icon-button" data-action="unlock" aria-label="解锁或锁定设置">${icon(state.unlocked ? 'lock-open' : 'shield-check')}</button></div></header><div class="layout"><nav class="sidebar" aria-label="主要功能">${entries.filter(item => item[0] !== 'wizard' || !state.status.configured || state.status.deployment?.phase !== 'complete').map(([view, symbol, label]) => `<button data-view="${view}" ${state.view === view ? 'aria-current="page"' : ''}>${icon(symbol)}${label}</button>`).join('')}<div class="bottom"><button data-action="guide">${icon('book-open')}使用说明</button><small>连接之前，多一层安心。</small></div></nav><main class="content" id="main-content">${content}${state.error ? `<p class="notice error" role="alert">${esc(state.error)}</p>` : ''}</main></div>`;
  }
  function overview() {
    if (!state.status.configured) return welcome();
    const paused = state.status.preferences?.paused;
    const services = state.status.services || [];
    const names = ['访问认证', 'SakuraFrp', 'Cloudflare Tunnel'];
    const symbols = ['shield-check', 'server', 'globe'];
    const allUp = services.length === 3 && services.every(item => item.ActiveState === 'active');
    const config = state.status.config || {};
    return title(paused ? '暂时停下，随时继续。' : allUp ? '你的远程入口，已就绪。' : '检查你的远程入口。', '在这里查看连接状态，管理访问与后台服务。', 'OVERVIEW') +
      `<div class="service-grid">${services.map((service, index) => `<section class="service">${icon(symbols[index])}<h3>${names[index]}</h3><p class="status ${service.ActiveState === 'active' ? 'ok' : ''}">${paused ? '已暂停' : ({active:'运行中',inactive:'已停止',failed:'启动失败',unknown:'无法读取'})[service.ActiveState] || '未启动'}</p></section>`).join('')}</div>` +
      card('连接信息', `<dl><div><dt>认证入口</dt><dd class="address">https://${esc(config.hostname)}</dd></div><div><dt>远程桌面</dt><dd class="address">${esc(config.rdp_address)}</dd></div><div><dt>授权时长</dt><dd>6 小时 · 密码 / 临时密码 / 通行密钥</dd></div></dl><div class="actions">${button('打开认证页面'+icon('external-link'),'open-portal','primary')}${button(paused ? '继续服务' : '暂停服务','pause')}${button('重启服务','restart','quiet')}</div>`) +
      (paused ? '<p class="notice">暂停会跨重启保留；只有点击“继续服务”后才恢复。</p>' : '<p class="notice">完成网页认证后，仍需使用远程桌面的系统账户登录。</p>');
  }
  function welcome() {
    return `<section class="welcome"><p class="eyebrow">WELCOME TO 2.0</p><img src="logo.png" alt=""><h1>把你的桌面，<br><span class="gradient">放心带到远方。</span></h1><p class="lead">从账号准备到第一次连接，我们会一步一步带你完成。也可以把部署交给你信任的 Agent。</p><div class="welcome-list"><div>${icon('shield-check')}密码、临时密码和通行密钥，保护每一次访问</div><div>${icon('server')}统一管理认证与隧道，随时暂停或继续</div><div>${icon('terminal')}Agent 操作需经你授权，危险操作再次确认</div></div>${button('开始设置'+icon('arrow-right'),'begin','primary')}</section>`;
  }
  function wizard() {
    if (state.step === 0) return welcome();
    const names = ['欢迎','保护设置','部署方式','连接平台','配置网络','访问认证','检查计划','完成部署'];
    const d = state.draft;
    const stepper = `<div class="stepper" aria-label="第 ${state.step + 1} 步，共 8 步">${names.map((name, index) => `<span title="${name}" class="${index === state.step ? 'current' : index < state.step ? 'done' : ''}"></span>`).join('')}<small>${state.step + 1} / 8</small></div>`;
    let body = '';
    if (state.step === 1) {
      body = title('先保护你的管理设置。','管理密码用于修改软件设置和授权 Agent，与远程访问密码独立。','YOUR CONTROL') + card(state.status.initialized ? '解锁已有管理账户' : '创建管理密码', form('bootstrap', `<div class="form-grid">${field('管理密码','password','',{type:'password',full:true,required:true,hint:state.status.initialized ? '使用已经设置的管理密码。' : '12～128 个字符；请私密保管。'})}${state.status.initialized ? '' : field('再次输入管理密码','repeat_password','',{type:'password',full:true,required:true})}</div><p class="notice">首次初始化时，系统会请求 Linux 管理员认证，以注册受保护的本机管理服务。</p><div class="actions">${submit(state.status.initialized ? '解锁并继续' : '创建并继续')}</div>`));
    } else if (state.step === 2) {
      body = title('选择适合你的部署方式。','自动配置会减少官网上的重复操作，所有密钥都在本机保存。','SETUP METHOD') + card('怎样开始', form('mode', `<div class="radio-cards">${[['automatic','自动配置','提供 API 密钥后，由软件创建或配置隧道。','推荐'],['manual','手动引导','按步骤在官网准备，再由软件校验并安装本机组件。',''],['agent','交给 Agent','登记 MCP 身份，批准后让 Agent 帮你部署。','']].map(([value,label,text,tag])=>`<label class="choice"><input type="radio" name="mode" value="${value}" ${d.mode===value?'checked':''}><div><strong>${label}${tag?`<span class="tag">${tag}</span>`:''}</strong><p>${text}</p></div></label>`).join('')}</div><div class="actions">${button('上一步','previous','quiet')}${submit('继续')}</div>`));
    } else if (state.step === 3) {
      body = title('连接你的平台账户。','先准备 SakuraFrp 账户，以及已经托管到 Cloudflare 的域名。','CONNECT ACCOUNTS') +
        card('还没有准备好？', `<p>1. 注册 SakuraFrp，并按平台要求完成账户认证，取得“访问密钥”。</p><p>2. 在 Cloudflare 添加自己的域名，按提示修改域名的 DNS 服务器。</p><p>3. 自动配置需要 API Token：Cloudflare Tunnel 编辑、DNS 编辑、区域读取；自动创建 Turnstile 时再添加 Turnstile 编辑权限。</p><div class="actions">${button('SakuraFrp 控制台','link-sakura','compact')}${button('Cloudflare 控制台','link-cloudflare','compact')}${button('创建 API Token','link-cf-token','compact')}</div>`) +
        card(d.mode === 'manual' ? '填写现有凭据' : '验证 API 连接', form('platforms', `<div class="form-grid">${field('SakuraFrp 访问密钥','sakura_api_token','',{type:'password',full:true,placeholder:state.inventory?'已连接；留空继续使用':''})}${d.mode === 'automatic' ? field('Cloudflare API Token','cloudflare_api_token','',{type:'password',full:true,hint:'为最小必要的账户、域名授权，令牌需要能读取选定域名。'}) : field('Cloudflare Tunnel Token','cloudflare_tunnel_token',d.cloudflare_tunnel_token || '',{type:'password',full:true,hint:'只粘贴 Token，不粘贴完整的安装命令。'})}</div>${d.mode==='manual'?'<p class="notice">在 SakuraFrp 隧道自定义配置中，每行填写一个参数：<br>auth_mode = server<br>auth_time = 6h<br>proxy_protocol_version = v2<br>本地目标设为 127.0.0.1:13389。Cloudflare 路由指向 http://127.0.0.1:18089。</p>':''}<div class="actions">${button('上一步','previous','quiet')}${submit('验证并继续')}</div>`));
    } else if (state.step === 4) {
      const inv = state.inventory || {}; const cf = inv.cloudflare || {}; const sakura = inv.sakura || {};
      body = title('为连接选择一条路线。','认证页面使用你的子域名，远程桌面通过 SakuraFrp 连接。','NETWORK') + card('隧道与域名', form('network', `<div class="form-grid">${field('认证域名','hostname',d.hostname,{required:true,full:true,placeholder:'auth.example.com'})}${d.mode==='automatic' ?
        select('SakuraFrp 隧道','sakura_tunnel_id',d.sakura_tunnel_id,choices((sakura.tunnels||[]).map(t=>({value:t.id,label:t.name})), '创建新的 TCP 隧道')) +
        select('新隧道使用的节点','sakura_node_id',d.sakura_node_id,choices((sakura.nodes||[]).map(n=>({value:n.id,label:n.name+(n.available?'':'（当前不可创建）'),disabled:!n.available})))) +
        select('Cloudflare 账户','cf_account_id',d.cf_account_id,choices((cf.accounts||[]).map(a=>({value:a.id,label:a.name})))) +
        select('Cloudflare 域名','cf_zone_id',d.cf_zone_id,choices((cf.zones||[]).filter(z=>!d.cf_account_id||z.account_id===d.cf_account_id).map(z=>({value:z.id,label:z.name})))) : field('SakuraFrp 隧道 ID','sakura_tunnel_id',d.sakura_tunnel_id,{type:'number',required:true})}
        ${field('本机远程桌面端口','rdp_port',d.rdp_port,{type:'number',required:true,hint:'通常为 3389。KDE 远程桌面需由你自行启用。'})}</div><details><summary>高级网络选项</summary><div class="form-grid">${field('本机认证端口','portal_port',d.portal_port,{type:'number'})}${field('本机准入端口','admission_port',d.admission_port,{type:'number'})}${field('指定公网 RDP 地址（可选）','rdp_address',d.rdp_address,{full:true,placeholder:'留空时从 SakuraFrp 自动取得'})}${field('指定新隧道公网端口（可选）','remote_port',d.remote_port,{type:'number',hint:'留空由平台分配。'})}${d.mode==='automatic'?field('已有 Cloudflare 隧道 ID（可选）','cf_tunnel_id',d.cf_tunnel_id,{full:true,hint:'留空创建新隧道；使用已有隧道时保留它的其他路由。'}):''}</div></details><p class="notice">本机准入会核对真实来源 IP；公网隧道会启用 6 小时的服务端访问认证。</p><div class="actions">${button('上一步','previous','quiet')}${submit('继续')}</div>`));
    } else if (state.step === 5) {
      body = title('设置第一次访问的钥匙。','之后可在公网认证页面绑定 Passkey，也能使用自动轮换的临时密码。','ACCESS CREDENTIALS') + card('固定访问密码', form('access', `<div class="form-grid">${field('固定访问密码','password',d.password,{type:'password',required:true,full:true,hint:'16～128 位，包含大小写字母、数字和特殊符号。'})}${field('再次输入访问密码','repeat_password','',{type:'password',required:true,full:true})}</div>${d.mode==='automatic'?check('create_turnstile','同时创建 Cloudflare Turnstile 人机验证（API Token 需有对应权限）',d.create_turnstile):''}<details><summary>使用已有 Turnstile 或词库</summary><div class="form-grid">${field('Turnstile Site key','turnstile_site_key',d.turnstile_site_key)}${field('Turnstile Secret key','turnstile_secret_key',d.turnstile_secret_key,{type:'password'})}${field('已有词库路径（可选）','wordlist_path',d.wordlist_path,{full:true,hint:'留空自动下载并校验词库来源。'})}</div></details><div class="actions">${button('上一步','previous','quiet')}${submit('准备并检查')}</div>`));
    } else {
      const job = state.status.deployment || {}; const plan = job.plan || d;
      const complete = job.phase === 'complete';
      body = title(complete?'准备好了，试着连接吧。':state.step===7?'正在部署你的远程入口。':'核对后，就可以开始了。', complete?'从另一网络完成网页认证，再使用远程桌面客户端连接。':'先准备和校验运行文件，再将选定的配置应用到平台与本机。','REVIEW & FINISH') +
        card('本次部署', `<dl><div><dt>部署方式</dt><dd>${plan.mode==='manual'?'手动配置':'API 自动配置'}</dd></div><div><dt>认证域名</dt><dd>${esc(plan.hostname)}</dd></div><div><dt>本机端口</dt><dd>RDP ${esc(plan.rdp_port)} / 准入 ${esc(plan.admission_port)} / 认证 ${esc(plan.portal_port)}</dd></div><div><dt>KDE 修复</dt><dd>检测到兼容版本时，应用已有修复补丁</dd></div></dl><ol class="progress-list">${(job.events||['正在准备…']).map(event=>`<li>${esc(event)}</li>`).join('')}</ol>${job.error?`<p class="notice error">${esc(job.error)}</p>`:''}<div class="actions">${complete?button('打开认证页面','open-portal','primary')+button('进入运行概览','finish'):job.phase==='ready'?button('返回调整','back-access','quiet')+button('确认并部署'+icon('arrow-right'),'apply','primary'):['failed','interrupted'].includes(job.phase)?button('返回检查','back-access')+(job.revision?button('按原计划重试','apply','primary'):''):'<span class="status">请稍候，进度会自动更新</span>'}</div>`);
    }
    return stepper + body;
  }
  function settingsPage() {
    if (!state.status.configured) return title('先完成连接设置。','完成欢迎向导后，可以在这里调整认证与连接参数。','CONFIGURATION')+button('打开配置向导','begin','primary');
    const c = state.settings?.config || state.status.config || {}; const a = c.local_admission || {};
    return title('让连接适合你的日常。','查看无需解锁；保存前输入管理密码，未填写的新密钥会保持原值。','CONFIGURATION') +
      card('认证与 SakuraFrp',form('settings',`<div class="form-grid">${field('认证域名','hostname',c.hostname,{required:true})}${field('公网 RDP 地址','rdp_address',c.rdp_address,{required:true})}${field('SakuraFrp 隧道 ID','tunnel_id',c.tunnel_id,{type:'number',required:true})}${field('SakuraFrp 访问密钥','sakura_token',c.sakura_token||'',{type:'password',placeholder:c.has_sakura_token?'已设置；留空不修改':''})}${field('更换固定访问密码','password','',{type:'password',full:true,placeholder:'已有密码不会显示，留空不修改'})}${field('Turnstile Site key','turnstile_site_key',c.turnstile_site_key)}${field('Turnstile Secret key','turnstile_secret_key',c.turnstile_secret_key||'',{type:'password',placeholder:c.has_turnstile_secret?'已设置；留空不修改':''})}</div>${check('disable_turnstile','关闭并清除 Turnstile 配置')}<details><summary>本机准入与词库</summary><div class="form-grid">${field('准入监听端口','listen_port',a.listen_port||13389,{type:'number'})}${field('RDP 目标端口','target_port',a.target_port||3389,{type:'number'})}${field('本机授权时长（秒）','duration_seconds',a.duration_seconds||21600,{type:'number'})}${field('词库路径','wordlist_path',c.wordlist_path,{full:true})}</div></details><div class="actions">${submit('保存配置')}${button('查看已保存密钥','show-secrets','quiet')}${button('同步隧道并重启','sync-connectors','quiet')}</div><p class="hint">修改域名、隧道或准入端口后，请同步连接器；只修改密码时，重启服务即可生效。</p>`)) +
      card('平台连接凭据',form('provider-update', `<div class="form-grid">${field('Cloudflare API Token','cloudflare_api_token','',{type:'password',full:true,placeholder:'留空继续使用已保存的 API Token'})}${field('Cloudflare Tunnel Token（手动模式）','cloudflare_tunnel_token','',{type:'password',full:true,placeholder:'留空不修改'})}</div><div class="actions">${submit('更新并验证')}</div>`));
  }
  function credentialsPage() {
    const s = state.settings?.state;
    return title('管理可以进入的钥匙。','通行密钥在网页完成首次密码认证后绑定；临时密码成功使用后自动轮换。','ACCESS') +
      card('临时访问密码',`<p>当前密码只可使用一次，查看不会触发轮换。</p>${state.temporary?`<div class="address log">${esc(state.temporary)}</div>`:''}<div class="actions">${button('查看当前临时密码','temporary','primary')}${state.temporary?button('重新生成','regenerate'):''}</div>`) +
      card('已绑定的通行密钥',s ? (s.passkey_list?.length?s.passkey_list.map(key=>`<div class="list-row"><div class="details"><strong>${esc(key.name)}</strong><p>已登记的远程访问设备</p></div>${button('移除','delete-passkey','compact danger',`data-id="${esc(key.id)}"`)}</div>`).join(''):'<div class="empty">尚未绑定通行密钥。</div>'):`<p>${esc(state.settings?.state_error||'正在读取凭据状态…')}</p>`) +
      card('访问保护',`<p>当前封禁 IP：${esc(s?.banned_ips??'—')}；全站锁定剩余：${esc(s?.global_lock_seconds??'—')} 秒。</p><div class="actions">${button('解除认证封禁','clear-bans')}</div>`);
  }
  function agentsPage() {
    const a = state.agents;
    return title('让 Agent 帮忙，由你掌控。','每个 Agent 使用独立身份。普通修改授权固定 1 小时，危险操作始终单独确认。','AGENT ACCESS') +
      (!state.status.initialized?card('先创建管理账户','<p>登记 Agent 前，需要先设置管理密码。</p>'+button('继续欢迎向导','begin','primary')):
      !a?card('查看和管理 Agent',`<p>解锁后可以登记身份、处理申请以及查看操作记录。</p>${button('解锁并读取','load-agents','primary')}`):
      (a.pending?.length?card('等待你批准',a.pending.map(p=>`<div class="list-row"><div class="details"><strong>${esc(p.name)} ${p.kind==='dangerous'?'<span class="tag">危险操作</span>':''}</strong><p>${esc(p.reason||'申请 1 小时配置权限')}</p><p>申请编号：${esc(p.id)}${p.fields?.length?' · 修改字段：'+esc(p.fields.join('、')):''}</p>${p.details?`<p>${esc(JSON.stringify(p.details))}</p>`:''}</div><div class="row">${button('拒绝','deny-agent','compact quiet',`data-id="${esc(p.id)}"`)}${button('批准','approve-agent','compact primary',`data-id="${esc(p.id)}" data-danger="${p.kind==='dangerous'}"`)}</div></div>`).join('')):'') +
      card('已登记身份',a.agents?.length?a.agents.map(agent=>`<div class="list-row"><div class="details"><strong>${esc(agent.name)}</strong><p>${agent.remaining_seconds>0?'授权至 '+when(agent.expires_at):'当前未授权'}${agent.disabled?' · 已禁用':''}</p></div><div class="row">${button('撤销授权','revoke-agent','compact quiet',`data-id="${esc(agent.id)}"`)}${button('移除','remove-agent','compact danger',`data-id="${esc(agent.id)}"`)}</div></div>`).join(''):'<div class="empty">还没有登记 Agent。</div>') +
      card('登记新的 Agent',form('register-agent',`<div class="form-grid">${field('身份名称','name','',{full:true,required:true,placeholder:'例如：我的部署助手'})}</div><div class="actions">${submit('登记并导出 MCP 配置')}</div><p class="hint">不同工具应分别登记身份。新身份不会自动取得修改权限。</p>`)) +
      (state.exported?card('接入 MCP',`<p>将下面的配置添加到你的 Agent 工具中。身份凭据已保存在仅当前 Linux 用户可读的本机文件里。</p><pre class="log">${esc(JSON.stringify(state.exported.configuration,null,2))}</pre><div class="actions">${button('复制 MCP 配置','copy-mcp','primary')}${button('复制部署说明','copy-prompt')}</div>`):'') +
      card('最近操作',a.audit?.length?`<table class="audit"><thead><tr><th>时间</th><th>操作者</th><th>操作</th><th>结果</th></tr></thead><tbody>${a.audit.slice(-15).reverse().map(event=>`<tr><td>${when(event.time)}</td><td>${esc(event.actor)}</td><td>${esc(event.operation)}</td><td>${esc(event.outcome)}</td></tr>`).join('')}</tbody></table>`:'<p>尚无修改记录。</p>'));
  }
  function maintenancePage() {
    return title('照看正在运行的服务。','日志自动隐藏已知凭据，操作结果以后台的实际状态为准。','MAINTENANCE') +
      card('服务控制',`<div class="actions">${button(state.status.preferences?.paused?'继续全部服务':'暂停全部服务','pause','primary')}${button('重启全部服务','restart')}${button('退出并停止服务','exit','danger')}</div>`) +
      card('服务日志',`<div class="row">${button('访问认证','logs-portal','compact')}${button('SakuraFrp','logs-frpc','compact')}${button('Cloudflare','logs-cloudflare','compact')}</div><pre class="log">${esc(state.logs||'选择一个服务查看最近日志。')}</pre>`) +
      card('管理密码',form('change-password',`<div class="form-grid">${field('新的管理密码','password','',{type:'password',full:true,required:true})}${field('再次输入新密码','repeat_password','',{type:'password',full:true,required:true})}</div><div class="actions">${submit('更换管理密码','danger')}</div><p class="hint">危险操作。需要验证当前密码；更换后所有窗口会话及 Agent 临时授权都会失效。</p>`)) +
      card('旧部署迁移与恢复',`<p>保留旧凭据、通行密钥与数据库，先检查条件和计划，再开始迁移。</p><div class="actions">${button('检查迁移计划','migration-plan')}${button('恢复未完成迁移','migration-recover','quiet')}</div>${state.migration?`<pre class="log">${esc(JSON.stringify(state.migration,null,2))}</pre>${state.migration.available?button('备份并迁移','migration-start','primary'):''}`:''}`);
  }
  function appearancePage() {
    const p = state.status.preferences || {};
    return title('让它成为你的习惯。','两套配色与深浅模式独立选择，延续个人主页的视觉风格。','APPEARANCE') +
      card('配色方案',`<div class="palette-options"><button class="palette" data-action="palette-blue" aria-pressed="${p.palette!=='hutao-red'}"><span class="swatch blue"></span><strong>芙芙蓝</strong></button><button class="palette" data-action="palette-red" aria-pressed="${p.palette==='hutao-red'}"><span class="swatch red"></span><strong>胡桃红</strong></button></div>`) +
      card('明暗与启动',form('preferences',`<div class="form-grid">${select('明暗模式','theme',p.theme||'system',[{value:'system',label:'跟随系统'},{value:'light',label:'固定浅色'},{value:'dark',label:'固定深色'}])}${select('服务启动方式','startup',p.startup||'boot',[{value:'boot',label:'系统开机后自动启动'},{value:'login',label:'登录桌面后自动启动'},{value:'manual',label:'仅手动启动'}])}</div>${check('show_tray','显示系统托盘图标',p.show_tray!==false)}<p class="hint">隐藏托盘图标不会停止服务。可以随时从应用启动器打开本程序。</p><p class="hint">暂停状态不受自启动设置影响，始终保持到你手动继续。</p><div class="actions">${submit('保存偏好')}</div>`));
  }
  function render() {
    const views = {overview, wizard, settings:settingsPage, credentials:credentialsPage, agents:agentsPage, maintenance:maintenancePage, appearance:appearancePage};
    root.innerHTML = shell((views[state.view] || overview)());
  }
  async function refresh() {
    state.status = await rpc('status');
    window.RdpTheme.set(state.status.preferences || {});
  }
  async function navigate(view) {
    state.view = ['overview','wizard','settings','credentials','agents','maintenance','appearance'].includes(view)?view:'overview';
    state.error = ''; render();
    if (['settings','credentials','maintenance'].includes(state.view) && state.status.configured) {
      state.settings = await rpc('settings.read'); render();
    }
    if (state.view === 'agents' && state.unlocked) {state.agents = await api('agents.list'); render();}
  }
  async function service(action) {
    const paused = state.status.preferences?.paused;
    if (action==='pause' && paused) action='resume';
    const text = {pause:'将停止认证、frp 和 Cloudflare 隧道，经 frp 建立的连接会断开。暂停状态会跨重启保留，直到你手动继续。',resume:'将恢复认证、frp 和 Cloudflare 隧道。',restart:'将重启全部连接服务，经 frp 建立的当前连接会短暂断开。',exit:'退出后将停止认证、frp 和 Cloudflare 隧道，经 frp 建立的远程连接会断开。主机上的 KDE 远程桌面会保留。不勾选下方选项，下次开机会自动恢复；若服务已暂停，仍需手动继续。'};
    const answer = await ask({title:{pause:'暂停全部服务？',resume:'继续全部服务？',restart:'重启全部服务？',exit:'退出并停止服务？'}[action],text:text[action],danger:action==='exit',confirm:action==='exit'?'停止并退出':'确认',checkbox:action==='exit'?'下次开机不自启动':null,checked:false});
    if (!answer) return;
    const result = await api('services.'+action,{disable_autostart:!!answer.checked});
    if (action==='exit') {await rpc('ui.quit'); return;}
    toast(result.message); await refresh(); render();
  }
  async function click(action, element) {
    if (action==='begin') {state.view='wizard';state.step=1;render();return;}
    if (action==='previous') {state.step=Math.max(0,state.step-1);render();return;}
    if (action==='back-access') {state.step=5;render();return;}
    if (action==='finish') {state.draft={mode:'automatic',rdp_port:3389,admission_port:13389,portal_port:18089};await refresh();await navigate('overview');return;}
    if (action==='unlock') {
      if (state.unlocked) {await rpc('security.logout');state.unlocked=false;state.agents=null;state.temporary=null;render();}
      else await unlock();
      return;
    }
    if (['pause','restart','exit'].includes(action)) {await service(action);return;}
    const links={'link-sakura':'https://www.natfrp.com/user/','link-cloudflare':'https://dash.cloudflare.com/','link-cf-token':'https://dash.cloudflare.com/profile/api-tokens'};
    if (links[action]) {await rpc('ui.open_link',{url:links[action]});return;}
    if (action==='open-portal') {const hostname=state.status.config?.hostname||state.draft.hostname;if(hostname)await rpc('ui.open_link',{url:'https://'+hostname});return;}
    if (action==='guide') {state.guide=await rpc('onboarding.guide');await ask({title:'第一次连接',text:'先在欢迎向导完成部署，再从另一网络打开认证域名。使用固定密码、临时密码或已绑定的 Passkey 授权后，使用原远程桌面客户端连接；仍需系统账户登录。',confirm:'知道了'});return;}
    if (action==='apply') {
      const ok=await ask({title:'确认部署计划',text:'将创建或更新选定的 SakuraFrp 与 Cloudflare 配置，安装本机连接器并启动服务。检测到兼容的 KDE 远程桌面时将应用修复，相关连接可能重启。',confirm:'开始部署'});
      if(!ok)return;
      state.status.deployment=await api('onboarding.apply',{revision:state.status.deployment.revision});state.step=7;render();return;
    }
    if(action==='show-secrets'){state.settings=await api('settings.secrets');render();return;}
    if(action==='sync-connectors'){const result=await api('connectors.sync');toast(result.message);await refresh();render();return;}
    if(action==='load-agents'){state.agents=await api('agents.list');render();return;}
    if(action==='approve-agent'||action==='deny-agent'){
      const allow=action==='approve-agent';let password;
      if(allow&&element.dataset.danger==='true'){
        const result=await ask({title:'批准这一次危险操作？',text:'批准只针对申请中列出的这一次修改，不扩大 Agent 的普通授权。',password:true,danger:true,confirm:'批准本次操作',validate:async values=>api('agents.approve',{request_id:element.dataset.id,allow:true,password:values.password})});
        if(!result)return;
      }else await api('agents.approve',{request_id:element.dataset.id,allow,password});
      state.agents=await api('agents.list');render();return;
    }
    if(action==='remove-agent'||action==='revoke-agent'){
      const result=await dangerous(action==='remove-agent'?'agents.remove':'agents.revoke',{id:element.dataset.id},action==='remove-agent'?'移除此 Agent 身份及其授权，原 MCP 凭据将失效。':'立即撤销此 Agent 的一小时临时授权。');
      if(result){state.agents=await api('agents.list');render();}return;
    }
    if(action==='copy-mcp'){await rpc('ui.copy',{text:JSON.stringify(state.exported.configuration,null,2)});toast('MCP 配置已复制。');return;}
    if(action==='copy-prompt'){await rpc('ui.copy',{text:'请通过已配置的 rdp-access-auth MCP 帮我部署本机软件。先调用 get_status、get_setup_guide 和 get_environment；通过 request_authorization 请求我批准此 Agent 的一小时修改权限，不要索取管理密码或代替我批准。按向导收集账号、域名和 API 密钥，优先选择 automatic 模式。调用 inspect_platforms、prepare_deployment，向我说明具体计划后调用 apply_deployment，再轮询 deployment_status。危险操作必须单独征求批准。不要绕过 MCP 直接改系统文件；不要输出密钥。最后说明本机检查结果，并指导我从另一网络完成真实连接测试。'});toast('部署说明已复制。');return;}
    if(action==='temporary'){const result=await api('credentials.temporary');state.temporary=result.temporary;render();return;}
    if(action==='regenerate'){const ok=await ask({title:'重新生成临时密码？',text:'当前临时密码将立即失效。',confirm:'重新生成'});if(ok){const result=await api('credentials.regenerate',{expected:state.temporary});state.temporary=result.temporary;render();}return;}
    if(action==='delete-passkey'){const ok=await ask({title:'移除此通行密钥？',text:'对应设备将无法再用这个通行密钥完成访问认证。',danger:true});if(ok){await api('credentials.delete_passkey',{id:element.dataset.id});state.settings=await rpc('settings.read');render();}return;}
    if(action==='clear-bans'){await api('credentials.unlock');toast('认证封禁已解除。');state.settings=await rpc('settings.read');render();return;}
    if(action.startsWith('logs-')){state.logs=(await api('logs.read',{service:action.slice(5)})).output;render();return;}
    if(action.startsWith('palette-')){const prefs=await api('preferences.save',{changes:{palette:action==='palette-red'?'hutao-red':'fufu-blue'}});state.status.preferences=prefs;window.RdpTheme.set(prefs);render();return;}
    if(action==='migration-plan'){state.migration=await api('migration.plan');render();return;}
    if(action==='migration-start'||action==='migration-recover'){const ok=await ask({title:'确认迁移操作',text:'将备份并切换现有服务；失败时尝试恢复，当前远程连接可能短暂中断。'});if(ok){state.migration=await api('migration.start',{revision:state.migration?.revision,recover:action==='migration-recover'});render();}return;}
  }
  async function submitted(name, values) {
    if(name==='bootstrap'){
      if(!state.status.initialized){if(values.password!==values.repeat_password)throw {message:'两次输入的管理密码不一致。'};state.status=await rpc('ui.bootstrap',{password:values.password});}
      await rpc('security.login',{password:values.password});state.unlocked=true;state.step=2;render();return;
    }
    if(name==='mode'){state.draft.mode=values.mode;if(values.mode==='agent'){await navigate('agents');if(state.unlocked){state.agents=await api('agents.list');render();}}else{state.step=3;render();}return;}
    if(name==='platforms'){
      if(values.cloudflare_tunnel_token)state.draft.cloudflare_tunnel_token=values.cloudflare_tunnel_token;
      state.inventory=await api('onboarding.inspect',values);
      if(state.draft.mode==='automatic'&&!state.inventory.cloudflare)throw {message:'自动配置还需要 Cloudflare API Token。'};
      const cf=state.inventory.cloudflare;
      if(cf?.accounts.length===1)state.draft.cf_account_id=cf.accounts[0].id;
      if(cf?.zones.length===1)state.draft.cf_zone_id=cf.zones[0].id;
      state.step=4;render();return;
    }
    if(name==='network'){
      for(const key of ['sakura_node_id','sakura_tunnel_id'])if(values[key])values[key]=Number(values[key]);else delete values[key];
      state.draft={...state.draft,...values};state.step=5;render();return;
    }
    if(name==='access'){
      if(values.password!==values.repeat_password)throw {message:'两次输入的访问密码不一致。'};
      delete values.repeat_password;state.draft={...state.draft,...values};
      const fields={...state.draft};for(const key of Object.keys(fields))if(fields[key]==='')delete fields[key];
      state.status.deployment=await api('onboarding.prepare',fields);state.step=6;render();return;
    }
    if(name==='settings'){
      const changes={};for(const key of ['hostname','rdp_address','tunnel_id','password','sakura_token','turnstile_site_key','turnstile_secret_key','wordlist_path'])if(values[key]!==''&&values[key]!==undefined)changes[key]=values[key];
      if(values.wordlist_path!==undefined)changes.wordlist_path=values.wordlist_path;
      changes.disable_turnstile=values.disable_turnstile;
      changes.local_admission={listen_port:values.listen_port,target_port:values.target_port,duration_seconds:values.duration_seconds};
      const result=await api('settings.save',{changes,revision:state.settings?.revision||state.status.revision});
      toast(result.message);state.settings=await rpc('settings.read');await refresh();render();return;
    }
    if(name==='provider-update'){await api('onboarding.inspect',values);toast('平台凭据已验证并保存。');return;}
    if(name==='register-agent'){
      const identity=await dangerous('agents.register',{name:values.name},'登记一个独立的 Agent 身份。登记后仍需批准一小时修改权限，才可更改配置。');
      if(!identity)return;state.exported=await rpc('ui.export_agent',{identity});identity.key='';state.agents=await api('agents.list');render();return;
    }
    if(name==='preferences'){const prefs=await api('preferences.save',{changes:values});state.status.preferences=prefs;window.RdpTheme.set(prefs);toast('外观与启动偏好已保存。');render();return;}
    if(name==='change-password'){
      if(values.password!==values.repeat_password)throw {message:'两次输入的新密码不一致。'};
      const result=await dangerous('security.password',{password:values.password},'更换管理密码后，当前窗口将锁定，全部 Agent 临时授权会立即失效。');
      if(result){state.unlocked=false;state.agents=null;toast(result.message);render();}return;
    }
  }
  document.addEventListener('click',event=>{
    const eye=event.target.closest('[data-eye]');if(eye){const input=document.getElementById(eye.dataset.eye);input.type=input.type==='password'?'text':'password';return;}
    const nav=event.target.closest('[data-view]');if(nav){navigate(nav.dataset.view).catch(error=>toast(error.message));return;}
    const action=event.target.closest('[data-action]');if(action&&!action.disabled){action.disabled=true;click(action.dataset.action,action).catch(error=>{if(error.code!==499)toast(error.message);}).finally(()=>{action.disabled=false;});}
  });
  document.addEventListener('submit',event=>{
    const target=event.target.closest('[data-form]');if(!target)return;
    event.preventDefault();const name=target.dataset.form;const values=data(target);const submitButton=target.querySelector('[type=submit]');submitButton.disabled=true;
    target.querySelector('.inline-error').textContent='';
    submitted(name,values).catch(error=>{if(target.isConnected)target.querySelector('.inline-error').textContent=error.message||'操作未完成。';else toast(error.message);}).finally(()=>{submitButton.disabled=false;});
  });
  document.addEventListener('change',event=>{
    if(event.target.name==='cf_account_id'&&state.view==='wizard'){
      const current=event.target.closest('form');state.draft={...state.draft,...data(current)};state.draft.cf_zone_id='';render();
    }
  });
  function receiveEvent(raw){
    const event=JSON.parse(raw);
    if(event.type==='locked'){
      state.unlocked=false;state.agents=null;state.temporary=null;state.settings=null;
      for(const key of ['password','cloudflare_tunnel_token','turnstile_secret_key'])delete state.draft[key];
      if(dialog.open)dialog.close();render();
    }else if(event.type==='status'){
      const old=JSON.stringify(state.status.deployment);state.status=event.data;
      window.RdpTheme.set(event.data.preferences||{});
      if(state.view==='overview'||state.view==='wizard'&&state.step>=6&&old!==JSON.stringify(event.data.deployment))render();
      if(state.view==='agents'&&state.unlocked&&event.data.pending_count){api('agents.list').then(value=>{if(state.view==='agents'){state.agents=value;render();}}).catch(()=>{});}
    }else if(event.type==='system-theme')window.RdpTheme.system(event.theme);
    else if(event.type==='navigate')navigate(event.action==='settings'?'settings':event.action).catch(error=>toast(error.message));
    else if(event.type==='action')service(event.action).catch(error=>toast(error.message));
  }
  if(!window.qt?.webChannelTransport||!window.QWebChannel){root.innerHTML='<main class="loading"><h1>请从应用启动器打开软件</h1><p>本机管理界面需要 RDP Access Auth 的桌面窗口。</p></main>';return;}
  new QWebChannel(qt.webChannelTransport,async channel=>{
    bridge=channel.objects.bridge;
    bridge.response.connect(raw=>{const value=JSON.parse(raw);const pending=waiting.get(value.id);if(!pending)return;clearTimeout(pending.timeout);waiting.delete(value.id);value.error?pending.reject(value.error):pending.resolve(value.result);});
    bridge.event.connect(receiveEvent);
    try{state.status=await rpc('ui.ready');state.view=new URL(location.href).searchParams.get('view')||'overview';if(state.view==='open')state.view=state.status.configured?'overview':'wizard';if(!state.status.initialized){state.view='wizard';state.step=0;}else if(!state.status.configured){state.view=state.view==='agents'?'agents':'wizard';state.step=state.status.deployment?.phase==='ready'?6:state.status.deployment?.phase==='installing'?7:0;}window.RdpTheme.set(state.status.preferences||{});render();}
    catch(error){state.error=error.message;render();}
  });
})();
