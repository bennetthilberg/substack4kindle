(function () {
  var shell = document.getElementById('reader-shell');
  var viewport = document.getElementById('reader-viewport');
  var content = document.getElementById('reader-content');
  var previous = document.getElementById('reader-prev');
  var next = document.getElementById('reader-next');
  var status = document.getElementById('reader-status');
  if (!shell || !viewport || !content || !window.addEventListener) return;

  var articleId = shell.getAttribute('data-article-id');
  var storageKey = 'substack4kindle:progress:' + articleId;
  var page = 0;
  var total = 1;
  var step = 0;
  var gap = 32;
  var resizeTimer = null;
  var imageTimer = null;
  var inFlight = false;
  var queued = null;
  var active = false;

  function parseJSON(value) {
    try { return JSON.parse(value); } catch (error) { return null; }
  }

  function localRecord() {
    try { return parseJSON(window.localStorage.getItem(storageKey)); }
    catch (error) { return null; }
  }

  function storeLocal(record) {
    try { window.localStorage.setItem(storageKey, JSON.stringify(record)); }
    catch (error) { /* The server still keeps the reading position. */ }
  }

  var serverRecord = parseJSON(shell.getAttribute('data-progress'));
  var deviceRecord = localRecord();
  var restored = deviceRecord && deviceRecord.pending ? deviceRecord :
    serverRecord && typeof serverRecord.page === 'number' ? serverRecord : deviceRecord;

  function send(record) {
    if (inFlight) { queued = record; return; }
    inFlight = true;
    var xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/progress/' + encodeURIComponent(articleId), true);
    xhr.setRequestHeader('Content-Type', 'application/x-www-form-urlencoded');
    xhr.setRequestHeader('X-Reader-Progress', '1');
    xhr.onreadystatechange = function () {
      if (xhr.readyState !== 4) return;
      inFlight = false;
      if (xhr.status === 200) {
        var current = localRecord();
        if (current && current.page === record.page && current.total === record.total) {
          current.pending = false;
          storeLocal(current);
        }
      }
      if (queued) {
        var newest = queued;
        queued = null;
        send(newest);
      }
    };
    try {
      xhr.send('page_number=' + record.page + '&total=' + record.total +
        '&width=' + record.width + '&height=' + record.height);
    } catch (error) {
      inFlight = false;
    }
  }

  function remember() {
    var record = {
      page: page,
      total: total,
      width: viewport.clientWidth,
      height: viewport.clientHeight,
      pending: true
    };
    storeLocal(record);
    send(record);
  }

  function showPage(number, save) {
    page = Math.max(0, Math.min(total - 1, number));
    viewport.scrollLeft = page * step;
    status.innerHTML = 'Page ' + (page + 1) + ' of ' + total +
      (page === 0 && viewport.clientWidth > 400 ? ' · Tap sides to turn' : '');
    previous.disabled = page === 0;
    next.disabled = page === total - 1;
    if (save) remember();
  }

  function fallback() {
    active = false;
    document.body.className = document.body.className.replace(/\s*paged\b/g, '');
    content.style.webkitColumnWidth = '';
    content.style.columnWidth = '';
    content.style.webkitColumnGap = '';
    content.style.columnGap = '';
  }

  function layout(resized) {
    if (!('webkitColumnWidth' in content.style || 'columnWidth' in content.style)) return;
    if (document.body.className.indexOf('paged') < 0) document.body.className += ' paged';
    var width = viewport.clientWidth;
    var height = viewport.clientHeight;
    if (width < 100 || height < 150) { fallback(); return; }

    var oldPage = page;
    var oldTotal = total;
    gap = width < 440 ? 24 : 32;
    step = width + gap;
    content.style.webkitColumnWidth = width + 'px';
    content.style.columnWidth = width + 'px';
    content.style.webkitColumnGap = gap + 'px';
    content.style.columnGap = gap + 'px';
    var images = content.getElementsByTagName('img');
    for (var i = 0; i < images.length; i++) {
      images[i].style.maxHeight = Math.max(120, height - 110) + 'px';
    }

    viewport.scrollLeft = 0;
    total = Math.max(1, Math.ceil((content.scrollWidth + gap - 1) / step));
    if (content.scrollHeight > height + 8) { fallback(); return; }
    active = true;

    var target = oldPage;
    if (restored) {
      if (restored.width === width && restored.height === height && restored.total === total) {
        target = restored.page;
      } else if (restored.total > 1) {
        target = Math.round((restored.page / (restored.total - 1)) * (total - 1));
      }
      restored = null;
    } else if (resized && oldTotal > 1) {
      target = Math.round((oldPage / (oldTotal - 1)) * (total - 1));
    }
    showPage(target, false);
  }

  function turn(direction) {
    if (!active) return;
    if (page + direction < 0 || page + direction >= total) return;
    showPage(page + direction, true);
  }

  previous.onclick = function () { turn(-1); };
  next.onclick = function () { turn(1); };
  viewport.onclick = function (event) {
    if (!active) return;
    var target = event.target || event.srcElement;
    while (target && target !== viewport) {
      var tag = target.tagName;
      if (tag === 'A' || tag === 'IMG' || tag === 'VIDEO' || tag === 'AUDIO' ||
          tag === 'BUTTON' || tag === 'INPUT' || tag === 'SELECT') return;
      target = target.parentNode;
    }
    if (window.getSelection && String(window.getSelection())) return;
    var bounds = viewport.getBoundingClientRect();
    turn(event.clientX < bounds.left + bounds.width * .3 ? -1 : 1);
  };
  document.onkeydown = function (event) {
    if (!active) return;
    event = event || window.event;
    var tag = (event.target || event.srcElement || {}).tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'BUTTON' || tag === 'A' || tag === 'SELECT') return;
    var key = event.keyCode;
    if (key === 37 || key === 33) turn(-1);
    else if (key === 39 || key === 34 || key === 32) turn(1);
    else return;
    if (event.preventDefault) event.preventDefault();
    event.returnValue = false;
  };

  window.addEventListener('resize', function () {
    window.clearTimeout(resizeTimer);
    resizeTimer = window.setTimeout(function () { layout(true); if (active) remember(); }, 180);
  });
  var images = content.getElementsByTagName('img');
  for (var i = 0; i < images.length; i++) {
    images[i].onload = images[i].onerror = function () {
      window.clearTimeout(imageTimer);
      imageTimer = window.setTimeout(function () { layout(false); }, 80);
    };
  }
  layout(false);
  if (active && !(serverRecord && typeof serverRecord.page === 'number') &&
      !(deviceRecord && typeof deviceRecord.page === 'number')) remember();
}());
