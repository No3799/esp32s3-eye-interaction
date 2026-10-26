
const $ = (id) => document.getElementById(id);

/* 把"多少秒前"换成人话。
 *
 * 为什么必须换：求助列表默认显示最近几条，而它们可能是几天前的（设备不是一直在用）。
 * 改版前直接写 age_seconds.toFixed(0) + ' 秒前'，9/28 的事件在 10 月打开就显示
 * 「2155226 秒前」—— 没人读得出来这是 25 天。这个数字是真的，但不可读等于没有。
 *
 * 10 秒以内保留一位小数（看画面新鲜度时秒级有意义），超过就取整。 */
function humanAge(sec) {
  if (sec === null || sec === undefined || !isFinite(sec)) return '—';
  if (sec < 0) sec = 0;
  if (sec < 10) return sec.toFixed(1) + ' 秒前';
  if (sec < 60) return Math.round(sec) + ' 秒前';
  if (sec < 3600) return Math.round(sec / 60) + ' 分钟前';
  if (sec < 86400) return Math.round(sec / 3600) + ' 小时前';
  return Math.round(sec / 86400) + ' 天前';
}
const fmt = (v, n = 3) => (v === null || v === undefined) ? '—' : Number(v).toFixed(n);
const mag = (x, y, z) => Math.sqrt(x * x + y * y + z * z);
/* 字节 → MB，保留 2 位小数；null/undefined 显示为"—"而不是 0 */
const mb = (b) => (b === null || b === undefined) ? '—' : (Number(b) / 1048576).toFixed(2);
/* 取数值，缺失时为 null（不要退化成 0，避免假读数） */
const num = (v) => (v === null || v === undefined) ? null : Number(v);

/* 曲线颜色取自 CSS 令牌，不写死在 JS 里 —— 深色模式下要换一组更亮的，
   写死就会在深底上画出一条发闷的线。 */
const SERIES = [
  ['ax', '加速度 X', 'ax'],
  ['ay', '加速度 Y', 'ay'],
  ['az', '加速度 Z', 'az'],
];
/* 画布颜色一次读齐。不能只读一次就完 —— 系统切换深色模式时要跟着变，
   见下面的 matchMedia 监听。 */
let THEME = {};
function readTheme() {
  const cs = getComputedStyle(document.documentElement);
  const v = (n) => cs.getPropertyValue(n).trim();
  THEME = {
    ax: v('--s-ax'), ay: v('--s-ay'), az: v('--s-az'),
    muted: v('--muted'), muted2: v('--muted-2'), line: v('--line'),
    card: v('--card'), text: v('--text'), border: v('--border-strong'),
  };
}
readTheme();
let LAST = [];
let PAUSED = false;      /* 板子当前是否处于「暂停周期上报」，由 /api/latest 带回来 */

/* 服务端 API 地址自适应：
 *   从服务器打开（http/https）→ 跟随当前地址（本机/局域网/内网穿透/云上都通用）；
 *   直接双击 html（file://）→ 退回本机 8000 端口。 */
const API_BASE = (location.protocol === 'http:' || location.protocol === 'https:')
  ? location.origin
  : 'http://127.0.0.1:8000';

/* ---------- 摄像头取流方式 ----------
 * 'server'（默认）：走服务器中转，网页不必知道板子 IP，板子只维持 1 路连接。
 * 'direct'：网页直连板子 81 端口，仅用于排查故障（板子最多扛 2 路）。 */
const CAM_MODE = 'server';
const CAM_BASE = 'http://10.1.41.160:81';   // 仅 'direct' 模式使用
const CAM_STREAM   = CAM_MODE === 'server' ? API_BASE + '/api/camera' : CAM_BASE + '/stream';
const CAM_HEARTBEAT = CAM_MODE === 'server' ? API_BASE + '/api/camera/status' : CAM_BASE + '/ping';

/* ---------- 摄像头：自带掉线自愈 ----------
 * 1) 每次重连换 URL 时间戳，避开浏览器缓存的失败结果；
 * 2) 失败后 2s→4s…封顶 10s 退避重试，不一次性判死；
 * 3) 每 3 秒心跳：状态端点超时 = 服务被堵死 → 重建；
 *    端点活着但报告无人观看 = 画面其实已停在最后一帧 → 同样重建。 */
(function mountCamera() {
  const img = document.getElementById('camStream');
  const st  = document.getElementById('camStatus');
  const btn = document.getElementById('btnCamRe');
  if (!img) return;

  let retries = 0, retryTimer = null, everOK = false, deadVotes = 0, zeroVotes = 0;
  const text = (s) => { st.textContent = s; };

  function connect() {
    clearTimeout(retryTimer);
    text(retries === 0 ? '连接中…' : '第 ' + retries + ' 次重连…');
    img.onload = function () { retries = 0; zeroVotes = 0; everOK = true; text('实时画面 · 已连接'); };
    img.onerror = function () {
      if (++retries > 20) { text('未连接 · 请给板子重新上电后刷新本页面'); return; }
      const wait = Math.min(2 * retries, 10);
      text('未连接 · ' + wait + ' 秒后重试（第 ' + retries + ' 次）');
      retryTimer = setTimeout(connect, wait * 1000);
    };
    img.src = CAM_STREAM + '?nocache=' + Date.now();
  }

  function hardReconnect(reason) {
    text('检测到' + reason + '，正在重建连接…');
    retries = 0; zeroVotes = 0; deadVotes = 0;
    img.onload = img.onerror = null;
    img.src = '';                       // 先掐断，释放服务端通路
    setTimeout(connect, 300);
  }

  function parseLive(r, body) {
    if (CAM_MODE === 'server') {
      try {
        const j = JSON.parse(body);
        const age = j.last_frame_age;
        const flowing = j.state === 'streaming' && age !== null && age < 3;
        const note = age === null ? '尚无画面' : ' ' + humanAge(age) + '的帧';
        return { alive: flowing, note: note };
      } catch (e) { return { alive: false, note: '状态解析失败' }; }
    }
    const m = body.match(/clients=(\d+)\/(\d+)/);
    const clients = m ? Number(m[1]) : null;
    return { alive: r.ok && clients !== null && clients > 0, note: clients === null ? '' : clients + ' 路直连' };
  }

  async function heartbeat() {
    if (document.hidden) return;        // 页面在后台就别添乱
    const ctl = new AbortController();
    const to = setTimeout(() => ctl.abort(), 4000);
    try {
      const r = await fetch(CAM_HEARTBEAT + '?nocache=' + Date.now(),
                            { cache: 'no-store', signal: ctl.signal });
      const body = await r.text();
      clearTimeout(to);
      deadVotes = 0;
      const s = parseLive(r, body);
      if (!s.alive) {
        if (++zeroVotes >= 2) hardReconnect('画面已停止');
        else text('实时画面 · 已连接（' + s.note.trim() + '）');
      } else {
        zeroVotes = 0;
        text(everOK ? '实时画面 · 已连接（' + s.note.trim() + '）' : '服务在线，正在取画面…');
      }
    } catch (e) {
      clearTimeout(to);
      if (++deadVotes >= 2) {
        deadVotes = 0;
        hardReconnect(CAM_MODE === 'server' ? '服务器无响应' : '板子无响应');
      } else if (retries === 0) text('连接中…');
    }
  }

  btn.addEventListener('click', function () {
    retries = 0; deadVotes = 0; zeroVotes = 0; everOK = false;
    hardReconnect('手动重连');
  });

  connect();
  setInterval(heartbeat, 3000);
})();

/* 三条曲线的图例。 */
function renderLegend() {
  $('legend').innerHTML = SERIES
    .map(([, name, key]) =>
      `<span><i style="background:${THEME[key]}"></i>${name}</span>`).join('');
}

/* ---------- 时间窗 ----------
 * 改版前固定画最近 60 条（＝1 分钟），标题写着"趋势"却看不出趋势。
 *
 * 时间范围由**服务器按时间过滤**（minutes 参数），不是"取最近 N 条"。
 * 这两者差别很大：设备有离线空档时，"最近 3600 条"实测横跨 4.6 天 ——
 * 界面写着"最近 1 小时"、画的却是 4 天，又是一条看着对其实是错的证据。
 * limit 只当安全上限用。
 *
 * 跨度越大，重拉间隔越长：1 分钟每秒重拉无所谓，1 小时是 3600 条，
 * 每秒拉一次纯属浪费。两次重拉之间用 /api/latest 那条往队首补，曲线始终是活的。 */
const RANGES = {
  '1m':  { minutes: 1,  limit: 180,  refetchMs: 1000,  stat: '最近 1 分钟' },
  '10m': { minutes: 10, limit: 1200, refetchMs: 5000,  stat: '最近 10 分钟' },
  '1h':  { minutes: 60, limit: 4200, refetchMs: 15000, stat: '最近 1 小时' },
};
let CUR_RANGE = '1m';
let lastHistMs = 0;

/* 纵轴的**最小跨度**（g）。改版前没有这一条：板子静止时三轴只有约 ±15 mg 的
 * 本底噪声，自动缩放会把这 0.06 g 撑满整个图高，看起来像剧烈抖动 ——
 * 把正常的物理噪声显示成了故障。加了下限之后，静止就是一条平坦的线。 */
const MIN_Y_SPAN = 0.25;

let CHART_GEO = null;

/* 曲线绘制：只在"数据变了"或"尺寸变了"时才真正重画。
 * 原来每秒都重设 canvas.width/height（等于重新分配画布），是主要性能开销。 */
let lastSig = '';
function drawChart(force) {
  const cv = $('chart');
  const hv = $('chartHover');
  const dpr = window.devicePixelRatio || 1;
  /* 分区不可见时 clientWidth 是 0 —— 原来那句 `cv.clientWidth || 900` 会真的
     按 900px 画一张图（还每秒重画一次），切回来再被拉伸变形。
     这里直接不画；切到可见分区时由 applyTab 强制重画一次。 */
  if (!cv.clientWidth) { CHART_GEO = null; return; }
  const w = cv.clientWidth;
  const h = 240;
  const needW = Math.round(w * dpr), needH = Math.round(h * dpr);

  const sig = CUR_RANGE + ':' + LAST.length + ':' + (LAST[0] ? LAST[0].id : '');
  if (!force && sig === lastSig && cv.width === needW && cv.height === needH) return;
  lastSig = sig;

  if (cv.width !== needW || cv.height !== needH) { cv.width = needW; cv.height = needH; }
  /* 悬停层与数据层同尺寸。在这里设一次，而不是每次鼠标移动都设 ——
     重复设 canvas.width 会清空并重新分配位图，鼠标一动就卡。 */
  if (hv && (hv.width !== needW || hv.height !== needH)) { hv.width = needW; hv.height = needH; }
  const g = cv.getContext('2d');
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, w, h);

  const pts = LAST.slice().reverse();      // 服务端返回倒序，这里还原成时间正序

  if (!pts.length) {
    CHART_GEO = null;
    g.fillStyle = THEME.muted2;
    g.font = '13px sans-serif';
    g.textAlign = 'center';
    g.fillText('暂无数据', w / 2, h / 2);
    clearHover();
    return;
  }

  const pl = 48, pr = 12, pt = 10, pb = 24;

  /* 纵轴仍用真实 min/max，只兜一个最小跨度。
   * 这里**刻意不用**分位数裁掉极值 —— 那是"只要主趋势"的场景才合适；
   * 本页面盯的正是加速度峰值，把尖峰裁掉等于把最该看见的东西藏起来。 */
  let min = Infinity, max = -Infinity;
  pts.forEach(p => SERIES.forEach(([k]) => {
    const v = Number(p[k]);
    if (!isFinite(v)) return;
    if (v < min) min = v;
    if (v > max) max = v;
  }));
  if (!isFinite(min) || !isFinite(max)) { min = -1; max = 1; }
  if (max - min < MIN_Y_SPAN) {
    const mid = (max + min) / 2;
    min = mid - MIN_Y_SPAN / 2;
    max = mid + MIN_Y_SPAN / 2;
  }
  const pad = (max - min) * 0.08;
  min -= pad; max += pad;

  /* 横轴按**真实时间**定位，不按数组下标。
   * 按下标画的话，设备离线的那段空档会被压缩成一条水平直线 ——
   * 看起来像"这段时间一直是这个值"，而事实是这段时间根本没有数据。
   * 那又是一条"看着对其实是错的"图形证据。 */
  const t0 = pts[0].server_ms, t1 = pts[pts.length - 1].server_ms;
  const tSpan = Math.max(1, t1 - t0);
  const X = i => pl + (w - pl - pr) * (pts.length === 1 ? 0.5 : (pts[i].server_ms - t0) / tSpan);
  const Y = v => pt + (h - pt - pb) * (1 - (v - min) / (max - min));

  /* 断线阈值：相邻两点时间差超过"正常间隔的 5 倍"（且至少 3 秒）就认定中间是空档，
   * 在空档处把曲线断开，而不是连过去。1Hz 下正常间隔约 1 秒，阈值约 5 秒。 */
  const dts = [];
  for (let i = 1; i < pts.length; i++) dts.push(pts[i].server_ms - pts[i - 1].server_ms);
  dts.sort((a, b) => a - b);
  const medDt = dts.length ? dts[Math.floor(dts.length / 2)] : 1000;
  const GAP_MS = Math.max(3000, medDt * 5);

  g.font = '11px sans-serif';
  g.strokeStyle = THEME.line;
  g.lineWidth = 1;
  g.textAlign = 'right';
  for (let i = 0; i <= 4; i++) {
    const v = min + (max - min) * i / 4;
    const yy = Math.round(Y(v)) + 0.5;
    g.beginPath(); g.moveTo(pl, yy); g.lineTo(w - pr, yy); g.stroke();
    g.fillStyle = THEME.muted;
    g.fillText(v.toFixed(2), pl - 8, yy + 4);
  }

  g.fillStyle = THEME.muted;
  g.textAlign = 'left';
  g.fillText((pts[0].server_time || '').slice(11), pl, h - 6);
  g.textAlign = 'right';
  g.fillText((pts[pts.length - 1].server_time || '').slice(11), w - pr, h - 6);

  /* 抽稀：1 小时窗有 3600 个点，画布只有约 900 像素宽。
   * 逐点连线既费性能又会糊成一团。点比像素多时改成按**像素列**归并，
   * 每列画一条 min→max 竖线段（形状和峰值都保住，只丢掉列内先后顺序 ——
   * 这个尺度下本来也看不出来）；点不多时照旧画折线，线条更好看。 */
  const plotW = Math.max(1, Math.round(w - pl - pr));
  const dense = pts.length > plotW / 2;

  SERIES.forEach(([k, , key]) => {
    g.strokeStyle = THEME[key];
    g.lineWidth = dense ? 1.2 : 1.8;
    g.lineJoin = 'round';
    g.beginPath();

    if (!dense) {
      pts.forEach((p, i) => {
        const v = Number(p[k]);
        if (!isFinite(v)) return;
        const xx = X(i), yy = Y(v);
        /* 空档处 moveTo 而不是 lineTo：断开，不连成一条假的水平线 */
        if (i === 0 || pts[i].server_ms - pts[i - 1].server_ms > GAP_MS) g.moveTo(xx, yy);
        else g.lineTo(xx, yy);
      });
    } else {
      const lo = new Float64Array(plotW).fill(Infinity);
      const hi = new Float64Array(plotW).fill(-Infinity);
      for (let i = 0; i < pts.length; i++) {
        const v = Number(pts[i][k]);
        if (!isFinite(v)) continue;
        let c = Math.round(X(i) - pl);
        if (c < 0) c = 0; else if (c >= plotW) c = plotW - 1;
        if (v < lo[c]) lo[c] = v;
        if (v > hi[c]) hi[c] = v;
      }
      for (let c = 0; c < plotW; c++) {
        if (lo[c] === Infinity) continue;      // 空档：这一列什么都不画
        const xx = pl + c;
        const y1 = Y(lo[c]), y2 = Y(hi[c]);
        g.moveTo(xx, y1);
        /* 同一列内数值几乎不变时给个极短竖线，否则这段曲线会整段消失 */
        g.lineTo(xx, Math.abs(y2 - y1) < 0.7 ? y1 - 0.7 : y2);
      }
    }
    g.stroke();
  });

  /* 几何留给悬停层复用 —— 悬停时绝不能重画数据层，否则鼠标一动画面就抖 */
  CHART_GEO = { pts, X, Y, pl, pr, pt, pb, w, h, min, max };

  const st = $('chartStat');
  if (st) {
    const a = pts[0], b = pts[pts.length - 1];
    st.textContent = RANGES[CUR_RANGE].stat + ' · ' + pts.length + ' 点'
      + (a.server_time && b.server_time
          ? ' · ' + a.server_time.slice(11) + '→' + b.server_time.slice(11) : '');
  }
  renderLegend();
}

/* ---------- 鼠标悬停读数 ----------
 * 单独一层画布：十字线画在它上面，数据层的曲线一个像素都不用重画。 */
function clearHover() {
  const hv = $('chartHover');
  if (!hv) return;
  const g = hv.getContext('2d');
  g.setTransform(1, 0, 0, 1, 0, 0);
  g.clearRect(0, 0, hv.width, hv.height);
}

function drawHover(ev) {
  const hv = $('chartHover');
  if (!hv || !CHART_GEO) return;
  const { pts, X, Y, pl, pr, pt, pb, w, h } = CHART_GEO;
  const dpr = window.devicePixelRatio || 1;
  const rect = hv.getBoundingClientRect();
  const mx = ev.clientX - rect.left;
  if (mx < pl - 6 || mx > w - pr + 6) { clearHover(); return; }

  /* 找横向最近的采样点 */
  let idx = 0, best = Infinity;
  for (let i = 0; i < pts.length; i++) {
    const d = Math.abs(X(i) - mx);
    if (d < best) { best = d; idx = i; }
  }
  const p = pts[idx];
  const g = hv.getContext('2d');
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, w, h);

  const xx = X(idx);
  g.strokeStyle = THEME.border;
  g.lineWidth = 1;
  g.setLineDash([3, 3]);
  g.beginPath();
  g.moveTo(Math.round(xx) + .5, pt);
  g.lineTo(Math.round(xx) + .5, h - pb);
  g.stroke();
  g.setLineDash([]);

  SERIES.forEach(([k, , key]) => {
    const y = Y(Number(p[k]));
    g.fillStyle = THEME[key];
    g.beginPath(); g.arc(xx, y, 2.6, 0, Math.PI * 2); g.fill();
    g.strokeStyle = THEME.card; g.lineWidth = 1.2; g.stroke();
  });

  /* 读数框：靠右时翻到左侧，免得被画布边缘截掉 */
  const rows = SERIES.map(([k, name, key]) =>
    ({ name: name, val: Number(p[k]), color: THEME[key] }));
  const m = Math.sqrt(rows.reduce((s, r) => s + r.val * r.val, 0));
  const lines = rows.map(r => `${r.name}  ${r.val.toFixed(3)} g`);
  lines.push(`合加速度 |a|  ${m.toFixed(3)} g`);
  const head = (p.server_time || '').slice(11) || ('#' + (p.id == null ? '' : p.id));

  g.font = '11px ui-monospace, Consolas, monospace';
  let bw = g.measureText(head).width;
  lines.forEach(t => { bw = Math.max(bw, g.measureText(t).width); });
  bw += 18;
  const bh = 16 * (lines.length + 1) + 12;
  let bx = xx + 12;
  if (bx + bw > w - pr) bx = xx - 12 - bw;
  const by = Math.min(Math.max(pt + 2, pt + 4), h - pb - bh);

  g.fillStyle = THEME.card;
  g.strokeStyle = THEME.border;
  g.lineWidth = 1;
  g.beginPath();
  if (g.roundRect) g.roundRect(bx, by, bw, bh, 6); else g.rect(bx, by, bw, bh);
  g.fill(); g.stroke();

  g.textAlign = 'left';
  g.fillStyle = THEME.muted;
  g.fillText(head, bx + 9, by + 14);
  lines.forEach((t, i) => {
    g.fillStyle = i < rows.length ? rows[i].color : THEME.text;
    g.fillText(t, bx + 9, by + 28 + i * 16);
  });
}

/* 时间窗切换 */
(function mountRangePicker() {
  const grp = $('chartRange');
  if (!grp) return;
  grp.addEventListener('click', (ev) => {
    const b = ev.target.closest('button[data-range]');
    if (!b || b.dataset.range === CUR_RANGE) return;
    CUR_RANGE = b.dataset.range;
    grp.querySelectorAll('button').forEach(x => x.classList.toggle('on', x === b));
    lastHistMs = 0;            // 立刻重拉一次，别等下一个周期
    tick();
  });
})();

const CHART_WRAP = document.querySelector('.chartwrap');
if (CHART_WRAP) {
  CHART_WRAP.addEventListener('mousemove', drawHover);
  CHART_WRAP.addEventListener('mouseleave', clearHover);
}

window.addEventListener('resize', () => { drawChart(true); });

/* 系统切换深色/浅色时，画布上的颜色要跟着换 —— canvas 不认 CSS 变量，
   必须重读一遍再重画。这就是用变量做主题唯一的额外成本。 */
if (window.matchMedia) {
  const mq = window.matchMedia('(prefers-color-scheme: dark)');
  const onTheme = () => { readTheme(); renderLegend(); drawChart(true); };
  if (mq.addEventListener) mq.addEventListener('change', onTheme);
  else if (mq.addListener) mq.addListener(onTheme);
}

function setPill(cls, text) {
  const p = $('pill');
  p.className = 'pill ' + cls;
  p.textContent = text;
}

function clearAll() {
  setPill('idle', '暂无数据');
  $('dev').textContent = 'device: —';
  ['ax', 'ay', 'az', 'am', 'tp', 'hp', 'rf'].forEach(k => $(k).innerHTML = '—');
  $('hpUsed').textContent = '已用 — MB / 共 — MB';
  $('rfQ').textContent = '—';
  $('hpCard').title = '';
  $('mtime').textContent = $('mage').textContent = $('mseq').textContent = $('mip').textContent = '—';
  $('tbody').innerHTML = '<tr><td colspan="10" class="empty">暂无数据</td></tr>';
  LAST = [];
  drawChart(true);
}

/* 带超时的 JSON 拉取：服务器卡住时不至于让页面一直挂着请求 */
async function getJSON(url) {
  const ctl = new AbortController();
  const to = setTimeout(() => ctl.abort(), 6000);
  try {
    const r = await fetch(url, { cache: 'no-store', signal: ctl.signal });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return await r.json();
  } finally { clearTimeout(to); }
}

/* ================= 第3周：实体按键事件 =================
 *
 * 数据来自 /api/buttons，服务端按 request_id 把一次按键产生的几条聚合成一条。
 * 这里**刻意不画四阶段时间线** —— 实体按键没有下行通道，压根不存在
 * 「服务器受理」和「指令下发」两步，画出来就是假的证据。只列真实发生的：
 * 谁按的、什么时候、新采了几条、跨度多少。
 */
/* 暂停态的界面同步：状态徽标 + 按钮文案。
 * 按钮文案跟着状态走，避免出现"当前是暂停态、按钮却还写着暂停"这种自相矛盾。 */
function updatePauseUI() {
  const chip = $('modeChip'), btn = $('btnPause');
  if (chip) {
    chip.textContent = PAUSED ? '已暂停周期上报' : '上报中';
    chip.className = 'tag' + (PAUSED ? ' paused' : '');
    chip.title = PAUSED
      ? '板子已停掉 1Hz 传感上报，改用 /api/poll 心跳保持命令通道'
      : '板子每秒上报一次传感数据';
  }
  if (btn && !btn.disabled) {
    btn.textContent = PAUSED ? '恢复周期上报' : '暂停周期上报';
  }
}

/* 教学求助面板。
 *
 * 与上一版「按键批次」面板的区别不只是换了个数据源 —— 语义变了：
 * 上一版回答"板子被按了几次、采了几条"；这一版回答任务卡那句
 * 「佩戴者按下按键，自己和查看信息的人分别应获得什么反馈」。
 * 所以每一行都要把**三级反馈分开摆**，并且第三级没有人点过就绝不显示"已收到"。
 */
/* 求助列表默认只展开最近几条。改版前把服务端给的 6 条全铺出来，
 * 这个面板一度占到整页高度的 26% —— 而其中大半是几天前的旧事件。
 * 旧事件不该删（它们是证据），但默认不必占满屏幕。 */
const HELP_FETCH = 12;      // 服务端一次给够，展开时不用再请求
const HELP_VISIBLE = 3;     // 默认显示几条
let HELP_SHOW_ALL = false;

function renderHelp(d) {
  const box = $('helpList');
  const evs = (d && d.events) || [];
  const counts = (d && d.counts) || {};
  if (!evs.length) {
    $('helpCount').textContent = '等待求助…';
    box.innerHTML = '<div class="helprow">还没有收到求助 —— 去按一下板子上的键。</div>';
    return;
  }
  const newest = evs[0];
  /* 默认把「已取消」排到后面。
   *
   * 为什么：设备不是一直在用，列表里常常整屏都是几天前被取消的测试事件。
   * 第一次打开的人会以为"这功能一直被取消"，而真正该看的「待回应」被挤到看不见。
   * 用稳定排序（现代引擎保证），组内仍按时间倒序，不改变事件的时间线。 */
  const cancelled = evs.filter(e => e.state === 'cancelled').length;
  const ordered = evs.slice().sort(
    (a, b) => (a.state === 'cancelled' ? 1 : 0) - (b.state === 'cancelled' ? 1 : 0));
  const shown = HELP_SHOW_ALL ? ordered : ordered.slice(0, HELP_VISIBLE);
  $('helpCount').textContent =
    (counts.open ? '待回应 ' + counts.open : '当前没有待回应的求助')
    + ' · 共 ' + evs.length + ' 条' + (cancelled ? '（已取消 ' + cancelled + '）' : '')
    + ' · 显示 ' + shown.length + ' 条'
    + ' · 最新 ' + humanAge(newest.age_seconds);

  /* 把"有待回应的求助"同步到顶部标签上的徽标。
   * 这一步是分区之后**必须**加的：面板藏起来了，信号不能跟着藏 ——
   * 否则别人按了键、页面开着，却没人知道要去看。 */
  const badge = $('badgeHelp');
  if (badge) {
    const open = counts.open || 0;
    badge.hidden = (open === 0);
    badge.textContent = open;
    badge.title = open ? (open + ' 条求助还没有人回应') : '';
  }

  box.innerHTML = shown.map(e => {
    const lv = e.levels || {};
    const fresh = (e.age_seconds !== null && e.age_seconds < 20);
    const st = e.state;
    const stText = st === 'open' ? '待回应'
                 : (st === 'acknowledged' ? '已回应' : '已取消');
    const stroke = e.snapshot || {};
    const snapTxt = (stroke.imu_ok === false)
      ? '（当时读不到 IMU —— 求助照发，不因传感器故障被吞掉）'
      : `|a| ≈ ${(stroke.norm_g ?? 0).toFixed(3)} g`;

    /* 三个徽标，三个独立事实。第三级只有真的有人点过才点亮。
     * offText 单独给：③ 不亮的时候写「—」，读起来像"不适用"或"没数据"，
     * 而它真正的意思是"还没有人回应" —— 这件事必须说出来，不能靠猜。 */
    const badge = (on, label, detail, offText) =>
      `<span class="lv ${on ? 'on' : 'off'}" title="${detail}">` +
      `<span class="t">${label}</span> ${on ? '✓' : (offText || '—')}</span>`;

    const canReply = (st === 'open');
    const canCancel = (st !== 'cancelled');
    return `<div class="helprow ${st}">` +
      `<div class="head">` +
        `<span class="key">${e.button || '?'}</span>` +
        `<span><b>${stText}</b></span>` +
        `<span>${e.created_at || ''}</span>` +
        `<span>${humanAge(e.age_seconds)}</span>` +
        (fresh ? `<span class="tag cmd">刚发生</span>` : '') +
        (e.simulated ? `<span class="sim" title="模拟/演练数据，不是真机按键">模拟</span>` : '') +
        (e.button && stroke.press_delay_ms !== undefined
          ? `<span>按下→快照 <b>${stroke.press_delay_ms}</b> ms</span>` : '') +
        `<span class="eid">${e.id}</span>` +
      `</div>` +
      `<div class="snap">当时环境：${snapTxt}</div>` +
      `<div class="levels">` +
        badge(lv.local && lv.local.done, '① 本地确认',
              (lv.local || {}).note + '｜证据：' + (lv.local || {}).evidence) +
        badge(lv.server && lv.server.done, '② VPS 接收',
              (lv.server || {}).note + '｜证据：' + (lv.server || {}).evidence) +
        badge(lv.viewer && lv.viewer.done, '③ 查看者回应',
              (lv.viewer || {}).note + '｜证据：' + (lv.viewer || {}).evidence,
              '未回应') +
      `</div>` +
      (e.state === 'cancelled'
        ? `<div class="snap">已取消${e.cancel_reason ? '：' + e.cancel_reason : ''}` +
          `${e.cancel_at ? '（' + e.cancel_at + '）' : ''}</div>`
        : '') +
      (e.levels && e.levels.viewer && e.levels.viewer.done
        ? `<div class="snap">${e.levels.viewer.by || '有人'} 已回应` +
          `${e.ack_at ? '（' + e.ack_at + '）' : ''}</div>`
        : '') +
      (canReply || canCancel
        ? `<div class="acts">` +
          (canReply ? `<button class="primary" data-act="ack" data-id="${e.id}">我来处理（回应）</button>` : '') +
          (canCancel ? `<button data-act="cancel" data-id="${e.id}">取消求助</button>` : '') +
          `</div>`
        : '') +
      `</div>`;
  }).join('') +
  /* 有更早的就给一个展开入口。它同样是每轮重绘出来的，
     所以下面也走事件委托，而不是在这里直接绑。 */
  (evs.length > HELP_VISIBLE
    ? (HELP_SHOW_ALL
        ? `<button class="morebtn" data-act="collapse">收起，只留最近 ${HELP_VISIBLE} 条</button>`
        : `<button class="morebtn" data-act="more">显示更早的 ${evs.length - HELP_VISIBLE} 条</button>`)
    : '');

  /* 按钮是每轮重绘出来的，所以用事件委托而不是逐个绑定 ——
   * 逐个绑会在重绘后失效，表现为"点了没反应"，很难查。 */
  box.querySelectorAll('button[data-act]').forEach(b => {
    const act = b.dataset.act;
    if (act === 'more' || act === 'collapse') {
      b.addEventListener('click', () => {
        HELP_SHOW_ALL = (act === 'more');
        tick();                     // 立刻重绘，不用等下一个 1 秒周期
      });
      return;
    }
    b.addEventListener('click', () => replyHelp(b.dataset.id, act, b));
  });
}

async function replyHelp(eid, action, btn) {
  /* 回应人取自面板上的输入框；没填就明确写成"匿名同学"，
   * 而不是编一个名字 —— 第三级反馈的证据必须是真的那个人。 */
  const typed = ($('whoami') && $('whoami').value || '').trim();
  const body = { action };
  if (action === 'ack')   body.by = typed || '匿名同学';
  if (action === 'cancel') body.reason = typed ? ('由 ' + typed + ' 取消') : '未填写原因';
  btn.disabled = true;
  try {
    const r = await fetch(API_BASE + '/api/help/' + encodeURIComponent(eid) + '/reply', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const j = await r.json();
    if (!r.ok) {
      alert('操作失败：' + (j.hint || j.error || ('HTTP ' + r.status)));
    } else if (j.reply_command_id) {
      /* 把"回传设备"这件事明确说出来 —— 否则使用者不知道板子那边还会再亮一次灯 */
      $('cmdHint').innerHTML = `<code>${eid}</code> 已${action === 'ack' ? '回应' : '取消'}，` +
        `并已生成回传指令 <code>${j.reply_command_id}</code>（搭车下发，` +
        `板子取走后会再给一次本地提示）。`;
    }
  } catch (e) {
    alert('操作失败：' + e.message);
  } finally {
    btn.disabled = false;
    tick();
  }
}

async function tick() {
  if (document.hidden) return;            // 后台标签页不轮询
  try {
    /* 先取最新一条：它的 device_id 决定后面两个查询只看哪台设备。
     * 少了这一步，自测脚本（device_id=selftest-sim）模拟出来的求助事件
     * 会混进真板子的「教学求助」面板 —— 那等于让模拟数据冒充真机证据，
     * 正是本项目一直在防的那种"看起来像证据"的东西。
     * （模拟数据不是不能有：它必须带 simulated 标记，在面板上标出来。） */
    const lr = await getJSON(API_BASE + '/api/latest');
    const dev = (lr.record && lr.record.device_id) || '';
    const q = dev ? '&device_id=' + encodeURIComponent(dev) : '';
    const R = RANGES[CUR_RANGE];

    /* 求助列表每次都要拉（它变化不规律，可能是几秒前刚发生的）。
     * 历史曲线则**只在需要时**重拉：时间窗越长，间隔越大（见 RANGES）。
     * 两次重拉之间用 /api/latest 那条往队首补一条，曲线就不会停 ——
     * 否则切到"1 小时"档之后每 15 秒才动一下，看起来像卡死了。 */
    const br = await getJSON(API_BASE + '/api/help?limit=' + HELP_FETCH + q);
    if (!LAST.length || Date.now() - lastHistMs >= R.refetchMs) {
      const hr = await getJSON(API_BASE + '/api/history?minutes=' + R.minutes
                               + '&limit=' + R.limit + q);
      LAST = hr.records || [];
      lastHistMs = Date.now();
    } else if (lr.record && lr.record.id !== LAST[0].id) {
      /* 补一条到队首，同时把滑出时间窗的尾部丢掉 ——
       * 否则切到"1 分钟"档之后，尾部那些几分钟前的点会一直赖在图上。 */
      LAST.unshift(lr.record);
      const cutoff = Date.now() - R.minutes * 60000;
      while (LAST.length && LAST[LAST.length - 1].server_ms < cutoff) LAST.pop();
    }

    $('mthr').textContent = (lr.stale_seconds ?? '—') + ' 秒';

    if (!lr.has_data) { clearAll(); return; }

    const r = lr.record;
    $('dev').textContent = 'device: ' + r.device_id;
    $('ax').innerHTML = fmt(r.ax) + '<span class="u">g</span>';
    $('ay').innerHTML = fmt(r.ay) + '<span class="u">g</span>';
    $('az').innerHTML = fmt(r.az) + '<span class="u">g</span>';
    const m = mag(Number(r.ax), Number(r.ay), Number(r.az));
    $('am').innerHTML = fmt(m) + '<span class="u">g</span>';

    /* 设备自身状态。板子没上报时字段是 null —— 显示"—"而不是 0，
     * 免得把"没测到"伪造成"温度 0 度 / 内存 0 KB"这种假读数。 */
    const tp = (r.temp_c === null || r.temp_c === undefined) ? null : Number(r.temp_c);
    $('tp').innerHTML = tp === null ? '—' : fmt(tp, 1) + '<span class="u">°C</span>';

    /* 内存：剩余 = 板子上报值；已用 = 总量 - 剩余（同口径，都是 MALLOC_CAP_DEFAULT）。
     * 老固件没上报 total_heap 时，已用/总量都显示"—"，不硬算成 0。 */
    const heap     = num(r.free_heap);
    const heapMin  = num(r.min_free_heap);
    const heapTot  = num(r.total_heap);
    const heapUsed = (heap === null || heapTot === null) ? null : Math.max(0, heapTot - heap);
    /* 历史最低水位挂在 title 上：它持续下降 = 可能有内存泄漏 */
    $('hpCard').title = heapMin === null ? '' : '开机以来最低剩余：' + mb(heapMin) + ' MB';
    $('hp').innerHTML = heap === null ? '—' : mb(heap) + '<span class="u">MB</span>';
    $('hpUsed').textContent = '已用 ' + mb(heapUsed) + ' MB / 共 ' + mb(heapTot) + ' MB';

    /* WiFi 信号强度（dBm 是负数，越接近 0 越好） */
    const rssi = num(r.rssi);
    if (rssi === null) {
      $('rf').innerHTML = '—';
      $('rfQ').textContent = '—';
    } else {
      const q = rssi >= -60 ? '很好' : rssi >= -70 ? '尚可'
              : rssi >= -80 ? '偏弱，可能丢包' : '很差，必然断流';
      $('rf').innerHTML = rssi + '<span class="u">dBm</span>';
      $('rfQ').textContent = q;
    }

    $('mtime').textContent = r.server_time;
    $('mage').textContent = r.age_seconds.toFixed(1) + ' 秒';
    $('mseq').textContent = r.seq;
    $('mip').textContent = r.src_ip || '—';

    /* 三种状态必须分开，不能混成一句"未更新"：
     *   · 暂停中     —— 设备活得好好的，是**我们让它别上报**的；数据年龄变大是预期
     *   · 正在恢复   —— 刚发了 resume，心跳还在、数据还没来，给个过渡提示
     *   · 真未更新   —— 设备确实掉线了
     * 把第一种误判成第三种，就是冤假错案（而且会掩盖"暂停生效了"这个事实）。 */
    PAUSED = !!lr.paused;
    updatePauseUI();
    const seen = lr.device_seen_seconds;
    const seenTxt = (seen === null || seen === undefined) ? '—' : humanAge(seen);
    if (PAUSED) {
      setPill('warn', '已暂停周期上报 · 心跳 ' + seenTxt);
    } else if (r.stale && lr.last_request_kind === 'poll'
               && seen !== null && seen < 5) {
      setPill('warn', '正在恢复上报…（心跳仍在）');
    } else {
      setPill(r.stale ? 'bad' : 'ok',
              r.stale ? '未更新 · 已 ' + r.age_seconds.toFixed(0) + ' 秒' : '实时更新中');
    }

    drawChart(false);
    renderHelp(br);

    const rows = LAST.slice(0, 8).map(x => {
      const mm = mag(Number(x.ax), Number(x.ay), Number(x.az));
      const t = (x.temp_c === null || x.temp_c === undefined) ? '—' : fmt(x.temp_c, 1);
      const xf = num(x.free_heap), xt = num(x.total_heap);
      const xu = (xf === null || xt === null) ? '—' : mb(Math.max(0, xt - xf));
      /* 第2/3周：把三种来源在表里区分开。依据是数据自带的 trigger 字段，
       * 不是界面上的猜测 —— 换台机器跑同样的数据也是这个结论。
       *   定时 = 板子自己按时上报（默认节奏）
       *   指令 = 网页点了「重新采集」，服务器下发的
       *   按键 = 现场有人按了板子上的实体键 */
      const isCmd = (x.trigger === 'command');
      const isBtn = (x.trigger === 'button');
      const tag = isCmd
        ? `<span class="tag cmd" title="${x.request_id || ''} · ${x.cmd_state || ''}">指令</span>`
        : isBtn
          ? `<span class="tag btn" title="${x.request_id || ''} · 按的是 ${x.button || ''} 键">按键 ${x.button || ''}</span>`
          : `<span class="tag">定时</span>`;
      return `<tr class="${isCmd ? 'is-cmd' : (isBtn ? 'is-btn' : '')}"><td>${x.server_time}</td>` +
             `<td>${tag}</td>` +
             `<td>${fmt(x.ax)}</td><td>${fmt(x.ay)}</td>` +
             `<td>${fmt(x.az)}</td><td>${fmt(mm)}</td><td>${t}</td>` +
             `<td>${mb(xf)}</td><td>${xu}</td>` +
             `<td>${x.seq}</td></tr>`;
    }).join('');
    $('tbody').innerHTML = rows || '<tr><td colspan="10" class="empty">暂无数据</td></tr>';
  } catch (e) {
    setPill('warn', '无法连接服务');
  }
}

/* ================= 第2周：主动交互（下行指令） =================
 *
 * 与「刷新页面」的本质区别：刷新只是 GET 已有的历史记录，数据条数不会变；
 * 这里会 POST 一条指令，服务器**受理**它，等板子真的执行完再回报。
 * 下面把四个必经阶段和各自耗时摊开显示，任何一步卡住都能看出来。
 */
const CMD_STEPS = [
  { key: 'issued',    name: '① 服务器受理', at: 'created_at',   leg: null },
  { key: 'delivered', name: '② 指令已下发', at: 'delivered_at', leg: 'issue_to_deliver_ms' },
  { key: 'received',  name: '③ 设备已接收', at: 'received_at',  leg: 'deliver_to_receive_ms' },
  /* 第4步刻意叫「执行结果」而不是「执行完成」：这一步有三种结局
   * （完成 / 设备回报失败 / 超时），名字里预设成"完成"的话，
   * 失败时会出现「执行完成 · 执行失败（设备回报）」这种自相矛盾的读法。 */
  { key: 'done',      name: '④ 执行结果',   at: 'done_at',      leg: 'receive_to_done_ms' },
];
const TERMINAL = ['done', 'timeout', 'failed'];

let CMD = { id: null, timer: null, totalBefore: null, last: null };

/* 指令在途时在「交互」标签上点一个小圆点。
 * 和求助徽标同一个理由：分区把面板藏起来之后，"还在跑"这件事得让人看得见，
 * 否则切走了就完全不知道那条指令是成了还是卡住了。 */
function setCmdDot(on) {
  const d = $('dotCmd');
  if (d) d.hidden = !on;
}

function renderTimeline(cmd) {
  if (!cmd) {
    $('cmdTimeline').innerHTML = CMD_STEPS.map(s =>
      `<div class="step"><div class="n">${s.name}</div><div class="t">等待</div></div>`
    ).join('');
    return;
  }
  const st = cmd.status;
  const t = cmd.timeout_stage;
  const legs = cmd.legs_ms || {};
  const terminal = TERMINAL.includes(st);
  /* 第一个还没到达的阶段 —— 它就是"当前停在哪一步"，无论是进行中还是超时。
   * 注意不能因为状态是终态就不标它，否则超时指令四步全灰，看不出卡在哪。 */
  const firstUnreached = CMD_STEPS.findIndex(s => !cmd[s.at]);

  $('cmdTimeline').innerHTML = CMD_STEPS.map((s, i) => {
    const reached = !!cmd[s.at];
    const isHere = !reached && i === firstUnreached;
    let cls, note;
    if (reached) {
      cls = 'done';
      note = (cmd[s.at] || '').slice(11);
    } else if (isHere) {
      cls = terminal ? 'bad' : 'cur';
      note = st === 'timeout'
        ? '超时（' + (t === 'accept' ? '设备未取走' : '未执行完') + '）'
        : (st === 'failed' ? '执行失败（设备回报）' : '进行中…');
    } else {
      cls = '';
      note = '等待';
    }
    const leg = (s.leg && legs[s.leg])
      ? `<div class="d">+${legs[s.leg]} ms</div>` : '<div class="d">&nbsp;</div>';
    return `<div class="step ${cls}"><div class="n">${s.name}</div>` +
           `<div class="t">${note}</div>${leg}</div>`;
  }).join('');
}

function renderCompare(cmd) {
  const lines = [];
  if (!cmd) { $('cmdCmp').innerHTML = '还没有下发过指令。'; return; }
  lines.push(`<div>指令 <b>${cmd.id}</b> · 状态 <b>${cmd.status}</b></div>`);
  if (cmd.sample_count !== null && cmd.sample_count !== undefined) {
    lines.push(`<div>本次新采集入库 <b>${cmd.sample_count}</b> 条` +
               `（id ${cmd.first_reading_id ?? '—'} ~ ${cmd.last_reading_id ?? '—'}）</div>`);
  }
  /* 设备给的原因/备注。失败时它是"为什么失败"的唯一线索；
   * 部分送达时它说明"完成得不干净"。有就一定要显示出来 ——
   * 只看到一个红色的"失败"却不知道为什么，等于白跑一趟。 */
  if (cmd.note) {
    const bad = (cmd.status === 'failed');
    lines.push(`<div class="${bad ? 't-bad' : 't-warn'}">` +
               `设备备注：<b>${cmd.note}</b></div>`);
  }
  const legs = cmd.legs_ms || {};
  if (legs.total_ms !== undefined) {
    lines.push(`<div>端到端耗时 <b>${legs.total_ms} ms</b></div>`);
  }
  if (CMD.totalBefore !== null) {
    lines.push(`<div>下发前库里 <b>${CMD.totalBefore}</b> 条（刷新不会让它变多）</div>`);
  }
  $('cmdCmp').innerHTML = lines.join('');
}

async function fetchTotal() {
  try {
    const d = await getJSON(API_BASE + '/api/devices');
    return (typeof d.total === 'number') ? d.total
         : (d.devices || []).reduce((s, x) => s + (x.count || 0), 0);
  } catch (e) { return null; }
}

async function pollCommand() {
  if (!CMD.id) return;
  try {
    const cmd = await getJSON(API_BASE + '/api/command/' + encodeURIComponent(CMD.id));
    CMD.last = cmd;
    renderTimeline(cmd);
    renderCompare(cmd);
    if (TERMINAL.includes(cmd.status)) {
      clearInterval(CMD.timer);
      CMD.timer = null;
      setCmdDot(false);
      ['btnCmd', 'btnPause'].forEach(id => { const b = $(id); if (b) b.disabled = false; });
      const after = await fetchTotal();
      if (after !== null && CMD.totalBefore !== null) {
        const extra = `<div>下发后库里 <b>${after}</b> 条` +
          `<b class="${after > CMD.totalBefore ? 't-ok' : 't-warn'}">` +
          `（${after > CMD.totalBefore ? '+' : ''}${after - CMD.totalBefore}）</b></div>`;
        $('cmdCmp').insertAdjacentHTML('beforeend', extra);
      }
      if (cmd.status === 'done') {
        if (cmd.type === 'pause') {
          $('cmdHint').innerHTML = '周期上报已暂停。板子改用 <code>/api/poll</code> 心跳保持' +
            '命令通道 —— 现在再点「重新采集」，新出现的记录<b>只可能来自指令</b>，' +
            '这就是最硬的证据。';
        } else if (cmd.type === 'resume') {
          $('cmdHint').innerHTML = '周期上报已恢复，板子重新每秒上报。';
        } else {
          $('cmdHint').innerHTML =
            `已让设备完成一次新采集：${cmd.sample_count} 条在 ${cmd.legs_ms?.total_ms} ms 内入库。` +
            ' 表格里带 <span class="tag cmd">指令</span> 标记的行就是这次新采的。';
        }
      } else if (cmd.status === 'failed') {
        /* 失败与超时必须分开说，因为它们要查的地方完全不同：
         *   失败 —— 设备明确回报"我试了，没干成"（原因在 cmd.note 里）
         *   超时 —— 服务器什么回音都没听到，只能判定"不知道"
         * 混成一句"没成功"，排查时就会在错误的方向上浪费时间。 */
        $('cmdHint').innerHTML =
          `<b class="t-bad">设备回报执行失败</b>：` +
          `${cmd.note || '（设备没有给出原因）'}` +
          ' —— 这是<b>设备亲口说的</b>，说明指令确实送到了，问题出在执行这一端。';
      } else {
        $('cmdHint').innerHTML =
          `指令 ${cmd.status}（${cmd.timeout_stage || ''}）—— 板子当前不在线或未响应。` +
          ' 注意：<b>超时只说明这一条没走完，不能据此断定硬件坏了</b>。';
      }
      tick();                       // 立刻刷新一次表格，不用等下一个周期
    }
  } catch (e) {
    // 服务器偶发卡顿，下个周期再试，不打断
  }
}

/* 统一的指令下发。采集指令与控制指令（暂停/恢复）走**完全相同**的一条通道、
 * 同一套四阶段时间线 —— 这不是偷懒，本身就是要展示的事实：
 * 暂停之后命令通道还在，靠的就是这条搭车下行通道没被一起停掉。
 * 如果给暂停单独做一套界面，反而看不清"通道保持"这件事。 */
async function issueCommand(type, extra = {}) {
  const dev = ($('dev').textContent || '').replace('device: ', '').trim();
  if (!dev || dev === '—') {
    $('cmdHint').textContent = '还没识别到设备，等板子上报一次再试。';
    return false;
  }
  const btns = ['btnCmd', 'btnPause'].map(id => $(id)).filter(Boolean);
  btns.forEach(b => { b.disabled = true; });
  $('cmdHint').textContent = '正在向服务器受理指令…';

  CMD.totalBefore = await fetchTotal();   // 记下"下发前"的条数，用于对比
  try {
    const r = await fetch(API_BASE + '/api/command', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(Object.assign({ device_id: dev, type }, extra)),
    });
    if (!r.ok) {
      let msg = 'HTTP ' + r.status;
      let payload = null;
      try {
        payload = await r.json();
        msg = payload.error || msg;
        if (payload.allowed) msg += '（允许的类型：' + payload.allowed.join(' / ') + '）';
      } catch (_) { /* 服务器没返回 JSON，就用状态码 */ }

      /* 409 = 上一次同类指令还没走完。这不是错误，而是"你点重复了"，
       * 所以别丢一句红字就完事 —— 直接把界面接到那条在途指令上继续跟踪。
       * 用户在意的不是"被拒绝了"，而是"我这一下到底有没有生效"。 */
      if (r.status === 409 && payload && payload.existing_request_id) {
        CMD.id = payload.existing_request_id;
        $('cmdHint').innerHTML =
          `上一次「${type === 'recollect' ? '重新采集' : '周期上报'}」还没走完，` +
          `已改为继续跟踪 <code>${CMD.id}</code>（当前 ${payload.existing_status}）——` +
          `重复点击不会多采一次，也不会把上一次挤掉。`;
        renderTimeline(null);
        await pollCommand();
        if (CMD.timer) clearInterval(CMD.timer);
        CMD.timer = setInterval(pollCommand, 500);
        setCmdDot(true);
        return true;
      }
      throw new Error(msg);
    }
    const j = await r.json();
    CMD.id = j.request_id;
    const what = type === 'recollect'
      ? `采 ${j.params.samples} 条、间隔 ${j.params.interval_ms} ms`
      : (type === 'pause' ? '暂停周期上报' : '恢复周期上报');
    $('cmdHint').innerHTML = `已受理指令 <code>${CMD.id}</code>（${what}）：` +
      '等板子下一次上报/心跳来取走。';
    renderTimeline(null);
    await pollCommand();
    if (CMD.timer) clearInterval(CMD.timer);
    CMD.timer = setInterval(pollCommand, 500);   // 500ms 一次，看清每步推进
    setCmdDot(true);
    return true;
  } catch (e) {
    btns.forEach(b => { b.disabled = false; });
    $('cmdHint').textContent = '下发失败：' + e.message;
    return false;
  }
}

$('btnCmd').addEventListener('click', () => {
  const [samples, interval] = $('cmdPlan').value.split(',').map(Number);
  issueCommand('recollect', { samples: samples, interval_ms: interval });
});

/* 暂停/恢复：按当前状态决定发哪个动作，避免连点两次都发 pause */
$('btnPause').addEventListener('click', () => {
  issueCommand(PAUSED ? 'resume' : 'pause');
});

renderTimeline(null);

/* ---------- 下载历史数据 ----------
 * 直接指向服务端导出接口，让浏览器按 Content-Disposition 自己下载，
 * 不用 fetch 回来再拼 Blob —— 几万条也不会先把内存撑起来。 */
$('btnCsv').addEventListener('click', () => {
  const v = $('csvRange').value;
  const q = (v === 'all') ? '' : '?' + v;   // 'all' = 不传参数 = 服务端默认上限
  window.location.href = API_BASE + '/api/export.csv' + q;
});

/* 顺带显示库里累计多少条，让"下载的是什么"一目了然 */
(async function loadCount() {
  try {
    const d = await getJSON(API_BASE + '/api/devices');
    const n = (typeof d.total === 'number') ? d.total
            : (d.devices || []).reduce((s, x) => s + (x.count || 0), 0);
    $('csvInfo').textContent = '累计 ' + n.toLocaleString('zh-CN') + ' 条'
      + (d.max_rows ? '（上限 ' + d.max_rows.toLocaleString('zh-CN') + '）' : '');
  } catch (e) { /* 拿不到就不显示，不影响下载 */ }
})();

/* ================= 分区标签页 =================
 *
 * 显隐规则在 CSS 里（body[data-tab=...]），JS 只表达"现在是哪个分区"这一个意图。
 * 逐个元素 toggle 的话，分区一多就容易出现"两个同时可见"或"都不见"，
 * 而且那种 bug 只在特定点击顺序下出现，很难复现。
 *
 * 用 URL hash 而不是纯内存状态，多两个好处：
 *   · 刷新 / 分享链接能直接落在同一个分区（验收时可以直接给 #help）；
 *   · 浏览器前进后退键可用。
 */
const TABS = ['overview', 'command', 'help', 'camera', 'data', 'all'];

function applyTab(name) {
  if (TABS.indexOf(name) < 0) name = 'overview';
  document.body.dataset.tab = name;
  document.querySelectorAll('.tabs button[data-tab]').forEach(b => {
    const on = b.dataset.tab === name;
    b.classList.toggle('on', on);
    b.setAttribute('aria-selected', on ? 'true' : 'false');
  });
  /* 曲线画布在隐藏分区里宽度是 0（drawChart 会直接跳过不画），
     所以切到可见分区时必须强制重画一次，否则看到的是一张空白或旧尺寸的图。 */
  if (name === 'overview' || name === 'all') drawChart(true);
  clearHover();
}

function goTab(name) {
  if (name === 'overview') {
    /* 默认分区不进 hash，地址栏干净一点 */
    history.pushState(null, '', location.pathname);
    applyTab('overview');
  } else {
    location.hash = name;          // 触发 hashchange，同时留下历史记录
  }
}

document.querySelectorAll('.tabs button[data-tab]').forEach(b => {
  b.addEventListener('click', () => goTab(b.dataset.tab));
});

/* 左右方向键切换 —— 标签页该有的键盘行为。缺了它，
   键盘用户在这套界面上会被困在"只能点鼠标"的状态。 */
(function mountTabKeys() {
  const bar = document.querySelector('.tabs');
  if (!bar) return;
  bar.addEventListener('keydown', (e) => {
    const step = e.key === 'ArrowRight' ? 1 : (e.key === 'ArrowLeft' ? -1 : 0);
    if (!step) return;
    e.preventDefault();
    const i = TABS.indexOf(document.body.dataset.tab || 'overview');
    goTab(TABS[(i + step + TABS.length) % TABS.length]);
  });
})();

window.addEventListener('hashchange', () => applyTab(location.hash.slice(1) || 'overview'));

renderLegend();
applyTab(location.hash.slice(1) || 'overview');
tick();
setInterval(tick, 1000);
/* 切回前台立即补一次，不用等下一个 1 秒周期 */
document.addEventListener('visibilitychange', () => { if (!document.hidden) tick(); });
