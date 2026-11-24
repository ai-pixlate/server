// 글 맞추기와 위치 검사. 페이지가 열리면 자동으로 실행되고 결과를 window.__aplusReport 에 둔다.
// 1) 칸 안에서 줄바꿈 → 2) 넘치면 글자 크기를 1px씩 줄임(최소 = 기본 크기의 80%)
// 3) 그래도 넘치면 자르지 않고 그대로 두고 "넘침"으로 기록한다. 말줄임표는 쓰지 않는다.
(function () {
  function px(v) { return parseFloat(v) || 0; }

  function fitOne(el) {
    var t = el.querySelector('.t');
    if (!t || !t.textContent.trim()) return null;
    var base = +el.dataset.size, maxL = +el.dataset.lines, lhr = +el.dataset.lh;
    var min = Math.ceil(base * 0.8 - 1e-9);
    var boxH = el.clientHeight, boxW = el.clientWidth;
    var cs = getComputedStyle(t);
    var padV = px(cs.paddingTop) + px(cs.paddingBottom);
    var size = base, s;
    function measure() {
      el.style.fontSize = size + 'px';
      var h = t.offsetHeight, lh = size * lhr;
      var lines = Math.max(1, Math.round((h - padV) / lh));
      var wOver = Math.max(0, t.scrollWidth - boxW);
      var r = t.getBoundingClientRect();
      wOver = Math.max(wOver, r.width - boxW);
      var hOver = Math.max(0, h - padV - boxH);  // 알약 라벨처럼 글 둘레 여백(padding)은 칸 높이에 셈하지 않음
      return { h: h, lh: lh, lines: lines, wOver: wOver, hOver: hOver,
               ok: lines <= maxL && hOver <= 0.5 && wOver <= 0.5 };
    }
    s = measure();
    while (!s.ok && size > min) { size = Math.max(min, size - 1); s = measure(); }
    var rec = { name: el.dataset.name, area: el.dataset.area || '', base: base, final: size, min: min,
                lines: s.lines, max_lines: maxL, overflow: !s.ok };
    if (!s.ok) {
      var allowed = Math.max(0, Math.min(maxL, Math.floor((boxH - padV + 0.5) / s.lh)));
      rec.allowed_lines = allowed;
      rec.overflow_lines = Math.max(0, s.lines - allowed);
      rec.width_over = Math.round(s.wOver);
      rec.height_over = Math.round(s.hOver * 10) / 10;
      el.classList.add('is-overflow');
    }
    return rec;
  }

  function rectOf(el) {
    if (!el.classList.contains('fit')) return el.getBoundingClientRect();
    var t = el.querySelector('.t');
    var tr = t.getBoundingClientRect();
    if (getComputedStyle(t).display === 'inline-block') {
      if (!el.classList.contains('pill-label')) return tr;
      var cs = getComputedStyle(t);  // 알약 라벨: 둘레 여백은 빼고 글자 부분만 위치 검사
      return { left: tr.left + px(cs.paddingLeft), right: tr.right - px(cs.paddingRight),
               top: tr.top + px(cs.paddingTop), bottom: tr.bottom - px(cs.paddingBottom), width: tr.width };
    }
    var rg = document.createRange(); rg.selectNodeContents(t);
    var gr = rg.getBoundingClientRect();
    if (!gr.width) return null;
    return { left: gr.left, right: gr.right, top: tr.top, bottom: tr.bottom, width: gr.width };
  }

  async function run() {
    try { await document.fonts.ready; } catch (e) {}
    await Promise.all(Array.from(document.images).map(function (i) {
      return i.complete ? 0 : new Promise(function (r) { i.onload = i.onerror = r; });
    }));
    // 쌓는 글 칸(.flow) 안 제목 밑줄은 글 바로 아래 블록으로 넣는다 → 아래 글을 그만큼 민다
    document.querySelectorAll('.flow .fit[data-underline]').forEach(function (el) {
      var u = document.createElement('div');
      u.className = 'uline-flow ' + el.dataset.underline;
      el.appendChild(u);
    });
    var texts = [], recOf = new Map();
    document.querySelectorAll('.fit').forEach(function (el) {
      var r = fitOne(el); if (r) { texts.push(r); recOf.set(el, r); }
    });
    // 위에서부터 쌓는 글 칸(.flow): 전체 높이가 limit 를 넘으면 data-shrink 순서로 줄인다
    var pushes = [];
    document.querySelectorAll('.flow').forEach(function (fl) {
      var limit = +fl.dataset.limit, top = fl.getBoundingClientRect().top;
      var kids = Array.from(fl.querySelectorAll('.fit'));
      if (!kids.length) return;
      function bottom() {
        return Math.max.apply(null, kids.map(function (k) {
          return k.querySelector('.t').getBoundingClientRect().bottom; })) - top;
      }
      var order = kids.filter(function (k) { return +k.dataset.shrink > 0; })
                      .sort(function (a, b) { return a.dataset.shrink - b.dataset.shrink; });
      order.forEach(function (k) {
        var rec = recOf.get(k); if (!rec) return;
        while (bottom() > limit + 0.5 && rec.final > rec.min) {
          rec.final -= 1; k.style.fontSize = rec.final + 'px';
          rec.lines = Math.max(1, Math.round(k.querySelector('.t').offsetHeight / (rec.final * +k.dataset.lh)));
        }
      });
      fl.querySelectorAll('.slot[data-push]').forEach(function (s) {
        var minH = parseFloat(s.style.minHeight) || 0, extra = s.offsetHeight - minH;
        if (extra > 0.5) pushes.push({ name: (s.querySelector('.fit') || {}).dataset.name,
                                       px: Math.round(extra * 10) / 10 });
      });
      var over = bottom() - limit;
      if (over > 0.5) {
        var last = kids[kids.length - 1], lr = recOf.get(last);
        var lh = lr ? lr.final * +last.dataset.lh : 20;
        texts.push({ name: fl.dataset.name, area: '', base: null, final: null, min: null,
                     lines: null, max_lines: null, overflow: true, flow: true,
                     height_over: Math.round(over * 10) / 10, overflow_lines: Math.ceil(over / lh - 0.05) });
        fl.classList.add('is-overflow');
      }
    });
    // 제목 밑줄(쌓는 칸 밖): 실제 글 아래 6px 에 그림. bar = 40×3 강조색, line = 글 너비 1px
    document.querySelectorAll('.fit[data-underline]').forEach(function (el) {
      if (el.closest('.flow')) return;
      var r = rectOf(el); if (!r) return;
      var kind = el.dataset.underline, u = document.createElement('div');
      var center = getComputedStyle(el).textAlign === 'center';
      var w = kind === 'bar' ? 40 : r.right - r.left, h = kind === 'bar' ? 3 : 1;
      var x = kind === 'bar' ? (center ? (r.left + r.right) / 2 - 20 : r.left) : r.left;
      u.className = 'uline ' + kind; u.dataset.deco = 'underline'; u.dataset.owner = el.dataset.name;
      if (el.dataset.vgroup) u.dataset.vgroup = '1';
      Object.assign(u.style, { left: x + 'px', top: (r.bottom + 6) + 'px', width: w + 'px', height: h + 'px' });
      document.body.appendChild(u);
    });
    // 배치 변형 '세로 가운데': data-vgroup 묶음을 실제 글 높이 기준으로 세로 가운데(위아래 여백 24px 안)로 옮김
    var vg = Array.from(document.querySelectorAll('[data-vgroup]')), vshift = null;
    if (vg.length) {
      var t0 = 1e9, b0 = -1e9;
      vg.forEach(function (el) { var r = rectOf(el); if (r) { t0 = Math.min(t0, r.top); b0 = Math.max(b0, r.bottom); } });
      var dy = 150 - (t0 + b0) / 2;
      dy = Math.max(24 - t0, Math.min(276 - b0, dy));
      vg.forEach(function (el) { el.style.top = (parseFloat(el.style.top) + dy) + 'px'; el.dataset.moved = '1'; });
      vshift = Math.round(dy * 10) / 10;
    }
    // 여백 장식: 글·이미지·구역을 가리지 않는 빈 모서리에만 (최대 2개)
    var md = document.getElementById('mdeco'), mdeco = null;
    if (md) {
      var kind = md.dataset.kind, obs = [];
      document.querySelectorAll('[data-name], .zone, .panel, .cardbg, .backdrop, .uline, .tline, .conn path').forEach(function (el) {
        var r = el.classList && el.classList.contains('fit') ? rectOf(el) : el.getBoundingClientRect();
        if (r && r.right - r.left > 0) obs.push(r);
      });
      var C = { dots: [[14, 14, 34, 34], [922, 252, 34, 34], [922, 14, 34, 34], [14, 252, 34, 34]],
                corner: [[12, 12, 26, 26], [932, 262, 26, 26], [932, 12, 26, 26], [12, 262, 26, 26]],
                circle: [[-60, -60, 150, 150], [880, 210, 150, 150], [880, -60, 150, 150], [-60, 210, 150, 150]] }[kind];
      var sides = [['Top', 'Left'], ['Bottom', 'Right'], ['Top', 'Right'], ['Bottom', 'Left']];
      var placed = 0;
      C.forEach(function (c, i) {
        if (placed >= 2) return;
        var v = { left: Math.max(0, c[0]), top: Math.max(0, c[1]), right: Math.min(970, c[0] + c[2]), bottom: Math.min(300, c[1] + c[3]) };
        var hit = obs.some(function (o) { return v.left < o.right + 6 && v.right > o.left - 6 && v.top < o.bottom + 6 && v.bottom > o.top - 6; });
        if (hit) return;
        var d = document.createElement('div');
        d.className = 'mdeco ' + kind; d.dataset.deco = 'margin';
        Object.assign(d.style, { left: c[0] + 'px', top: c[1] + 'px', width: c[2] + 'px', height: c[3] + 'px' });
        if (kind === 'corner') sides[i].forEach(function (s) { d.style['border' + s + 'Width'] = '2px'; });
        document.body.insertBefore(d, document.body.firstChild);
        placed++;
      });
      mdeco = placed;
    }
    // 장식(밑줄)이 다른 글·이미지를 가리는지 검사
    var deco_hits = [];
    document.querySelectorAll('.uline').forEach(function (u) {
      var ur = u.getBoundingClientRect();
      document.querySelectorAll('[data-name]').forEach(function (el) {
        if (el.dataset.name === u.dataset.owner) return;
        var r = el.classList.contains('fit') ? rectOf(el) : el.getBoundingClientRect();
        if (r && ur.left < r.right && ur.right > r.left && ur.top < r.bottom && ur.bottom > r.top)
          deco_hits.push({ deco: '제목 밑줄', hits: el.dataset.name });
      });
    });
    var spec = window.__aplusSpec || {}, outside = [];
    document.querySelectorAll('[data-area]').forEach(function (el) {
      if (el.dataset.moved) return;  // 세로 가운데로 일부러 옮긴 것
      var a = spec[el.dataset.area]; if (!a) return;
      var r = rectOf(el); if (!r || !r.width) return;
      var d = { left: a.x - r.left, top: a.y - r.top, right: r.right - (a.x + a.w), bottom: r.bottom - (a.y + a.h) };
      var worst = 0, sides = [];
      for (var k in d) { if (d[k] > 1) { sides.push(k); worst = Math.max(worst, d[k]); } }
      if (sides.length) outside.push({ name: el.dataset.name, area: el.dataset.area,
                                       px: Math.round(worst * 10) / 10, sides: sides });
    });
    var broken = Array.from(document.images).filter(function (i) { return !i.naturalWidth; })
                      .map(function (i) { return i.getAttribute('src'); });
    window.__aplusReport = { texts: texts, outside: outside, broken_images: broken, pushes: pushes,
                             vshift: vshift, margin_deco: mdeco, deco_hits: deco_hits };
  }
  run();
})();
