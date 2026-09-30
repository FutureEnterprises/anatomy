// Break-even page for context deletions under prompt caching. No dependencies, no network
// beyond reading prices.json from the same folder, so it works from any path it is served under.
(function () {
  'use strict';

  var $ = function (id) { return document.getElementById(id); };
  var P = null;          // prices.json
  var mode = 'model';    // 'model' or 'custom'

  function num(el) {
    var v = parseFloat(el.value);
    return isFinite(v) ? v : NaN;
  }
  function fmtInt(x) {
    return Math.round(x).toLocaleString('en-US');
  }
  function fmtRatio(x) {
    if (!isFinite(x)) return 'never';
    return x >= 100 ? fmtInt(x) : (Math.round(x * 10) / 10).toLocaleString('en-US');
  }
  function fmtMult(x) {
    return String(parseFloat(x.toFixed(3)));
  }
  function fmtUsd(x) {
    if (!isFinite(x)) return '-';
    var a = Math.abs(x), s;
    if (a >= 1) s = a.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    else if (a >= 0.01) s = a.toFixed(4);
    else s = a.toPrecision(2);
    return (x < 0 ? '-$' : '$') + s;
  }

  function row(provider, id) {
    return P && P[provider] && P[provider].models[id];
  }
  function selected() {
    var v = $('model').value.split('|');
    return { provider: v[0], id: v[1] };
  }

  function fillModels() {
    var sel = $('model');
    sel.innerHTML = '';
    [['anthropic', 'Anthropic'], ['openai', 'OpenAI']].forEach(function (pv) {
      var g = document.createElement('optgroup');
      g.label = pv[1] + ' (snapshot ' + P[pv[0]].snapshot_date + ')';
      Object.keys(P[pv[0]].models).forEach(function (id) {
        var o = document.createElement('option');
        o.value = pv[0] + '|' + id;
        o.textContent = id;
        g.appendChild(o);
      });
      sel.appendChild(g);
    });
    sel.value = 'anthropic|claude-opus-5-5';
    if (!sel.value) sel.selectedIndex = 0;
  }

  function fillTiers() {
    var m = selected(), t = $('tier'), prev = t.value;
    t.innerHTML = '';
    var opts;
    if (m.provider === 'anthropic') {
      opts = [['5m', '5-minute cache (write 1.25x input)'], ['1h', '1-hour cache (write 2x input)']];
      $('tier-help').textContent = 'Where the rewritten tail is written.';
      $('long-wrap').classList.add('hidden');
    } else {
      var r = row('openai', m.id);
      opts = [['input', 'Rewrite billed as uncached input (as Codex logs report)']];
      if (r && r.cache_write !== undefined) opts.push(['write', 'Rewrite at the listed cache-write price']);
      $('tier-help').textContent = 'Codex rollouts report no cache-write tokens.';
      $('long-wrap').classList.toggle('hidden', !(r && r.long_context));
      if (!(r && r.long_context)) $('longctx').checked = false;
    }
    opts.forEach(function (o) {
      var e = document.createElement('option');
      e.value = o[0];
      e.textContent = o[1];
      t.appendChild(e);
    });
    if (prev && Array.prototype.some.call(t.options, function (o) { return o.value === prev; })) t.value = prev;
  }

  function applyModelPrices() {
    var m = selected(), w, r;
    if (m.provider === 'anthropic') {
      var a = row('anthropic', m.id);
      r = a.cache_read;
      w = $('tier').value === '1h' ? a.cache_write_1h : a.cache_write_5m;
    } else {
      var o = row('openai', m.id);
      var rates = ($('longctx').checked && o.long_context) ? o.long_context : o;
      r = rates.cached_input;
      w = ($('tier').value === 'write' && rates.cache_write !== undefined) ? rates.cache_write : rates.input;
    }
    $('w').value = w;
    $('r').value = r;
  }

  function setMode(m) {
    mode = m;
    $('mode-model').setAttribute('aria-pressed', String(m === 'model'));
    $('mode-custom').setAttribute('aria-pressed', String(m === 'custom'));
    ['model-wrap', 'tier-wrap'].forEach(function (id) { $(id).classList.toggle('hidden', m !== 'model'); });
    if (m === 'model') {
      $('long-wrap').classList.add('hidden');
      if (P) { fillTiers(); applyModelPrices(); }
    } else {
      $('long-wrap').classList.add('hidden');
    }
    compute();
  }

  function compute() {
    var b = num($('b')), S = num($('S')), L = num($('L')), w = num($('w')), r = num($('r'));
    var box = $('verdict');
    if ([b, S, L, w, r].some(function (x) { return isNaN(x) || x < 0; })) {
      box.className = 'verdict';
      $('verdict-big').textContent = 'Enter values';
      $('verdict-text').textContent = 'b, S, L, w and r must be zero or more.';
      ['o-ratio', 'o-need', 'o-save', 'o-cost'].forEach(function (id) { $(id).textContent = '-'; });
      return;
    }
    var R = r > 0 ? (w - r) / r : Infinity;
    var need = b > 0 ? (S / b) * R : Infinity;
    var save = b * L * r / 1e6;
    var cost = S * (w - r) / 1e6;
    var net = save - cost;
    var pays = net > 0;
    box.className = 'verdict' + (pays ? ' pays' : '');
    $('verdict-big').textContent = pays ? 'Pays' : 'Does not pay';
    var who = mode === 'model' ? selected().id + ' prices' : 'your prices';
    var needTxt = isFinite(need) ? 'pays back after ' + fmtRatio(need) + ' calls' : 'never pays back';
    $('verdict-text').textContent = 'At ' + who + ', deleting ' + fmtInt(b) + ' tokens with ' + fmtInt(S) +
      ' tokens after them ' + needTxt + '. You entered ' + fmtInt(L) + ', so the deletion ' +
      (pays ? 'saves ' + fmtUsd(net) : (net === 0 ? 'breaks even' : 'loses ' + fmtUsd(-net))) +
      ' (USD API list-price equivalent, modeled).';
    $('o-ratio').textContent = fmtRatio(R);
    $('o-need').textContent = fmtRatio(need);
    $('o-save').textContent = fmtUsd(save);
    $('o-cost').textContent = fmtUsd(cost);
  }

  function refTable() {
    $('ref-head').innerHTML = '<tr><th>Model</th><th class="num">Read (x input)</th><th class="num">5-minute cache</th><th class="num">1-hour cache</th></tr>';
    var body = $('ref-body'), html = [];
    function cell(x) { return '<td class="num">' + x + '</td>'; }
    Object.keys(P.anthropic.models).forEach(function (id) {
      var a = P.anthropic.models[id];
      html.push('<tr><td>' + id + '</td>' + cell(fmtMult(a.cache_read / a.input)) +
        cell(fmtRatio((a.cache_write_5m - a.cache_read) / a.cache_read)) +
        cell(fmtRatio((a.cache_write_1h - a.cache_read) / a.cache_read)) + '</tr>');
    });
    html.push('<tr><th>OpenAI model</th><th class="num">Read (x input)</th><th class="num">Rewrite as input</th><th class="num">At cache-write price</th></tr>');
    Object.keys(P.openai.models).forEach(function (id) {
      var o = P.openai.models[id];
      html.push('<tr><td>' + id + '</td>' + cell(fmtMult(o.cached_input / o.input)) +
        cell(fmtRatio((o.input - o.cached_input) / o.cached_input)) +
        cell(o.cache_write !== undefined ? fmtRatio((o.cache_write - o.cached_input) / o.cached_input) : 'not listed') + '</tr>');
    });
    body.innerHTML = html.join('');
  }

  function sources() {
    var f = $('sources');
    f.textContent = '';
    [['anthropic', 'Anthropic'], ['openai', 'OpenAI']].forEach(function (pv, i) {
      var s = P[pv[0]];
      if (i) f.appendChild(document.createTextNode(' '));
      f.appendChild(document.createTextNode(pv[1] + ' list prices, snapshot ' + s.snapshot_date + ', from '));
      var a = document.createElement('a');
      a.href = s.source_url;
      a.rel = 'noreferrer noopener';
      a.textContent = s.source_url.replace(/^https:\/\//, '');
      f.appendChild(a);
      f.appendChild(document.createTextNode('. ' + s.note));
    });
  }

  function bind() {
    $('mode-model').addEventListener('click', function () { if (P) setMode('model'); });
    $('mode-custom').addEventListener('click', function () { setMode('custom'); });
    $('model').addEventListener('change', function () { fillTiers(); applyModelPrices(); compute(); });
    $('tier').addEventListener('change', function () { applyModelPrices(); compute(); });
    $('longctx').addEventListener('change', function () { applyModelPrices(); compute(); });
    ['w', 'r'].forEach(function (id) {
      $(id).addEventListener('input', function () { if (mode !== 'custom') setMode('custom'); else compute(); });
    });
    ['b', 'S', 'L'].forEach(function (id) { $(id).addEventListener('input', compute); });
  }

  function offline(msg) {
    var n = $('load-note');
    n.textContent = msg;
    n.classList.remove('hidden');
    $('mode-model').disabled = true;
    $('w').value = 5;
    $('r').value = 0.2;
    setMode('custom');
  }

  bind();
  fetch('prices.json', { cache: 'no-cache' })
    .then(function (res) { if (!res.ok) throw new Error(String(res.status)); return res.json(); })
    .then(function (data) {
      P = data;
      fillModels();
      refTable();
      sources();
      setMode('model');
    })
    .catch(function () {
      offline('The price snapshot (prices.json) could not be loaded, so only your own prices work here. ' +
        'Serve this folder over http, for example with python3 -m http.server, to pick a model.');
    });
})();
