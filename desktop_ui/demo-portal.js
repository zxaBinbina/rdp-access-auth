/* Only served by the independent demo window. All interactions stay local. */
(() => {
  const show = screen => {
    const query = new URLSearchParams({screen, palette:document.documentElement.dataset.palette,
      theme:document.documentElement.dataset.theme});
    location.href = '/demo-portal.html?' + query;
  };
  document.querySelectorAll('form').forEach(form => {form.noValidate = true;});
  // Fill with per-window random fixtures supplied by the native demo renderer.
  const sample = JSON.parse(document.getElementById('demo-portal-data').textContent);
  const password = document.getElementById('password');
  if (password) password.value = sample.password;
  sample.words.forEach((word,index) => {const input=document.getElementById('temporary-'+(index+1));if(input)input.value=word;});
  document.addEventListener('submit', event => {
    event.preventDefault();event.stopImmediatePropagation();
    const form = event.target;
    if (form.id === 'auth-form') {
      sessionStorage.setItem('demo-auth-method', form.elements.method.value);
      show('success');
    }
    else if (form.querySelector('#revoke-admission')) show('password');
    else if (form.classList.contains('credential-item')) {
      sessionStorage.removeItem('demo-auth-method');
      show('password');
    }
    else if (form.querySelector('#regenerate-temporary')) {
      const target = document.getElementById('next-temporary');
      target.textContent = sample.words.slice().reverse().join('-');
    }
  }, true);
  document.addEventListener('click', event => {
    const register = event.target.closest('#register-key');
    if (register) {
      event.preventDefault();event.stopImmediatePropagation();
      document.getElementById('passkey-status').textContent = '示例通行密钥已绑定。';
      return;
    }
    const link = event.target.closest('a');
    if (!link) return;
    event.preventDefault();event.stopImmediatePropagation();
    if (link.dataset.authMethod) show(link.dataset.authMethod);
    else if (link.getAttribute('href') === '/credentials') show(sessionStorage.getItem('demo-auth-method') === 'passkey' ? 'credentials-passkey' : 'credentials');
    else if (link.getAttribute('href').startsWith('/')) show('password');
  }, true);
})();
