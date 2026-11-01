/* Patch changed text and attributes while retaining focus, form edits and frames. */
(() => {
  const attributes = new WeakMap();
  const values = new WeakMap();
  const isField = node => ['INPUT', 'TEXTAREA', 'SELECT'].includes(node.nodeName);
  function remember(node) {
    if (node.nodeType !== Node.ELEMENT_NODE) return;
    attributes.set(node, new Map([...node.attributes].map(attr => [attr.name, attr.value])));
    if (isField(node)) values.set(node, {value:node.value, checked:node.checked});
    node.childNodes.forEach(remember);
  }
  function key(node) {
    if (!node || node.nodeType !== Node.ELEMENT_NODE) return null;
    if (node.hasAttribute('data-key')) return 'key:' + node.dataset.key;
    if (node.id) return 'id:' + node.id;
    for (const name of ['data-view', 'data-form', 'data-eye']) {
      if (node.hasAttribute(name)) return name + ':' + node.getAttribute(name);
    }
    if (node.hasAttribute('data-action')) {
      return ['data-action','data-id','data-step','data-screen'].map(name => node.getAttribute(name) || '').join(':');
    }
    if (node.nodeName === 'OPTION') return 'option:' + node.value;
    return null;
  }
  const sameKind = (left, right) => left && left.nodeType === right.nodeType && left.nodeName === right.nodeName && left.namespaceURI === right.namespaceURI;
  function patch(node, next) {
    if (node.nodeType !== Node.ELEMENT_NODE) {
      if (node.nodeValue !== next.nodeValue) node.nodeValue = next.nodeValue;
      return;
    }
    const previous = attributes.get(node);
    const incoming = new Map([...next.attributes].map(attr => [attr.name, attr.value]));
    const field = isField(node) ? {value:node.value, checked:node.checked} : null;
    const priorValue = values.get(node);
    // Compare rendered attributes, not live ones: a password toggle or a busy
    // button may have intentionally changed the live DOM since the last render.
    for (const [name, value] of incoming) {
      if (previous.get(name) !== value) node.setAttribute(name, value);
    }
    for (const name of previous.keys()) {
      if (!incoming.has(name)) node.removeAttribute(name);
    }
    attributes.set(node, incoming);
    patchChildren(node, next);
    if (field) {
      const value = field.value === priorValue.value ? next.value : field.value;
      if (node.value !== value) node.value = value;
      if (node.nodeName === 'INPUT') {
        const checked = field.checked === priorValue.checked ? next.checked : field.checked;
        if (node.checked !== checked) node.checked = checked;
      }
      values.set(node, {value:next.value, checked:next.checked});
    }
  }
  function patchChildren(parent, next) {
    const keyed = new Map([...parent.childNodes].filter(node => key(node) !== null).map(node => [key(node), node]));
    let cursor = parent.firstChild;
    for (const child of next.childNodes) {
      const childKey = key(child);
      let node = childKey === null ? (key(cursor) === null ? cursor : null) : keyed.get(childKey);
      if (!sameKind(node, child)) {
        node = child.cloneNode(true);
        remember(node);
        parent.insertBefore(node, cursor);
      } else {
        if (node !== cursor) parent.insertBefore(node, cursor);
        patch(node, child);
      }
      cursor = node.nextSibling;
    }
    while (cursor) {
      const nextSibling = cursor.nextSibling;
      cursor.remove();
      cursor = nextSibling;
    }
  }
  window.RdpDom = {
    render(parent, html) {
      const active = document.activeElement;
      const selection = active && typeof active.selectionStart === 'number' ? [active.selectionStart, active.selectionEnd, active.selectionDirection] : null;
      const template = document.createElement('template');
      template.innerHTML = html;
      // The initial loading screen is static and has no saved render state yet.
      parent.childNodes.forEach(node => {if (node.nodeType === Node.ELEMENT_NODE && !attributes.has(node)) remember(node);});
      patchChildren(parent, template.content);
      // Moving a keyed card can blur a descendant even though its node survives.
      if (active?.isConnected && document.activeElement !== active) {
        active.focus({preventScroll:true});
        if (selection) active.setSelectionRange(...selection);
      }
    }
  };
})();
