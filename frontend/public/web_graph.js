/* web_graph.js — 의존성 없는 SVG 그래프 렌더러 (Vue 컴포넌트로 등록해서 쓴다).
 *
 * 입력은 서버가 정적 분석 결과로 만든 노드·간선뿐이고, 여기서는 배치와 그리기만 한다.
 *   node: { id, label, sub?, kind?: 'class'|'module'|'symbol'|'external'|'unresolved', badge? }
 *   클릭 → 'select', 더블클릭 → 'open'(안으로 들어가기)
 *   edge: { source, target, label?, dashed? }
 * 레이아웃: 순환 간선(DFS 뒤쪽 간선)을 제외하고 최장 경로로 열(layer)을 정한 뒤, 열마다 무게중심 순으로 정렬한다.
 */
(function (root) {
  const NODE_W = 178, NODE_H = 50, GAP_X = 74, GAP_Y = 20, PAD = 24;

  function layout(nodes, edges) {
    const ids = nodes.map(n => n.id);
    const known = new Set(ids);
    const out = {}, inc = {};
    ids.forEach(id => { out[id] = []; inc[id] = []; });
    edges.forEach(e => {
      if (known.has(e.source) && known.has(e.target) && e.source !== e.target) {
        out[e.source].push(e.target); inc[e.target].push(e.source);
      }
    });
    // 뒤쪽 간선(순환) 찾기
    const color = {}, back = new Set();
    const visit = u => {
      color[u] = 1;
      out[u].forEach(v => { if (color[v] === 1) back.add(u + '>' + v); else if (!color[v]) visit(v); });
      color[u] = 2;
    };
    ids.filter(id => inc[id].length === 0).forEach(r => { if (!color[r]) visit(r); });
    ids.forEach(id => { if (!color[id]) visit(id); });
    // 최장 경로 열
    const L = {};
    const depth = u => {
      if (L[u] !== undefined) return L[u];
      L[u] = 0;
      inc[u].forEach(p => { if (!back.has(p + '>' + u)) L[u] = Math.max(L[u], depth(p) + 1); });
      return L[u];
    };
    ids.forEach(depth);
    const maxL = Math.max(0, ...ids.map(id => L[id]));
    const cols = Array.from({ length: maxL + 1 }, () => []);
    ids.forEach(id => cols[L[id]].push(id));
    // 무게중심 정렬 (앞 열의 위치 기준)
    const order = {};
    cols[0].sort().forEach((id, i) => { order[id] = i; });
    for (let c = 1; c <= maxL; c++) {
      const bary = id => {
        const ps = inc[id].filter(p => L[p] < c);
        return ps.length ? ps.reduce((s, p) => s + order[p], 0) / ps.length : 0;
      };
      cols[c].sort((a, b) => bary(a) - bary(b) || (a < b ? -1 : 1));
      cols[c].forEach((id, i) => { order[id] = i; });
    }
    const tallest = Math.max(...cols.map(c => c.length));
    const H = PAD * 2 + tallest * NODE_H + (tallest - 1) * GAP_Y;
    const pos = {};
    cols.forEach((col, c) => {
      const colH = col.length * NODE_H + (col.length - 1) * GAP_Y;
      const y0 = (H - colH) / 2;
      col.forEach((id, i) => { pos[id] = { x: PAD + c * (NODE_W + GAP_X), y: y0 + i * (NODE_H + GAP_Y) }; });
    });
    const W = PAD * 2 + (maxL + 1) * NODE_W + maxL * GAP_X;
    return { pos, W: Math.max(W, 300), H: Math.max(H, 120), back };
  }

  function edgePath(a, b, isBack) {
    const sx = a.x + NODE_W, sy = a.y + NODE_H / 2, tx = b.x, ty = b.y + NODE_H / 2;
    if (isBack || tx <= sx) {          // 되돌아가는 간선은 위로 크게 돌린다
      const top = Math.min(a.y, b.y) - 26;
      return `M${sx},${sy} C${sx + 50},${top} ${tx - 50},${top} ${tx},${ty}`;
    }
    const dx = (tx - sx) * 0.55;
    return `M${sx},${sy} C${sx + dx},${sy} ${tx - dx},${ty} ${tx},${ty}`;
  }

  const trunc = (s, n) => (s && s.length > n ? s.slice(0, n - 1) + '…' : s || '');

  const GraphView = {
    props: { nodes: Array, edges: Array, highlight: { type: Array, default: () => [] },
             selected: { type: String, default: '' }, entry: { type: String, default: '' } },
    emits: ['select', 'open'],
    computed: {
      lay() { return layout(this.nodes, this.edges); },
      drawn() {
        const { pos, back } = this.lay;
        return this.edges.filter(e => pos[e.source] && pos[e.target] && e.source !== e.target).map((e, i) => {
          const a = pos[e.source], b = pos[e.target];
          const isBack = back.has(e.source + '>' + e.target);
          const mid = { x: (a.x + NODE_W + b.x) / 2, y: (a.y + b.y) / 2 + NODE_H / 2 - (isBack ? 18 : 6) };
          return { key: i, d: edgePath(a, b, isBack), label: e.label, dashed: e.dashed, mid };
        });
      },
    },
    data() { return { scale: 0.85, x: 18, y: 18, panning: false, moved: false, skipClick: false }; },
    methods: { trunc, cls(n) {
      return ['node', n.kind || 'symbol', { hl: this.highlight.includes(n.id), sel: this.selected === n.id,
                                              entry: this.entry === n.id }];
    },
      wheel(e) {
        const before = this.scale, next = Math.max(0.35, Math.min(2.5, before * (e.deltaY < 0 ? 1.12 : 0.89)));
        const box = this.$refs.canvas.getBoundingClientRect();
        const px = e.clientX - box.left, py = e.clientY - box.top;
        this.x = px - (px - this.x) * (next / before); this.y = py - (py - this.y) * (next / before); this.scale = next;
      },
      down(e) {
        if (e.button !== 0) return;
        const x0 = e.clientX, y0 = e.clientY, ox = this.x, oy = this.y; this.panning = true; this.moved = false;
        const move = ev => { const dx = ev.clientX - x0, dy = ev.clientY - y0; if (Math.abs(dx) + Math.abs(dy) > 3) this.moved = true; this.x = ox + dx; this.y = oy + dy; };
        const up = () => { this.panning = false; if (this.moved) { this.skipClick = true; setTimeout(() => { this.skipClick = false; }, 0); } window.removeEventListener('mousemove', move); window.removeEventListener('mouseup', up); };
        window.addEventListener('mousemove', move); window.addEventListener('mouseup', up);
      },
      nodeClick(id) { if (!this.skipClick) this.$emit('select', id); },
      reset() { this.scale = 0.85; this.x = 18; this.y = 18; }, zoom(delta) { this.scale = Math.max(0.35, Math.min(2.5, this.scale + delta)); },
    },
    template: `
      <div ref="canvas" class="graph-scroll" :class="{ panning }" @wheel.prevent="wheel" @mousedown="down">
        <div class="map-tools" @mousedown.stop><button class="small ghost" @click="zoom(-0.15)">−</button><button class="small ghost" @click="reset">{{ Math.round(scale * 100) }}%</button><button class="small ghost" @click="zoom(0.15)">+</button></div>
        <svg :width="lay.W" :height="lay.H" :viewBox="'0 0 ' + lay.W + ' ' + lay.H" :style="{ position:'absolute', left:'0', top:'0', transformOrigin:'0 0', transform:'translate(' + x + 'px,' + y + 'px) scale(' + scale + ')' }">
          <defs>
            <marker id="arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
              <path d="M0,0 L10,5 L0,10 z" style="fill:var(--edge, #8a93a6)"/></marker>
            <marker id="arr-d" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
              <path d="M0,0 L10,5 L0,10 z" style="fill:var(--warn, #a15c00)"/></marker>
          </defs>
          <g v-for="e in drawn" :key="e.key">
            <path :d="e.d" :class="['edge', { dashed: e.dashed }]" fill="none"
                  :marker-end="e.dashed ? 'url(#arr-d)' : 'url(#arr)'"/>
            <text v-if="e.label" :x="e.mid.x" :y="e.mid.y" class="elabel" text-anchor="middle">{{ e.label }}</text>
          </g>
          <g v-for="n in nodes" :key="n.id" :class="cls(n)" style="cursor:pointer"
             :transform="'translate(' + lay.pos[n.id].x + ',' + lay.pos[n.id].y + ')'" @click.stop="nodeClick(n.id)" @dblclick.stop="$emit('open', n.id)">
            <rect width="${NODE_W}" height="${NODE_H}" rx="9"/>
            <text x="12" y="21" class="nlabel">{{ trunc(n.label, 22) }}</text>
            <text x="12" y="38" class="nsub">{{ trunc(n.sub, 26) }}</text>
            <g v-if="n.badge"><rect :x="${NODE_W} - 34" y="-8" width="34" height="17" rx="8" class="badge-bg"/>
              <text :x="${NODE_W} - 17" y="4" text-anchor="middle" class="badge-t">{{ n.badge }}</text></g>
          </g>
        </svg>
      </div>`,
  };
  GraphView.template = GraphView.template.replace(/\$\{NODE_W\}/g, NODE_W).replace(/\$\{NODE_H\}/g, NODE_H);
  root.RecatGraph = { GraphView, layout };
})(window);
