"""Portable physical input helpers for the supported Qt WebEngine versions."""
import socket
import json
import signal
import time

NATIVE_EVENTS = {'close': signal.SIGUSR1, 'advance': signal.SIGUSR2, 'activity': signal.SIGHUP,
                 'advance_more': signal.SIGALRM, 'inspect': signal.SIGWINCH}


def install_native_window_controls():
    """Test-only native input and clock controls for the disposable Qt process."""
    from desktop import app as native
    try:
        from PySide6.QtCore import QEvent, Qt
        from PySide6.QtGui import QKeyEvent
        from PySide6.QtWidgets import QApplication, QMainWindow, QSystemTrayIcon
    except ImportError:
        from PyQt6.QtCore import QEvent, Qt
        from PyQt6.QtGui import QKeyEvent
        from PyQt6.QtWidgets import QApplication, QMainWindow, QSystemTrayIcon
    original_clock, offset = native.monotonic, 0
    native.monotonic = lambda: original_clock() + offset

    def handle(signum, _):
        nonlocal offset
        name = next(name for name, number in NATIVE_EVENTS.items() if number == signum)
        application = QApplication.instance()
        window = next(w for w in application.topLevelWidgets() if isinstance(w, QMainWindow))
        if name == 'close':
            window.close()
        elif name == 'advance':
            offset += native.IDLE_LOCK_SECONDS - 10
        elif name == 'advance_more':
            offset += 20
        elif name == 'activity':
            target = window.centralWidget().focusProxy() or window.centralWidget()
            QApplication.sendEvent(target, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Shift, Qt.KeyboardModifier.NoModifier))
        bridge = window.centralWidget().page().webChannel().registeredObjects()['bridge']
        print('NATIVE_TEST:' + json.dumps({'event': name, 'visible': window.isVisible(),
              'tray_visible': any(t.isVisible() for t in application.findChildren(QSystemTrayIcon)),
              'unlocked': bridge.session is not None, 'last_activity': bridge.last_activity}), flush=True)

    for number in NATIVE_EVENTS.values():
        signal.signal(number, handle)


def native_window_event(process, name, log_path):
    marker = 'NATIVE_TEST:'
    previous = log_path.read_text().count(marker)
    process.send_signal(NATIVE_EVENTS[name])
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        lines = [line for line in log_path.read_text().splitlines() if line.startswith(marker)]
        if len(lines) > previous:
            result = json.loads(lines[-1][len(marker):])
            assert result['event'] == name, result
            return result
        assert process.poll() is None, 'native test window exited unexpectedly'
        time.sleep(.05)
    raise AssertionError(f'native window did not handle {name}')


def check_idle_lock(page, process, log_path):
    before = native_window_event(process, 'inspect', log_path)
    assert before['unlocked']
    native_window_event(process, 'advance', log_path)
    page.wait_for_timeout(2800)  # Include a normal background status poll.
    after_poll = native_window_event(process, 'inspect', log_path)
    assert after_poll['last_activity'] == before['last_activity'], 'background polling extended the session'
    text_is(page, '#lock-label', '设置已解锁')
    activity = native_window_event(process, 'activity', log_path)
    assert activity['last_activity'] > before['last_activity']
    native_window_event(process, 'advance_more', log_path)
    page.wait_for_timeout(1100)
    text_is(page, '#lock-label', '设置已解锁')
    native_window_event(process, 'advance', log_path)
    text_is(page, '#lock-label', '设置已锁定')
    text_is(page, '#toast', '已连续 10 分钟未操作，设置已自动锁定。')
    assert not native_window_event(process, 'inspect', log_path)['unlocked']

# Use browser-native DOM checks and physical input. Current Playwright's locator
# assertions require Element.checkVisibility, absent in Ubuntu's Qt 6.4 engine.
class Element:
    def __init__(self, page, selector):
        self.page, self.selector = page, selector

    def wait(self):
        self.page.wait_for_function("s => {const e=document.querySelector(s);return e && e.getBoundingClientRect().height>0}", arg=self.selector)
        return self

    def click(self, allow_disabled=False):
        self.wait()
        if not allow_disabled:
            self.page.wait_for_function("s => !document.querySelector(s).disabled", arg=self.selector)
        point = self.page.evaluate("async s => {const e=document.querySelector(s);e.scrollIntoView({block:'center'});await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));const r=e.getBoundingClientRect();return [r.x+r.width/2,r.y+r.height/2]}", self.selector)
        self.page.mouse.click(*point)

    def fill(self, value):
        self.wait()
        self.page.evaluate("s => {const e=document.querySelector(s);e.focus();e.select()}", self.selector)
        self.page.keyboard.insert_text(value)

    def check(self):
        if not self.page.evaluate('s => document.querySelector(s).checked', self.selector): self.click()

    def uncheck(self):
        if self.page.evaluate('s => document.querySelector(s).checked', self.selector): self.click()

    def evaluate(self, expression):
        self.wait()
        return self.page.evaluate('v => ('+expression+')(document.querySelector(v))', self.selector)


def text_is(page, selector, value, contains=False):
    page.wait_for_function("v => {const e=document.querySelector(v[0]);return e && (v[2] ? e.textContent.includes(v[1]) : e.textContent===v[1])}", arg=[selector,value,contains])


def attribute_is(page, selector, name, value):
    page.wait_for_function("v => document.querySelector(v[0])?.getAttribute(v[1])===v[2]", arg=[selector,name,value])


def check_fixed_layout(page, screenshot_path):
    """Scroll long pages at normal and minimum window sizes using real input."""
    original = page.evaluate('() => ({width:innerWidth,height:innerHeight})')
    scrolled = False
    try:
        for size in (original, {'width': 760, 'height': 560}):
            page.set_viewport_size(size)
            page.evaluate("() => {document.getElementById('main-content').scrollTop=0;}")
            page.wait_for_function("() => document.getElementById('main-content').clientHeight>0")
            before = page.evaluate("""() => {
                const main=document.getElementById('main-content');
                return {fixed:[...document.querySelectorAll('.topbar,.sidebar,.demo-banner,.setup-navigation')].map(e=>e.getBoundingClientRect().toJSON()),
                    main:main.getBoundingClientRect().toJSON(), maxScroll:main.scrollHeight-main.clientHeight,
                    contentTop:main.firstElementChild.getBoundingClientRect().top};
            }""")
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth && document.documentElement.scrollHeight<=innerHeight'), 'the whole window still overflows'
            assert all(r['top'] >= 0 and r['bottom'] <= size['height'] for r in before['fixed']), before
            assert before['main']['top'] >= before['fixed'][0]['bottom'] and before['main']['bottom'] <= size['height'], before
            if before['maxScroll'] > 0:
                scrolled = True
                page.mouse.move(before['main']['right']-16, before['main']['top']+50)
                page.mouse.wheel(0, 600)
                page.wait_for_function("() => document.getElementById('main-content').scrollTop>0")
                after = page.evaluate("""() => ({
                    fixed:[...document.querySelectorAll('.topbar,.sidebar,.demo-banner,.setup-navigation')].map(e=>e.getBoundingClientRect().toJSON()),
                    contentTop:document.getElementById('main-content').firstElementChild.getBoundingClientRect().top,
                    scroll:document.getElementById('main-content').scrollTop, windowScroll:scrollY
                })""")
                assert after['fixed'] == before['fixed'], 'scrolling content moved the navigation or title bar'
                assert after['contentTop'] < before['contentTop'] and after['windowScroll'] == 0, after
                page.wait_for_timeout(2800)  # A background refresh must preserve the scroll position.
                assert page.evaluate("document.getElementById('main-content').scrollTop") == after['scroll']
            if page.evaluate("Boolean(document.querySelector('.sidebar'))"):
                # On a short window the sidebar may scroll independently.
                page.evaluate("async () => {document.querySelector('.sidebar .bottom button').scrollIntoView({block:'nearest'});await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));}")
                sidebar = page.evaluate("""() => {
                    const nav=document.querySelector('.sidebar').getBoundingClientRect();
                    const help=document.querySelector('.sidebar .bottom button').getBoundingClientRect();
                    return {visible:help.top>=nav.top-1 && help.bottom<=nav.bottom+1 && scrollY===0,
                        nav:nav.toJSON(),help:help.toJSON(),windowScroll:scrollY};
                }""")
                assert sidebar['visible'], f'the bottom sidebar entry is unreachable: {sidebar}'
            page.screenshot(path=str(screenshot_path.with_name(screenshot_path.stem+f'-{size["width"]}.png')))
        assert scrolled, 'fixture did not exercise content scrolling'
    finally:
        page.set_viewport_size(original)
        page.evaluate("() => {document.getElementById('main-content').scrollTop=0;const nav=document.querySelector('.sidebar');if(nav)nav.scrollTop=0;}")


def check_locked_settings(page):
    controls = {
        'settings': 'input, select, [type=submit], [data-action=sync-connectors]',
        'maintenance': 'input, [type=submit], [data-action=pause], [data-action=restart], [data-action=exit]',
        'credentials': 'input, [type=submit], [data-action=delete-passkey], [data-action=clear-bans]',
        'appearance': 'input, select, [type=submit], .palette, .theme-options button',
    }
    attempts = {'settings': ['[name=hostname]', '[data-action=sync-connectors]'],
                'maintenance': ['[data-action=pause]'], 'credentials': ['[data-action=clear-bans]', '[name=allow_passkey_registration]'],
                'appearance': ['[data-action=palette-blue]', '[data-action=theme-dark]', '[name=startup]', '.check span']}
    for view, selector in controls.items():
        Element(page, f'[data-view={view}]').click()
        assert page.evaluate("document.getElementById('locked-hint').hidden")
        page.wait_for_function("s => {const controls=[...document.getElementById('main-content').querySelectorAll(s)];return controls.length>0 && controls.every(e=>e.matches(':disabled'));}", arg=selector)
        for attempt in attempts[view]:
            page.keyboard.press('Escape')
            assert page.evaluate("document.getElementById('locked-hint').hidden")
            Element(page, attempt).click(allow_disabled=True)
            text_is(page, '#locked-hint', '设置已锁定，请点击右上角解锁后修改。')
            page.wait_for_function("() => !document.getElementById('locked-hint').hidden")
            geometry = page.evaluate("""s => {
                const target = document.querySelector(s);
                const anchor = (target.closest('label')?.control || target).getBoundingClientRect();
                const bubble = document.getElementById('locked-hint').getBoundingClientRect();
                const gap = bubble.bottom <= anchor.top ? anchor.top - bubble.bottom : bubble.top - anchor.bottom;
                return {inside: bubble.left >= 0 && bubble.right <= innerWidth && bubble.top >= 0 && bubble.bottom <= innerHeight,
                    near: gap >= 0 && gap <= 20 && bubble.right >= anchor.left && bubble.left <= anchor.right,
                    gap, anchor:anchor.toJSON(), bubble:bubble.toJSON(), hidden:document.getElementById('locked-hint').hidden};
            }""", attempt)
            assert geometry['inside'] and geometry['near'], f'locked hint is not beside {attempt}: {geometry}'
    page.wait_for_function("() => document.getElementById('locked-hint').hidden")
    prefs = page.evaluate('window.RdpTheme.get()')
    # Disabled clicks and even a synthetic submit must leave locked preferences alone.
    page.evaluate("""() => {
        document.querySelector('[data-action=theme-dark]').click();
        document.querySelector('[data-action=palette-blue]').click();
        document.querySelector('[data-form=preferences]').dispatchEvent(new Event('submit', {bubbles:true, cancelable:true}));
    }""")
    page.wait_for_timeout(150)
    assert not page.evaluate("Boolean(document.querySelector('#dialog[open]'))")
    assert page.evaluate('window.RdpTheme.get()') == prefs


def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


