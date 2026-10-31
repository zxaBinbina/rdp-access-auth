/* Palette and brightness are independent. Native windows provide the actual Qt theme. */
(() => {
  const media = matchMedia('(prefers-color-scheme: light)');
  const query = new URL(location.href).searchParams;
  let system = query.get('system') || (media.matches ? 'light' : 'dark');
  let settings = {palette: 'fufu-blue', theme: 'system'};
  try {
    const stored = JSON.parse(localStorage.getItem('rdp-access-auth-appearance') || '{}');
    settings = {...settings, ...stored};
  } catch {}
  if (query.has('theme')) settings.theme = query.get('theme');
  if (query.has('palette')) settings.palette = query.get('palette');
  function apply() {
    const palette = settings.palette === 'hutao-red' ? 'hutao-red' : 'fufu-blue';
    const theme = ['light', 'dark'].includes(settings.theme) ? settings.theme : system;
    document.documentElement.dataset.palette = palette;
    document.documentElement.dataset.theme = theme;
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.content = palette === 'hutao-red' ? (theme === 'light' ? '#fcf0e1' : '#3a1b19') : (theme === 'light' ? '#f4f6fa' : '#141521');
  }
  window.RdpTheme = {
    get: () => ({...settings}),
    set(value) { settings = {...settings, ...value}; apply(); try {localStorage.setItem('rdp-access-auth-appearance', JSON.stringify(settings));} catch {} },
    system(value) { system = value === 'light' ? 'light' : 'dark'; apply(); }
  };
  media.addEventListener('change', () => {system = media.matches ? 'light' : 'dark'; apply();});
  apply();
  document.addEventListener('DOMContentLoaded', () => {
    if (document.documentElement.dataset.context !== 'portal') return;
    document.querySelectorAll('[data-appearance]').forEach(select => {
      select.value = settings[select.dataset.appearance];
      select.addEventListener('change', () => window.RdpTheme.set({[select.dataset.appearance]: select.value}));
    });
  });
})();
