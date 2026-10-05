(function () {
  function sendEvent(name, params) {
    if (typeof window.gtag !== 'function') return;
    window.gtag('event', name, Object.assign({
      page_path: window.location.pathname,
      transport_type: 'beacon'
    }, params || {}));
  }

  window.ginguiTrack = sendEvent;

  document.addEventListener('click', function (event) {
    const target = event.target.closest('[data-track]');
    if (!target) return;
    const href = target.getAttribute('href') || '';
    const linkType = href.startsWith('tel:') ? 'telephone' : href.startsWith('http') ? 'external' : 'internal';
    const linkPath = linkType === 'internal' && target.href ? new URL(target.href).pathname : '';
    sendEvent(target.dataset.track, {
      link_label: target.dataset.trackLabel || target.dataset.needGroup || '',
      link_type: linkType,
      link_path: linkPath,
      member_id: target.dataset.member || '',
      need_group: target.dataset.needGroup || ''
    });
  });
})();
