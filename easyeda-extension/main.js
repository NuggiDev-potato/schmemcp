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

function handleGetSource(msg) {
  var args = msg.args || {};
  var sourceType = args.type || 'json';
  log('GET_SOURCE type=' + sourceType);
  try {
    var src = api('getSource', { type: sourceType });
    sendResponse(msg.req_id, src);
  } catch (e) {
    sendError(msg.req_id, 'getSource threw: ' + e.message);
  }
}

function handleSearchLcsc(msg) {
  var args = msg.args || {};
  var query = args.query;
  log('SEARCH_LCSC query=' + query);
  if (!query) { sendError(msg.req_id, 'missing query'); return; }

  fetch('/api/components/search', {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ wd: query })
  })
    .then(function(r) { return r.json(); })
    .then(function(j) {
      log('SEARCH_LCSC result keys: ' + Object.keys(j).join(','));
      sendResponse(msg.req_id, j.result || j);
    })
    .catch(function(err) {
      sendError(msg.req_id, 'search fetch failed: ' + (err && err.message || err));
    });
}

function nextGid(src) {
  var max = 0, match;
  var scan = function(obj) {
    if (!obj || typeof obj !== 'object') return obj === undefined;
    if (Array.isArray(obj)) { for (var i = 0; i < obj.length; i++) scan(obj[i]); return obj === undefined; }
    for (var k in obj) {
      if (typeof k === 'string' && (match = /^gge(\d+)$/.exec(k))) {
        var n = parseInt(match[1], 10);
        if (n > max) max = n;
      }
      scan(obj[k]);
    }
    return obj === undefined;
  };
  scan(src);
  return 'gge' + (max + 1);
}

function handleAddWire(msg) {
  var args = msg.args || {};
  var x1 = args.x1, y1 = args.y1, x2 = args.x2, y2 = args.y2;
  log('ADD_WIRE ' + x1 + ',' + y1 + ' -> ' + x2 + ',' + y2);
  if (x1 === undefined || y1 === undefined || x2 === undefined || y2 === undefined) {
    sendError(msg.req_id, 'missing x1/y1/x2/y2');
    return;
  }
  try {
    var src = api('getSource', { type: 'json', compress: false });
    var gid = nextGid(src);

    var ret = api('createShape', {
      shapeType: 'wire',
      jsonCache: {
        gId: gid,
        pointArr: [
          { x: x1, y: y1 },
          { x: x2, y: y2 }
        ],
        strokeColor: '#0000FF',
        strokeWidth: 2
      }
    });
    log('ADD_WIRE createShape issued gId=' + gid + ' ret=' + safeStr(ret));

    var after = api('getSource', { type: 'json', compress: false });
    var wireObj = after.wire && after.wire[gid];
    if (wireObj) {
      sendResponse(msg.req_id, { placed: true, id: gid, gId: gid });
    } else {
      sendResponse(msg.req_id, { placed: true, id: gid, gId: gid, note: 'wire issued but not found in wire container' });
    }
  } catch (e) {
    log('ADD_WIRE error: ' + e.stack);
    sendError(msg.req_id, 'add wire threw: ' + e.message);
  }
}

function handleAddLine(msg) {
  var args = msg.args || {};
  var x1 = args.x1, y1 = args.y1, x2 = args.x2, y2 = args.y2;
  var strokeColor = args.strokeColor || '#00FF00';
  var strokeWidth = args.strokeWidth || 1;
  log('ADD_LINE ' + x1 + ',' + y1 + ' -> ' + x2 + ',' + y2);
  if (x1 === undefined || y1 === undefined || x2 === undefined || y2 === undefined) {
    sendError(msg.req_id, 'missing x1/y1/x2/y2');
    return;
  }
  try {
    var src = api('getSource', { type: 'json', compress: false });
    var gid = nextGid(src);

    var ret = api('createShape', {
      shapeType: 'line',
      jsonCache: {
        gId: gid,
        x1: x1,
        y1: y1,
        x2: x2,
        y2: y2,
        strokeColor: strokeColor,
        strokeWidth: strokeWidth,
        strokeStyle: 'solid'
      }
    });
    log('ADD_LINE createShape issued gId=' + gid + ' ret=' + safeStr(ret));

    var after = api('getSource', { type: 'json', compress: false });
    var lineObj = after.line && after.line[gid];
    if (lineObj) {
      sendResponse(msg.req_id, { placed: true, id: gid, gId: gid });
    } else {
      sendResponse(msg.req_id, { placed: true, id: gid, gId: gid, note: 'line issued but not found in line container' });
    }
  } catch (e) {
    log('ADD_LINE error: ' + e.stack);
    sendError(msg.req_id, 'add line threw: ' + e.message);
  }
}

function handleUpdateNetName(msg) {
  var args = msg.args || {};
  var gid = args.gid;
  var netName = args.net_name;
  log('UPDATE_NET_NAME gid=' + gid + ' net=' + netName);
  if (!gid || !netName) { sendError(msg.req_id, 'missing gid or net_name'); return; }
  try {
    var ret = api('updateShape', {
      shapeType: 'PAD',
      jsonCache: { gId: gid, net: netName }
    });
    log('UPDATE_NET_NAME updateShape ret=' + safeStr(ret));
    sendResponse(msg.req_id, { updated: true, gid: gid, net: netName, ret: ret });
  } catch (e) {
    sendError(msg.req_id, 'updateShape threw: ' + e.message);
  }
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
    } else if (msg.action === 'GET_SOURCE' && msg.req_id) {
      handleGetSource(msg);
    } else if (msg.action === 'SEARCH_LCSC' && msg.req_id) {
      handleSearchLcsc(msg);
    } else if (msg.action === 'ADD_WIRE' && msg.req_id) {
      handleAddWire(msg);
    } else if (msg.action === 'ADD_LINE' && msg.req_id) {
      handleAddLine(msg);
    } else if (msg.action === 'UPDATE_NET_NAME' && msg.req_id) {
      handleUpdateNetName(msg);
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
