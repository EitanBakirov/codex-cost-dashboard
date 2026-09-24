(() => {
  // Keep icon geometry separate from color; CSS variables style every view.
  const shapes = {
    astra: '<path d="M7 2.5 8.2 5.8 11.5 7 8.2 8.2 7 11.5 5.8 8.2 2.5 7 5.8 5.8Z"/><path d="M17.2 12.6 18 15 20.4 15.8 18 16.6 17.2 19 16.4 16.6 14 15.8 16.4 15Z"/><path d="M17.7 3.2v3.2M16.1 4.8h3.2"/>',
    sol: '<circle cx="12" cy="12" r="4.2"/><path d="M12 1.8v2.4M12 19.8v2.4M1.8 12h2.4M19.8 12h2.4M4.8 4.8l1.7 1.7M17.5 17.5l1.7 1.7M19.2 4.8l-1.7 1.7M6.5 17.5l-1.7 1.7"/>',
    terra: '<circle cx="12" cy="12" r="9"/><ellipse cx="12" cy="12" rx="4.3" ry="9"/><path d="M3.6 8.2c5.4 2.4 11.4 2.4 16.8 0M3.6 15.8c5.4-2.4 11.4-2.4 16.8 0"/>',
    luna: '<path d="M19.5 17.8A9 9 0 0 1 6.2 4.5 9 9 0 1 0 19.5 17.8Z"/>'
  };
  const selector = '.model-astra,.model-sol,.model-terra,.model-luna,.model-other,.guide-model,.guide-intro h3';
  const familyFor = value => /(?:^|[^a-z])(astra|sol|terra|luna)(?:$|[^a-z])/i.exec(value)?.[1]?.toLowerCase();

  function decorate(element) {
    if (element.querySelector(':scope > .model-icon')) return;
    // Guide cells include a separate status span; read the name text alone.
    const name = element.firstChild?.nodeType === Node.TEXT_NODE
      ? element.firstChild.textContent : element.textContent;
    const family = familyFor(name || '');
    if (!family) return;
    element.classList.add(`model-${family}`);
    const icon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    icon.setAttribute('class', 'model-icon');
    icon.setAttribute('viewBox', '0 0 24 24');
    icon.setAttribute('aria-hidden', 'true');
    icon.setAttribute('focusable', 'false');
    icon.innerHTML = shapes[family];
    element.prepend(icon);
  }

  function decorateWithin(root) {
    if (!(root instanceof Element)) return;
    if (root.matches(selector)) decorate(root);
    root.querySelectorAll(selector).forEach(decorate);
  }

  decorateWithin(document.body);
  new MutationObserver(records => {
    for (const record of records) {
      for (const node of record.addedNodes) decorateWithin(node);
    }
  }).observe(document.body, {childList: true, subtree: true});
})();
