// extension-mcpbridge-id
var WS_URL = 'ws://127.0.0.1:3579';
var RECONNECT_MS = 3000;
var ws = null;
var reconnectTimer = null;

function log(msg) {
  console.log('[MCP Bridge] ' + msg);
}

function safeStr(v) {
  if (v === undefined) return 'undefined';
  if (v === null) return 'null';
  var s;
  try { s = JSON.stringify(v); } catch(e) { s = String(v); }
  if (typeof s !== 'string') s = String(s);
  return s;
}

function sendResponse(reqId, data) {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  try {
    ws.send(JSON.stringify({ req_id: reqId, status: 'success', data: data }));
  } catch (e) {
    log('Send error: ' + e.message);
  }
}

function sendError(reqId, errorMsg) {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  try {
    ws.send(JSON.stringify({ req_id: reqId, status: 'error', error: String(errorMsg) }));
  } catch (e) {
    log('Send error: ' + e.message);
  }
}

function handleExecJs(msg) {
  var code = msg.args ? msg.args.code : msg.code;
  log('EXEC_JS: ' + (code || '').substring(0, 200));

  try {
    var result = eval(code);
    log('EXEC_JS raw result type: ' + typeof result);

    if (result !== null && typeof result === 'object' && typeof result.then === 'function') {
      log('EXEC_JS result is thenable, waiting...');
      result.then(function(val) {
        log('EXEC_JS resolved: ' + safeStr(val).substring(0, 300));
        sendResponse(msg.req_id, val);
      }).catch(function(err) {
        log('EXEC_JS rejected: ' + (err.message || String(err)));
        sendError(msg.req_id, err.message || String(err));
      });
    } else {
      log('EXEC_JS result: ' + safeStr(result).substring(0, 300));
      sendResponse(msg.req_id, result);
    }
  } catch (e) {
    log('EXEC_JS error: ' + e.message);
    sendError(msg.req_id, e.message);
  }
}

var LCSC_LOOKUP_URL = 'https://easyeda.com/api/products/CODE/components?version=6.4.19.5';

function handlePlaceLcsc(msg) {
  var args = msg.args || {};
  var code = args.lcscPartNumber;
  var posX = args.x;
  var posY = args.y;
  log('PLACE_LCSC: ' + safeStr(args));

  if (!code) { sendError(msg.req_id, 'missing lcscPartNumber'); return; }

  var url = LCSC_LOOKUP_URL.replace('CODE', encodeURIComponent(code));
  fetch(url)
    .then(function(r) { return r.json(); })
    .then(function(j) {
      var res = j && j.result;
      if (!j || j.success !== true || !res) {
        sendError(msg.req_id, 'LCSC lookup failed for ' + code + ': ' + safeStr(j).substring(0, 300));
        return;
      }
      if (!res.uuid || !res.datastrid) {
        sendError(msg.req_id, 'LCSC lookup returned no uuid/datastrid for ' + code + ': ' + safeStr(res).substring(0, 300));
        return;
      }
      log('LCSC lookup OK for ' + code + ' uuid=' + res.uuid + ' datastrid=' + res.datastrid);
      placeLibShape(msg.req_id, code, res.uuid, res.datastrid, posX, posY);
    })
    .catch(function(err) {
      sendError(msg.req_id, 'LCSC fetch failed: ' + (err && err.message || err));
    });
}

function placeLibShape(reqId, code, uuid, datastrid, x, y) {
  var before, beforeKeys = [];
  try { before = api('getSource', { type: 'json' }); } catch (e) { before = null; }
  if (before && before.schlib) beforeKeys = Object.keys(before.schlib);

  var ret;
  try {
    ret = api('createShape', {
      shapeType: 'schlib',
      uuid: uuid,
      datastrid: datastrid,
      from: 'system',
      title: code,
      x: x,
      y: y
    });
  } catch (e) {
    sendError(reqId, 'createShape threw: ' + e.message);
    return;
  }
  log('createShape issued for ' + code + ', ret=' + safeStr(ret) + ', waiting for shape...');

  var attempts = 0;
  var timer = setInterval(function() {
    attempts++;
    var after, afterKeys = [];
    try { after = api('getSource', { type: 'json' }); } catch (e) { after = null; }
    if (after && after.schlib) afterKeys = Object.keys(after.schlib);
    var added = afterKeys.filter(function(k) { return beforeKeys.indexOf(k) === -1; });
    if (added.length) {
      clearInterval(timer);
      log('createShape placed gId=' + added[0]);
      sendResponse(reqId, { placed: true, id: added[0], gId: added[0] });
      return;
    }
    if (attempts >= 20) {
      clearInterval(timer);
      sendError(reqId, 'createShape issued but no new shape appeared within 10s');
    }
  }, 500);
}

function connect() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;

  log('Connecting to ' + WS_URL + '...');
  try {
    ws = new WebSocket(WS_URL);
  } catch (e) {
    log('WebSocket constructor failed: ' + e);
    scheduleReconnect();
    return;
  }

  ws.onopen = function() {
    log('Connected');
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
  };

  ws.onmessage = function(event) {
    var msg;
    try { msg = JSON.parse(event.data); } catch (e) { return; }
    log('Received action=' + msg.action + ' req_id=' + msg.req_id);

    if (msg.action === 'PLACE_LCSC' && msg.req_id) {
      handlePlaceLcsc(msg);
    } else if (msg.action === 'EXEC_JS' && msg.req_id) {
      handleExecJs(msg);
    }
  };

  ws.onclose = function(evt) {
    log('Disconnected code=' + evt.code);
    ws = null;
    scheduleReconnect();
  };

  ws.onerror = function() {
    log('WebSocket error');
  };
}

function scheduleReconnect() {
  if (reconnectTimer) return;
  reconnectTimer = setTimeout(function() { reconnectTimer = null; connect(); }, RECONNECT_MS);
}

log('Extension loaded');
setTimeout(connect, 2000);
