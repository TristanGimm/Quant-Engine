/* Point the product links at the local ports when this page is opened from
   this machine, so the three apps can be tested against each other without
   touching the deployed instances. */
(function () {
  // Tapping the button opens the menu on touch screens, where hover cannot.
  document.querySelectorAll('.nav-switch').forEach(function (box) {
    var button = box.querySelector('button');
    if (!button) return;
    button.addEventListener('click', function (event) {
      event.stopPropagation();
      var open = box.classList.toggle('open');
      button.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
    document.addEventListener('click', function () {
      box.classList.remove('open');
      button.setAttribute('aria-expanded', 'false');
    });
  });

  var host = location.hostname;
  var local = host === 'localhost' || host === '127.0.0.1' || /\.local$/.test(host);
  if (!local) return;
  var scheme = location.protocol === 'https:' ? 'https' : 'http';
  document.querySelectorAll('.nav-switch a[data-port]').forEach(function (link) {
    link.href = scheme + '://' + host + ':' + link.getAttribute('data-port') + (link.getAttribute('data-path') || '/');
  });
})();
