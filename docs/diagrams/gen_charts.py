"""Generate 5 blog chart HTML files in docs/diagrams/"""
import os, math

DIR = os.path.dirname(os.path.abspath(__file__))

# ─── helpers ───────────────────────────────────────────────────────────
def html_wrap(title, body_content, extra_head=""):
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>{title}</title>
{extra_head}
</head>
<body style="margin:0;padding:40px;width:1200px;height:675px;overflow:hidden;background:#ffffff;font-family:Arial,sans-serif;box-sizing:border-box">
{body_content}
</body>
</html>"""

# ─── Chart 1: spread-discovery ────────────────────────────────────────
def gen_spread_discovery():
    data = [2.1,2.3,2.0,1.8,2.2,2.5,2.8,3.2,3.8,4.5,5.2,5.8,6.1,5.5,4.8,4.2,3.6,3.0,2.5,2.2,1.9,2.1,2.4,2.7,3.1,3.5,4.0,4.6,5.0,5.3,4.9,4.3,3.7,3.2,2.8,2.4,2.1,1.8,2.0,2.3,2.6,2.9,3.3,3.8,4.2,4.8,5.5,6.0,5.4,4.7,4.0,3.5,3.0,2.6,2.3,2.0,1.7,2.0,2.3,2.5]
    n = len(data)
    mean = sum(data)/n  # ~3.307
    std = (sum((x-mean)**2 for x in data)/n)**0.5  # ~1.30

    L, R, T, B = 100, 1140, 90, 570
    W, H = R-L, B-T
    y_min, y_max = 1.0, 7.0
    x_step = W/(n-1)
    sx = lambda i: L + i*x_step
    sy = lambda v: B - (v-y_min)/(y_max-y_min)*H

    pts = " ".join(f"{sx(i):.1f},{sy(v):.1f}" for i,v in enumerate(data))

    # build svg elements
    svg_parts = []
    # grid lines (subtle)
    for v in [1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 5.5, 6.0, 6.5]:
        svg_parts.append(f'<line x1="{L}" y1="{sy(v):.1f}" x2="{R}" y2="{sy(v):.1f}" stroke="#f0f0f0" stroke-width="1"/>')
    # y-axis labels
    for v in [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0]:
        svg_parts.append(f'<text x="{L-12}" y="{sy(v)+4:.1f}" text-anchor="end" font-size="12" fill="#888">{v:.1f}</text>')

    # +2σ zone fill (between +1σ and +2σ)
    svg_parts.append(f'<rect x="{L}" y="{sy(mean+2*std):.1f}" width="{W}" height="{sy(mean+std)-sy(mean+2*std):.1f}" fill="#fef2f2" opacity="0.6"/>')
    # +1σ zone fill (between mean and +1σ)
    svg_parts.append(f'<rect x="{L}" y="{sy(mean+std):.1f}" width="{W}" height="{sy(mean)-sy(mean+std):.1f}" fill="#f0fdf4" opacity="0.5"/>')
    # -1σ zone (between mean and -1σ)
    svg_parts.append(f'<rect x="{L}" y="{sy(mean):.1f}" width="{W}" height="{sy(mean-std)-sy(mean):.1f}" fill="#f0fdf4" opacity="0.5"/>')

    # σ lines
    svg_parts.append(f'<line x1="{L}" y1="{sy(mean):.1f}" x2="{R}" y2="{sy(mean):.1f}" stroke="#3b82f6" stroke-width="2" stroke-dasharray="8,4"/>')
    svg_parts.append(f'<text x="{R+8}" y="{sy(mean)+4:.1f}" font-size="12" fill="#3b82f6" font-weight="bold">μ={mean:.1f}</text>')
    svg_parts.append(f'<line x1="{L}" y1="{sy(mean+std):.1f}" x2="{R}" y2="{sy(mean+std):.1f}" stroke="#f59e0b" stroke-width="1" stroke-dasharray="5,3"/>')
    svg_parts.append(f'<text x="{R+8}" y="{sy(mean+std)+4:.1f}" font-size="11" fill="#f59e0b">+1σ={mean+std:.1f}</text>')
    svg_parts.append(f'<line x1="{L}" y1="{sy(mean+2*std):.1f}" x2="{R}" y2="{sy(mean+2*std):.1f}" stroke="#e11d48" stroke-width="1" stroke-dasharray="5,3"/>')
    svg_parts.append(f'<text x="{R+8}" y="{sy(mean+2*std)+4:.1f}" font-size="11" fill="#e11d48">+2σ={mean+2*std:.1f}</text>')
    svg_parts.append(f'<line x1="{L}" y1="{sy(mean-std):.1f}" x2="{R}" y2="{sy(mean-std):.1f}" stroke="#f59e0b" stroke-width="1" stroke-dasharray="5,3"/>')
    svg_parts.append(f'<text x="{R+8}" y="{sy(mean-std)+4:.1f}" font-size="11" fill="#f59e0b">-1σ={mean-std:.1f}</text>')

    # opportunity zone annotation (right side)
    opp_y = sy(mean+std)
    svg_parts.append(f'<rect x="{L}" y="{sy(mean+2*std):.1f}" width="8" height="{sy(mean+std)-sy(mean+2*std):.1f}" fill="#e11d48" opacity="0.3"/>')
    svg_parts.append(f'<text x="{L+15}" y="{sy(mean+1.5*std):.1f}" font-size="11" fill="#e11d48" font-weight="bold">← 套利机会区域</text>')

    # polyline
    svg_parts.append(f'<polyline points="{pts}" fill="none" stroke="#3b82f6" stroke-width="2.5" stroke-linejoin="round"/>')

    # Entry signal at index 12 (value 6.1)
    ei = 12
    ex, ey = sx(ei), sy(data[ei])
    svg_parts.append(f'<circle cx="{ex:.1f}" cy="{ey:.1f}" r="8" fill="none" stroke="#e11d48" stroke-width="2.5"/>')
    svg_parts.append(f'<line x1="{ex:.1f}" y1="{ey-12:.1f}" x2="{ex:.1f}" y2="{ey-40:.1f}" stroke="#e11d48" stroke-width="1.5"/>')
    svg_parts.append(f'<text x="{ex:.1f}" y="{ey-46:.1f}" text-anchor="middle" font-size="13" fill="#e11d48" font-weight="bold">入场信号</text>')

    # Exit signal at index 18 (value 2.5, approaching mean from below after peak)
    xi = 18
    xx, xy = sx(xi), sy(data[xi])
    svg_parts.append(f'<circle cx="{xx:.1f}" cy="{xy:.1f}" r="7" fill="none" stroke="#16a34a" stroke-width="2.5"/>')
    svg_parts.append(f'<line x1="{xx:.1f}" y1="{xy+10:.1f}" x2="{xx:.1f}" y2="{xy+35:.1f}" stroke="#16a34a" stroke-width="1.5"/>')
    svg_parts.append(f'<text x="{xx:.1f}" y="{xy+48:.1f}" text-anchor="middle" font-size="13" fill="#16a34a" font-weight="bold">平仓获利</text>')

    # Second entry at index 47 (value 6.0)
    ei2 = 47
    ex2, ey2 = sx(ei2), sy(data[ei2])
    svg_parts.append(f'<circle cx="{ex2:.1f}" cy="{ey2:.1f}" r="8" fill="none" stroke="#e11d48" stroke-width="2.5"/>')
    svg_parts.append(f'<line x1="{ex2:.1f}" y1="{ey2-12:.1f}" x2="{ex2:.1f}" y2="{ey2-40:.1f}" stroke="#e11d48" stroke-width="1.5"/>')
    svg_parts.append(f'<text x="{ex2:.1f}" y="{ey2-46:.1f}" text-anchor="middle" font-size="13" fill="#e11d48" font-weight="bold">入场信号</text>')

    # Second exit at index 55 (value 2.0)
    xi2 = 55
    xx2, xy2 = sx(xi2), sy(data[xi2])
    svg_parts.append(f'<circle cx="{xx2:.1f}" cy="{xy2:.1f}" r="7" fill="none" stroke="#16a34a" stroke-width="2.5"/>')
    svg_parts.append(f'<text x="{xx2:.1f}" y="{xy2+22:.1f}" text-anchor="middle" font-size="13" fill="#16a34a" font-weight="bold">平仓获利</text>')

    # axes
    svg_parts.append(f'<line x1="{L}" y1="{B}" x2="{R}" y2="{B}" stroke="#ccc" stroke-width="1"/>')
    svg_parts.append(f'<line x1="{L}" y1="{T}" x2="{L}" y2="{B}" stroke="#ccc" stroke-width="1"/>')
    # x-axis labels
    for i in range(0, n, 10):
        svg_parts.append(f'<text x="{sx(i):.1f}" y="{B+18}" text-anchor="middle" font-size="11" fill="#888">T{i}</text>')

    svg_content = "\n    ".join(svg_parts)
    body = f"""
<div style="width:1120px;height:595px;position:relative">
  <div style="text-align:center;margin-bottom:8px">
    <span style="font-size:22px;font-weight:bold;color:#1e293b">价差发现与均值回归</span>
  </div>
  <svg width="1120" height="520" viewBox="0 0 1120 520">
    {svg_content}
  </svg>
  <div style="text-align:center;margin-top:4px">
    <span style="font-size:12px;color:#94a3b8">当价差偏离统计均值超过阈值时，系统识别为套利机会</span>
  </div>
</div>"""
    return html_wrap("价差发现与均值回归", body)


# ─── Chart 2: entry-exit-signals ──────────────────────────────────────
def gen_entry_exit_signals():
    # Left panel: histogram
    # bins: 0-1, 1-2, 2-3, 3-4, 4-5, 5-6, 6-7, 7-8, 8-9, 9-10
    bins =    [0,  1,  2,  3,  4,  5,  6,  7,  8,  9]
    counts =  [2,  5,  8, 14, 18, 15, 10,  6,  3,  1]
    # zones: 0-2 rejected(gray), 2-4 candidate(yellow), 4-8 executable(green), 8-10 rejected(gray)
    max_count = max(counts)

    hL, hR, hT, hB = 60, 480, 80, 420
    hW, hH = hR-hL, hB-hT
    bar_w = hW / len(bins) - 4

    hist_parts = []
    # bars
    for i, (b, c) in enumerate(zip(bins, counts)):
        x = hL + i*(hW/len(bins)) + 2
        bh = c/max_count * hH
        y = hB - bh
        # color by zone
        if b < 2:
            color = "#d1d5db"  # gray rejected
        elif b < 4:
            color = "#fde68a"  # yellow candidate
        elif b < 8:
            color = "#86efac"  # green executable
        else:
            color = "#d1d5db"  # gray rejected
        hist_parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{bh:.1f}" fill="{color}" stroke="#fff" stroke-width="1" rx="2"/>')
        hist_parts.append(f'<text x="{x+bar_w/2:.1f}" y="{y-5:.1f}" text-anchor="middle" font-size="11" fill="#555">{c}</text>')

    # x-axis labels
    for i in range(11):
        x = hL + i*(hW/len(bins))
        hist_parts.append(f'<text x="{x:.1f}" y="{hB+18}" text-anchor="middle" font-size="11" fill="#888">{i}</text>')
    hist_parts.append(f'<text x="{(hL+hR)/2:.1f}" y="{hB+35}" text-anchor="middle" font-size="12" fill="#666">价差值</text>')

    # y-axis
    for c in [0, 5, 10, 15, 20]:
        y = hB - c/max_count*hH
        hist_parts.append(f'<text x="{hL-8}" y="{y+4:.1f}" text-anchor="end" font-size="11" fill="#888">{c}</text>')
    hist_parts.append(f'<text x="{hL-35}" y="{(hT+hB)/2:.1f}" text-anchor="middle" font-size="12" fill="#666" transform="rotate(-90,{hL-35},{(hT+hB)/2:.1f})">频次</text>')

    # cost protection line (P90 cost) at x=3
    cost_x = hL + 3*(hW/len(bins)) + hW/len(bins)*0.5
    hist_parts.append(f'<line x1="{cost_x:.1f}" y1="{hT}" x2="{cost_x:.1f}" y2="{hB}" stroke="#e11d48" stroke-width="2" stroke-dasharray="6,3"/>')
    hist_parts.append(f'<text x="{cost_x:.1f}" y="{hT-8}" text-anchor="middle" font-size="11" fill="#e11d48" font-weight="bold">成本保护线</text>')
    hist_parts.append(f'<text x="{cost_x:.1f}" y="{hT-22}" text-anchor="middle" font-size="10" fill="#e11d48">(P90 cost)</text>')

    # entry threshold line at x=5
    entry_x = hL + 5*(hW/len(bins)) + hW/len(bins)*0.3
    hist_parts.append(f'<line x1="{entry_x:.1f}" y1="{hT}" x2="{entry_x:.1f}" y2="{hB}" stroke="#16a34a" stroke-width="2" stroke-dasharray="6,3"/>')
    hist_parts.append(f'<text x="{entry_x:.1f}" y="{hT-8}" text-anchor="middle" font-size="11" fill="#16a34a" font-weight="bold">入场线</text>')
    hist_parts.append(f'<text x="{entry_x:.1f}" y="{hT-22}" text-anchor="middle" font-size="10" fill="#16a34a">(entry threshold)</text>')

    # zone labels at bottom
    hist_parts.append(f'<text x="{hL+1*(hW/len(bins)):.1f}" y="{hB+50}" text-anchor="middle" font-size="10" fill="#9ca3af">rejected</text>')
    hist_parts.append(f'<text x="{hL+3*(hW/len(bins)):.1f}" y="{hB+50}" text-anchor="middle" font-size="10" fill="#d97706">candidate</text>')
    hist_parts.append(f'<text x="{hL+6*(hW/len(bins)):.1f}" y="{hB+50}" text-anchor="middle" font-size="10" fill="#16a34a">executable</text>')
    hist_parts.append(f'<text x="{hL+9*(hW/len(bins)):.1f}" y="{hB+50}" text-anchor="middle" font-size="10" fill="#9ca3af">rejected</text>')

    # axes
    hist_parts.append(f'<line x1="{hL}" y1="{hB}" x2="{hR}" y2="{hB}" stroke="#ccc" stroke-width="1"/>')
    hist_parts.append(f'<line x1="{hL}" y1="{hT}" x2="{hL}" y2="{hB}" stroke="#ccc" stroke-width="1"/>')

    hist_svg = "\n      ".join(hist_parts)

    # Right panel: exit signal curve
    rL, rR, rT, rB = 580, 1100, 80, 420
    rW, rH = rR-rL, rB-rT
    # curve from entry spread going down to exit
    close_data = [5.8, 5.6, 5.5, 5.3, 5.0, 4.8, 4.6, 4.5, 4.3, 4.1, 3.9, 3.8, 3.6, 3.5, 3.4, 3.3, 3.2, 3.1, 3.0, 2.9, 2.85, 2.8, 2.78]
    cn = len(close_data)
    ry_min, ry_max = 2.0, 6.5
    rx_step = rW/(cn-1)
    csx = lambda i: rL + i*rx_step
    csy = lambda v: rB - (v-ry_min)/(ry_max-ry_min)*rH

    right_parts = []
    # grid
    for v in [2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 5.5, 6.0]:
        right_parts.append(f'<line x1="{rL}" y1="{csy(v):.1f}" x2="{rR}" y2="{csy(v):.1f}" stroke="#f5f5f5" stroke-width="1"/>')

    # entry spread line
    right_parts.append(f'<line x1="{rL}" y1="{csy(5.8):.1f}" x2="{rR}" y2="{csy(5.8):.1f}" stroke="#3b82f6" stroke-width="1.5" stroke-dasharray="6,3"/>')
    right_parts.append(f'<text x="{rR+8}" y="{csy(5.8)+4:.1f}" font-size="11" fill="#3b82f6" font-weight="bold">entry_spread</text>')

    # exit target line
    right_parts.append(f'<line x1="{rL}" y1="{csy(2.8):.1f}" x2="{rR}" y2="{csy(2.8):.1f}" stroke="#16a34a" stroke-width="1.5" stroke-dasharray="6,3"/>')
    right_parts.append(f'<text x="{rR+8}" y="{csy(2.8)+4:.1f}" font-size="11" fill="#16a34a" font-weight="bold">exit_target</text>')

    # profit zone (green shaded between entry and exit)
    right_parts.append(f'<rect x="{rL}" y="{csy(5.8):.1f}" width="{rW}" height="{csy(2.8)-csy(5.8):.1f}" fill="#dcfce7" opacity="0.4"/>')

    # curve
    cpts = " ".join(f"{csx(i):.1f},{csy(v):.1f}" for i,v in enumerate(close_data))
    right_parts.append(f'<polyline points="{cpts}" fill="none" stroke="#3b82f6" stroke-width="2.5" stroke-linejoin="round"/>')

    # entry point
    right_parts.append(f'<circle cx="{csx(0):.1f}" cy="{csy(5.8):.1f}" r="6" fill="#3b82f6"/>')
    right_parts.append(f'<text x="{csx(0)+10:.1f}" y="{csy(5.8)-10:.1f}" font-size="12" fill="#3b82f6" font-weight="bold">入场点</text>')

    # exit point
    last_i = cn-1
    right_parts.append(f'<circle cx="{csx(last_i):.1f}" cy="{csy(close_data[last_i]):.1f}" r="6" fill="#16a34a"/>')
    right_parts.append(f'<text x="{csx(last_i)+10:.1f}" y="{csy(close_data[last_i])-10:.1f}" font-size="12" fill="#16a34a" font-weight="bold">平仓点</text>')

    # profit arrow
    arrow_x = (csx(0) + csx(last_i))/2
    right_parts.append(f'<line x1="{arrow_x:.1f}" y1="{csy(5.5):.1f}" x2="{arrow_x:.1f}" y2="{csy(3.1):.1f}" stroke="#16a34a" stroke-width="2" marker-end="url(#arrowG)"/>')
    right_parts.append(f'<text x="{arrow_x+12:.1f}" y="{(csy(5.5)+csy(3.1))/2:.1f}" font-size="13" fill="#16a34a" font-weight="bold">价差收敛 = 利润</text>')

    # arrowhead marker
    defs = '<defs><marker id="arrowG" markerWidth="10" markerHeight="7" refX="9" refY="3.5" orient="auto"><polygon points="0 0, 10 3.5, 0 7" fill="#16a34a"/></marker></defs>'

    # axes
    right_parts.append(f'<line x1="{rL}" y1="{rB}" x2="{rR}" y2="{rB}" stroke="#ccc" stroke-width="1"/>')
    right_parts.append(f'<line x1="{rL}" y1="{rT}" x2="{rL}" y2="{rB}" stroke="#ccc" stroke-width="1"/>')
    for v in [2.0, 3.0, 4.0, 5.0, 6.0]:
        y = csy(v)
        right_parts.append(f'<text x="{rL-8}" y="{y+4:.1f}" text-anchor="end" font-size="11" fill="#888">{v:.1f}</text>')

    right_svg = "\n      ".join(right_parts)

    body = f"""
<div style="width:1120px;height:595px;position:relative">
  <div style="text-align:center;margin-bottom:6px">
    <span style="font-size:22px;font-weight:bold;color:#1e293b">入场与退出信号判定</span>
  </div>
  <div style="display:flex;gap:20px">
    <div style="flex:0 0 500px">
      <div style="text-align:center;font-size:14px;color:#64748b;margin-bottom:4px;font-weight:bold">价差分布与信号区域</div>
      <svg width="500" height="460" viewBox="0 0 500 460">
        {hist_svg}
      </svg>
    </div>
    <div style="flex:0 0 560px">
      <div style="text-align:center;font-size:14px;color:#64748b;margin-bottom:4px;font-weight:bold">退出信号示意</div>
      <svg width="560" height="460" viewBox="0 0 560 460">
        {defs}
        {right_svg}
      </svg>
    </div>
  </div>
</div>"""
    return html_wrap("入场与退出信号判定", body)


# ─── Chart 3: cost-breakdown (Chart.js) ───────────────────────────────
def gen_cost_breakdown():
    body = """
<div style="width:1120px;height:595px;position:relative;display:flex;flex-direction:column;align-items:center">
  <div style="text-align:center;margin-bottom:10px">
    <span style="font-size:22px;font-weight:bold;color:#1e293b">8项成本结构分解</span>
  </div>
  <div style="display:flex;align-items:center;gap:40px;flex:1">
    <div style="position:relative;width:420px;height:420px">
      <canvas id="costChart" width="420" height="420"></canvas>
    </div>
    <div style="display:flex;flex-direction:column;gap:12px">
      <div style="display:flex;align-items:center;gap:10px"><span style="display:inline-block;width:16px;height:16px;background:#3b82f6;border-radius:3px"></span><span style="font-size:14px;color:#334155">HL手续费 <b>12%</b></span></div>
      <div style="display:flex;align-items:center;gap:10px"><span style="display:inline-block;width:16px;height:16px;background:#0d9488;border-radius:3px"></span><span style="font-size:14px;color:#334155">HL买卖价差 <b>18%</b></span></div>
      <div style="display:flex;align-items:center;gap:10px"><span style="display:inline-block;width:16px;height:16px;background:#d97706;border-radius:3px"></span><span style="font-size:14px;color:#334155">HL资金费 <b>8%</b></span></div>
      <div style="display:flex;align-items:center;gap:10px"><span style="display:inline-block;width:16px;height:16px;background:#e11d48;border-radius:3px"></span><span style="font-size:14px;color:#334155">MT5点差(扣除20%返佣) <b>25%</b></span></div>
      <div style="display:flex;align-items:center;gap:10px"><span style="display:inline-block;width:16px;height:16px;background:#7c3aed;border-radius:3px"></span><span style="font-size:14px;color:#334155">MT5佣金 <b>5%</b></span></div>
      <div style="display:flex;align-items:center;gap:10px"><span style="display:inline-block;width:16px;height:16px;background:#2563eb;border-radius:3px"></span><span style="font-size:14px;color:#334155">MT5隔夜费 <b>10%</b></span></div>
      <div style="display:flex;align-items:center;gap:10px"><span style="display:inline-block;width:16px;height:16px;background:#16a34a;border-radius:3px"></span><span style="font-size:14px;color:#334155">滑点预估 <b>15%</b></span></div>
      <div style="display:flex;align-items:center;gap:10px"><span style="display:inline-block;width:16px;height:16px;background:#f59e0b;border-radius:3px"></span><span style="font-size:14px;color:#334155">汇率损耗 <b>7%</b></span></div>
    </div>
  </div>
  <div style="text-align:center;margin-top:8px">
    <span style="font-size:12px;color:#94a3b8">8项全口径成本确保套利决策基于真实盈利空间</span>
  </div>
</div>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script>
const ctx = document.getElementById('costChart').getContext('2d');
const centerText = {
  id: 'centerText',
  afterDraw(chart) {
    const {ctx, chartArea:{left,right,top,bottom}} = chart;
    const cx = (left+right)/2, cy = (top+bottom)/2;
    ctx.save();
    ctx.font = 'bold 28px Arial';
    ctx.fillStyle = '#1e293b';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText('$47.3', cx, cy-10);
    ctx.font = '14px Arial';
    ctx.fillStyle = '#94a3b8';
    ctx.fillText('总成本 / 笔', cx, cy+16);
    ctx.restore();
  }
};
new Chart(ctx, {
  type: 'doughnut',
  data: {
    labels: ['HL手续费','HL买卖价差','HL资金费','MT5点差','MT5佣金','MT5隔夜费','滑点预估','汇率损耗'],
    datasets: [{
      data: [12, 18, 8, 25, 5, 10, 15, 7],
      backgroundColor: ['#3b82f6','#0d9488','#d97706','#e11d48','#7c3aed','#2563eb','#16a34a','#f59e0b'],
      borderWidth: 2,
      borderColor: '#fff'
    }]
  },
  options: {
    responsive: false,
    cutout: '58%',
    plugins: {
      legend: { display: false },
      tooltip: {
        callbacks: {
          label: function(c) { return c.label + ': ' + c.parsed + '%'; }
        }
      }
    }
  },
  plugins: [centerText]
});
</script>"""
    return html_wrap("8项成本结构分解", body)


# ─── Chart 4: open-decision flow ──────────────────────────────────────
def gen_open_decision():
    # Flow chart - vertical layout with branches
    # We'll use a compact layout
    steps = [
        # (label, shape, color, branch_label, branch_direction)
        ("价差扫描 (100ms)", "rect", "#3b82f6", None, None),
        ("成本保护线检查", "diamond", "#f59e0b", "rejected", "right"),
        ("入场线检查", "diamond", "#f59e0b", "candidate", "right"),
        ("流动性检查", "diamond", "#f59e0b", "rejected", "right"),
        ("断路器检查", "diamond", "#e11d48", "暂停交易", "right"),
        ("风控预检 (6项)", "diamond", "#e11d48", "中止", "right"),
        ("严格报价同步", "rect", "#3b82f6", None, None),
        ("价差再验证", "diamond", "#f59e0b", "放弃", "right"),
        ("双边下单 (HL + MT5)", "rect", "#16a34a", None, None),
        ("对冲组建立", "rect", "#16a34a", None, None),
    ]

    svg_w, svg_h = 1120, 500
    cx = 380  # center x for flow
    start_y = 15
    step_h = 46
    box_w = 200
    box_h = 32
    diamond_w = 220
    diamond_h = 38

    parts = []
    # Title area is outside SVG

    y = start_y
    positions = []  # (x, y, shape, label, color)

    for i, (label, shape, color, branch, bdir) in enumerate(steps):
        if shape == "rect":
            positions.append((cx, y, box_w, box_h, label, color, branch, bdir))
            y += step_h
        else:  # diamond
            positions.append((cx, y, diamond_w, diamond_h, label, color, branch, bdir))
            y += step_h + 4

    # Draw connecting lines
    for i in range(len(positions)-1):
        x1, y1, w1, h1, l1, c1, b1, d1 = positions[i]
        x2, y2, w2, h2, l2, c2, b2, d2 = positions[i+1]
        bot = y1 + h1/2
        top = y2 - h2/2
        parts.append(f'<line x1="{x1}" y1="{bot:.1f}" x2="{x2}" y2="{top:.1f}" stroke="#94a3b8" stroke-width="1.5"/>')
        # arrow
        parts.append(f'<polygon points="{x2-4},{top-2:.1f} {x2+4},{top-2:.1f} {x2},{top+4:.1f}" fill="#94a3b8"/>')

    # Draw shapes
    for i, (x, y, w, h, label, color, branch, bdir) in enumerate(positions):
        if label == "价差扫描 (100ms)":
            # rounded rect
            parts.append(f'<rect x="{x-w/2:.1f}" y="{y-h/2:.1f}" width="{w}" height="{h}" rx="6" fill="{color}" opacity="0.9"/>')
            parts.append(f'<text x="{x}" y="{y+4:.1f}" text-anchor="middle" font-size="12" fill="white" font-weight="bold">{label}</text>')
        elif "下单" in label or "对冲组" in label:
            parts.append(f'<rect x="{x-w/2:.1f}" y="{y-h/2:.1f}" width="{w}" height="{h}" rx="6" fill="{color}" opacity="0.9"/>')
            parts.append(f'<text x="{x}" y="{y+4:.1f}" text-anchor="middle" font-size="12" fill="white" font-weight="bold">{label}</text>')
        elif label == "严格报价同步":
            parts.append(f'<rect x="{x-w/2:.1f}" y="{y-h/2:.1f}" width="{w}" height="{h}" rx="6" fill="{color}" opacity="0.9"/>')
            parts.append(f'<text x="{x}" y="{y+4:.1f}" text-anchor="middle" font-size="12" fill="white" font-weight="bold">{label}</text>')
        else:
            # diamond
            dw, dh = w/2, h/2
            diamond_pts = f"{x},{y-dh:.1f} {x+dw:.1f},{y} {x},{y+dh:.1f} {x-dw:.1f},{y}"
            parts.append(f'<polygon points="{diamond_pts}" fill="{color}" opacity="0.15" stroke="{color}" stroke-width="1.5"/>')
            parts.append(f'<text x="{x}" y="{y+4:.1f}" text-anchor="middle" font-size="11" fill="#334155" font-weight="bold">{label}</text>')

        # branch labels
        if branch:
            bx = x + w/2 + 10
            parts.append(f'<line x1="{x+w/2:.1f}" y1="{y}" x2="{bx+5:.1f}" y2="{y}" stroke="{color}" stroke-width="1.2"/>')
            # branch endpoint
            end_x = bx + 60
            parts.append(f'<rect x="{bx+8:.1f}" y="{y-11:.1f}" width="58" height="22" rx="4" fill="{color}" opacity="0.12"/>')
            parts.append(f'<text x="{bx+37:.1f}" y="{y+4:.1f}" text-anchor="middle" font-size="11" fill="{color}" font-weight="bold">{branch}</text>')

    # "四层闸门" bracket on left
    bracket_x = cx - diamond_w/2 - 50
    # covers steps 1-4 (indices 1,2,3,4)
    y_top = positions[1][1] - positions[1][3]/2
    y_bot = positions[4][1] + positions[4][3]/2
    parts.append(f'<line x1="{bracket_x+15}" y1="{y_top:.1f}" x2="{bracket_x}" y2="{y_top:.1f}" stroke="#64748b" stroke-width="1.5"/>')
    parts.append(f'<line x1="{bracket_x}" y1="{y_top:.1f}" x2="{bracket_x}" y2="{y_bot:.1f}" stroke="#64748b" stroke-width="1.5"/>')
    parts.append(f'<line x1="{bracket_x+15}" y1="{y_bot:.1f}" x2="{bracket_x}" y2="{y_bot:.1f}" stroke="#64748b" stroke-width="1.5"/>')
    parts.append(f'<text x="{bracket_x-8}" y="{(y_top+y_bot)/2+4:.1f}" text-anchor="middle" font-size="13" fill="#475569" font-weight="bold" writing-mode="tb">四层闸门</text>')

    # "执行保护" bracket on left for steps 5-8
    bracket_x2 = cx - diamond_w/2 - 50
    y_top2 = positions[5][1] - positions[5][3]/2
    y_bot2 = positions[8][1] - positions[8][3]/2
    parts.append(f'<line x1="{bracket_x2+15}" y1="{y_top2:.1f}" x2="{bracket_x2}" y2="{y_top2:.1f}" stroke="#64748b" stroke-width="1.5"/>')
    parts.append(f'<line x1="{bracket_x2}" y1="{y_top2:.1f}" x2="{bracket_x2}" y2="{y_bot2:.1f}" stroke="#64748b" stroke-width="1.5"/>')
    parts.append(f'<line x1="{bracket_x2+15}" y1="{y_bot2:.1f}" x2="{bracket_x2}" y2="{y_bot2:.1f}" stroke="#64748b" stroke-width="1.5"/>')
    parts.append(f'<text x="{bracket_x2-8}" y="{(y_top2+y_bot2)/2+4:.1f}" text-anchor="middle" font-size="13" fill="#475569" font-weight="bold" writing-mode="tb">执行保护</text>')

    svg_content = "\n      ".join(parts)
    body = f"""
<div style="width:1120px;height:595px;position:relative">
  <div style="text-align:center;margin-bottom:6px">
    <span style="font-size:22px;font-weight:bold;color:#1e293b">开仓决策流程：10步保护链</span>
  </div>
  <svg width="{svg_w}" height="{svg_h}" viewBox="0 0 {svg_w} {svg_h}">
      {svg_content}
  </svg>
</div>"""
    return html_wrap("开仓决策流程", body)


# ─── Chart 5: close-conditions ────────────────────────────────────────
def gen_close_conditions():
    panels = []

    # Panel 1: Exit trigger
    p1_parts = []
    pL, pR, pT, pB = 40, 330, 50, 340
    pW, pH = pR-pL, pB-pT
    # curve descending from high to low
    curve1 = [5.8, 5.6, 5.5, 5.3, 5.0, 4.7, 4.5, 4.2, 4.0, 3.8, 3.5, 3.3, 3.1, 2.9, 2.8, 2.75, 2.7, 2.68]
    cn = len(curve1)
    y_min, y_max = 2.0, 6.5
    x_step = pW/(cn-1)
    sx1 = lambda i: pL + i*x_step
    sy1 = lambda v: pB - (v-y_min)/(y_max-y_min)*pH

    # entry line
    p1_parts.append(f'<line x1="{pL}" y1="{sy1(5.8):.1f}" x2="{pR}" y2="{sy1(5.8):.1f}" stroke="#3b82f6" stroke-width="1.5" stroke-dasharray="5,3"/>')
    p1_parts.append(f'<text x="{pR+5}" y="{sy1(5.8)+3:.1f}" font-size="9" fill="#3b82f6">entry</text>')
    # exit line
    p1_parts.append(f'<line x1="{pL}" y1="{sy1(2.8):.1f}" x2="{pR}" y2="{sy1(2.8):.1f}" stroke="#16a34a" stroke-width="1.5" stroke-dasharray="5,3"/>')
    p1_parts.append(f'<text x="{pR+5}" y="{sy1(2.8)+3:.1f}" font-size="9" fill="#16a34a">exit</text>')
    # profit zone
    p1_parts.append(f'<rect x="{pL}" y="{sy1(5.8):.1f}" width="{pW}" height="{sy1(2.8)-sy1(5.8):.1f}" fill="#dcfce7" opacity="0.3"/>')
    # curve
    pts1 = " ".join(f"{sx1(i):.1f},{sy1(v):.1f}" for i,v in enumerate(curve1))
    p1_parts.append(f'<polyline points="{pts1}" fill="none" stroke="#3b82f6" stroke-width="2"/>')
    # trigger point
    ti = len(curve1)-1
    p1_parts.append(f'<circle cx="{sx1(ti):.1f}" cy="{sy1(curve1[ti]):.1f}" r="5" fill="#16a34a"/>')
    p1_parts.append(f'<text x="{sx1(ti)-5:.1f}" y="{sy1(curve1[ti])-10:.1f}" font-size="10" fill="#16a34a" font-weight="bold" text-anchor="end">触发平仓</text>')
    # profit arrow
    ax = (sx1(0)+sx1(ti))/2 - 10
    p1_parts.append(f'<line x1="{ax:.1f}" y1="{sy1(5.5):.1f}" x2="{ax:.1f}" y2="{sy1(3.1):.1f}" stroke="#16a34a" stroke-width="1.5"/>')
    p1_parts.append(f'<polygon points="{ax-3},{sy1(3.1)+2:.1f} {ax+3},{sy1(3.1)+2:.1f} {ax},{sy1(3.1)+8:.1f}" fill="#16a34a"/>')
    p1_parts.append(f'<text x="{ax-8:.1f}" y="{(sy1(5.5)+sy1(3.1))/2:.1f}" font-size="10" fill="#16a34a" text-anchor="end" transform="rotate(-90,{ax-8:.1f},{(sy1(5.5)+sy1(3.1))/2:.1f})">利润</text>')
    # axes
    p1_parts.append(f'<line x1="{pL}" y1="{pB}" x2="{pR}" y2="{pB}" stroke="#e5e7eb" stroke-width="1"/>')

    panels.append(("退出线触发（最常见）", "\n".join(p1_parts), 370, 400))

    # Panel 2: Timeout trigger
    p2_parts = []
    pL2, pR2 = 40, 330
    pW2 = pR2-pL2
    # timeline
    ty = 200
    p2_parts.append(f'<line x1="{pL2}" y1="{ty}" x2="{pR2}" y2="{ty}" stroke="#94a3b8" stroke-width="2"/>')
    # time ticks
    for i, t in enumerate(["0", "T₁", "T₂", "T₃", "max"]):
        tx = pL2 + i*(pW2/4)
        p2_parts.append(f'<line x1="{tx:.1f}" y1="{ty-5}" x2="{tx:.1f}" y2="{ty+5}" stroke="#94a3b8" stroke-width="1.5"/>')
        p2_parts.append(f'<text x="{tx:.1f}" y="{ty+20}" text-anchor="middle" font-size="10" fill="#64748b">{t}</text>')
    # max point highlight
    max_x = pR2
    p2_parts.append(f'<circle cx="{max_x}" cy="{ty}" r="6" fill="#f59e0b"/>')
    # condition box
    p2_parts.append(f'<rect x="{pL2+20}" y="{ty+45}" width="{pW2-40}" height="50" rx="8" fill="#fef3c7" stroke="#f59e0b" stroke-width="1.5"/>')
    p2_parts.append(f'<text x="{(pL2+pR2)/2:.1f}" y="{ty+68}" text-anchor="middle" font-size="12" fill="#92400e" font-weight="bold">持仓超时 + 利润达标</text>')
    p2_parts.append(f'<text x="{(pL2+pR2)/2:.1f}" y="{ty+85}" text-anchor="middle" font-size="12" fill="#92400e" font-weight="bold">→ 平仓</text>')
    # arrow from timeout to box
    p2_parts.append(f'<line x1="{max_x}" y1="{ty+8}" x2="{max_x-30:.1f}" y2="{ty+42}" stroke="#f59e0b" stroke-width="1.5" stroke-dasharray="4,2"/>')
    # label
    p2_parts.append(f'<text x="{(pL2+pR2)/2:.1f}" y="{ty-30}" text-anchor="middle" font-size="11" fill="#64748b">max_holding_minutes</text>')
    p2_parts.append(f'<line x1="{pL2}" y1="{pB}" x2="{pR2}" y2="{pB}" stroke="#e5e7eb" stroke-width="1"/>')

    panels.append(("超时触发", "\n".join(p2_parts), 370, 400))

    # Panel 3: Profit protection
    p3_parts = []
    pL3, pR3, pT3, pB3 = 40, 330, 50, 340
    pW3, pH3 = pR3-pL3, pB3-pT3
    # profit curve: starts negative, goes positive
    profit_data = [-2.0, -1.5, -0.8, 0.2, 1.0, 2.5, 3.8, 5.0, 5.5, 4.8, 4.2, 3.5, 5.2, 6.0, 5.8, 6.5, 7.0, 6.8]
    pn = len(profit_data)
    py_min, py_max = -3.0, 8.0
    px_step = pW3/(pn-1)
    sx3 = lambda i: pL3 + i*px_step
    sy3 = lambda v: pB3 - (v-py_min)/(py_max-py_min)*pH3

    # auto_close_min_profit line
    p3_parts.append(f'<line x1="{pL3}" y1="{sy3(2.0):.1f}" x2="{pR3}" y2="{sy3(2.0):.1f}" stroke="#e11d48" stroke-width="1.5" stroke-dasharray="6,3"/>')
    p3_parts.append(f'<text x="{pR3+5}" y="{sy3(2.0)+3:.1f}" font-size="9" fill="#e11d48">auto_close</text>')
    p3_parts.append(f'<text x="{pR3+5}" y="{sy3(2.0)+14:.1f}" font-size="9" fill="#e11d48">min_profit</text>')
    # zero line
    p3_parts.append(f'<line x1="{pL3}" y1="{sy3(0):.1f}" x2="{pR3}" y2="{sy3(0):.1f}" stroke="#e5e7eb" stroke-width="1"/>')
    p3_parts.append(f'<text x="{pL3-5}" y="{sy3(0)+3:.1f}" text-anchor="end" font-size="9" fill="#94a3b8">0</text>')
    # profit zone shading
    p3_parts.append(f'<rect x="{pL3}" y="{sy3(2.0):.1f}" width="{pW3}" height="{sy3(0)-sy3(2.0):.1f}" fill="#fef2f2" opacity="0.3"/>')
    p3_parts.append(f'<rect x="{pL3}" y="{sy3(8.0):.1f}" width="{pW3}" height="{sy3(2.0)-sy3(8.0):.1f}" fill="#dcfce7" opacity="0.2"/>')
    # curve
    pts3 = " ".join(f"{sx3(i):.1f},{sy3(v):.1f}" for i,v in enumerate(profit_data))
    p3_parts.append(f'<polyline points="{pts3}" fill="none" stroke="#7c3aed" stroke-width="2"/>')
    # label where profit crosses threshold
    for i in range(1, pn):
        if profit_data[i] >= 2.0 and profit_data[i-1] < 2.0:
            cx_cross = sx3(i)
            p3_parts.append(f'<circle cx="{cx_cross:.1f}" cy="{sy3(2.0):.1f}" r="5" fill="#16a34a"/>')
            p3_parts.append(f'<text x="{cx_cross+8:.1f}" y="{sy3(2.0)-8:.1f}" font-size="10" fill="#16a34a" font-weight="bold">可平仓</text>')
            break
    # y-axis labels
    for v in [-2, 0, 2, 4, 6, 8]:
        p3_parts.append(f'<text x="{pL3-5}" y="{sy3(v)+3:.1f}" text-anchor="end" font-size="9" fill="#94a3b8">{v}</text>')
    p3_parts.append(f'<text x="{pL3-20}" y="{(pT3+pB3)/2:.1f}" text-anchor="middle" font-size="10" fill="#64748b" transform="rotate(-90,{pL3-20},{(pT3+pB3)/2:.1f})">估算利润</text>')
    p3_parts.append(f'<line x1="{pL3}" y1="{pB3}" x2="{pR3}" y2="{pB3}" stroke="#e5e7eb" stroke-width="1"/>')

    panels.append(("利润保护", "\n".join(p3_parts), 370, 400))

    # Build HTML
    panel_html = ""
    for title, svg_content, w, h in panels:
        panel_html += f"""
    <div style="flex:1;border:1px solid #e2e8f0;border-radius:10px;padding:12px;background:#fafbfc">
      <div style="text-align:center;font-size:13px;font-weight:bold;color:#334155;margin-bottom:6px">{title}</div>
      <svg width="{w}" height="{h}" viewBox="0 0 {w} {h}">
        {svg_content}
      </svg>
    </div>"""

    body = f"""
<div style="width:1120px;height:595px;position:relative;display:flex;flex-direction:column">
  <div style="text-align:center;margin-bottom:10px">
    <span style="font-size:22px;font-weight:bold;color:#1e293b">平仓触发条件</span>
  </div>
  <div style="display:flex;gap:12px;flex:1">
    {panel_html}
  </div>
  <div style="text-align:center;margin-top:10px">
    <span style="font-size:12px;color:#94a3b8">三种条件组合确保在最优时机安全退出</span>
  </div>
</div>"""
    return html_wrap("平仓触发条件", body)


# ─── Main ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    os.makedirs(DIR, exist_ok=True)

    files = {
        "spread-discovery.html": gen_spread_discovery(),
        "entry-exit-signals.html": gen_entry_exit_signals(),
        "cost-breakdown.html": gen_cost_breakdown(),
        "open-decision.html": gen_open_decision(),
        "close-conditions.html": gen_close_conditions(),
    }

    for name, content in files.items():
        path = os.path.join(DIR, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"✓ {path}")

    print(f"\nAll {len(files)} files generated in {DIR}")
