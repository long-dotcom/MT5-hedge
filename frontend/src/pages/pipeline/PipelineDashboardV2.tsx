import { ReloadOutlined } from '@ant-design/icons';
import { Button, Select, Switch, Typography } from 'antd';
import type { V2DashboardData, V2HedgeGroup, V2LifecycleCounts, V2NodeStatus, V2PipelineSymbol } from './v2Types';

function delayClass(ms: number) {
  if (ms === 0) return 'delay-idle';
  if (ms < 20) return 'delay-good';
  if (ms <= 100) return 'delay-warn';
  return 'delay-bad';
}

function statusClass(status: V2NodeStatus) {
  return `v2-node-${status}`;
}

function PipelineNode({ label, status }: { label: string; status: V2NodeStatus }) {
  return (
    <div className={`v2-pipeline-node ${statusClass(status)}`}>
      <span />
      <strong>{label}</strong>
    </div>
  );
}

function DelayBadge({ ms }: { ms: number }) {
  return <span className={`v2-delay ${delayClass(ms)}`}>{ms >= 0 ? `${ms}ms` : '-'}</span>;
}

function PipeSegment({ status }: { status: 'normal' | 'blocked' | 'inactive' }) {
  return (
    <svg className="v2-pipe-segment" viewBox="0 0 40 20" preserveAspectRatio="none">
      <rect x="0" y="8" width="40" height="4" rx="2" className={`v2-pipe-track pipe-${status}`} />
      <line x1="2" y1="10" x2="38" y2="10" className={`v2-pipe-flow pipe-${status}`} />
    </svg>
  );
}

function segmentStatus(from: V2NodeStatus, to: V2NodeStatus): 'normal' | 'blocked' | 'inactive' {
  if (from === 'inactive' || to === 'inactive') return 'inactive';
  if (from === 'blocked' || to === 'blocked') return 'blocked';
  return 'normal';
}

function directionText(value: string) {
  if (value === 'long_mt5_short_hyperliquid') return 'long MT5 · short HL';
  if (value === 'long_hyperliquid_short_mt5') return 'long HL · short MT5';
  return value;
}

function PipelineRow({ data }: { data: V2PipelineSymbol }) {
  const isBlocked = data.pipelineStatus === 'blocked';
  const stages = [
    { key: 'scan' as const, label: '扫描', delay: data.delays.syncToScan },
    { key: 'signal' as const, label: '信号', delay: data.delays.scanToSignal },
    { key: 'candidate' as const, label: '候选', delay: data.delays.signalToCandidate },
  ];
  let previous: V2NodeStatus = data.nodes.sync;
  return (
    <div className={`v2-pipeline-row ${isBlocked ? 'is-blocked' : ''}`}>
      <div className="v2-row-header">
        <div className="v2-row-title">
          <strong>{data.symbol}</strong>
          <span>{directionText(data.direction)}</span>
        </div>
        <div className={`v2-spread ${data.spread >= 0 ? 'positive' : 'negative'}`}>价差 {data.spread >= 0 ? '+' : ''}{data.spread.toFixed(2)}</div>
      </div>
      <div className="v2-row-flow">
        <div className="v2-sources">
          <div className="v2-source hl"><i />HL</div>
          <div className="v2-source mt5"><i />MT5</div>
        </div>
        <svg className="v2-merge-svg" viewBox="0 0 56 76">
          <line x1="0" y1="18" x2="24" y2="18" className={`v2-merge-line ${isBlocked ? 'blocked' : 'hl'}`} />
          <line x1="24" y1="18" x2="42" y2="38" className={`v2-merge-line ${isBlocked ? 'blocked' : 'hl'}`} />
          <text x="10" y="13" className={delayClass(data.delays.hlToSync)}>{data.delays.hlToSync}ms</text>
          <line x1="0" y1="58" x2="24" y2="58" className={`v2-merge-line ${isBlocked ? 'blocked' : 'mt5'}`} />
          <line x1="24" y1="58" x2="42" y2="38" className={`v2-merge-line ${isBlocked ? 'blocked' : 'mt5'}`} />
          <text x="10" y="54" className={delayClass(data.delays.mt5ToSync)}>{data.delays.mt5ToSync}ms</text>
          <line x1="42" y1="38" x2="56" y2="38" className={`v2-merge-line ${isBlocked ? 'blocked' : 'normal'}`} />
          <circle cx="42" cy="38" r="3.5" className={`v2-merge-dot ${isBlocked ? 'blocked' : 'normal'}`} />
        </svg>
        <PipelineNode label="同步" status={data.nodes.sync} />
        {stages.map((stage) => {
          const current = data.nodes[stage.key];
          const seg = segmentStatus(previous, current);
          previous = current;
          return (
            <div className="v2-stage-chain" key={stage.key}>
              <PipeSegment status={seg} />
              <DelayBadge ms={stage.delay} />
              <PipeSegment status={seg} />
              <PipelineNode label={stage.label} status={current} />
            </div>
          );
        })}
      </div>
      {isBlocked && data.blockReason && <div className="v2-block-reason">{data.blockReason}</div>}
      {data.netPnl !== undefined && data.annualized !== undefined && data.pipelineStatus === 'normal' && (
        <div className="v2-row-profit">
          <span>净利 <strong>{data.netPnl >= 0 ? '+' : ''}{data.netPnl.toFixed(2)}</strong></span>
          <span>年化 <strong>{data.annualized.toFixed(2)}%</strong></span>
        </div>
      )}
    </div>
  );
}

function PipelinePanelV2({ pipelines }: { pipelines: V2PipelineSymbol[] }) {
  return (
    <section className="v2-panel v2-pipeline-panel">
      <div className="v2-panel-header green">
        <h2>行情管道</h2>
        <span>Hyperliquid ←→ MT5</span>
      </div>
      <div className="v2-legend">
        <span><i className="normal" />正常流动</span>
        <span><i className="blocked" />阻塞</span>
        <span><i className="warning" />警告</span>
        <span><i className="inactive" />未激活</span>
      </div>
      <div className="v2-pipeline-list">
        {pipelines.map((pipeline) => <PipelineRow key={pipeline.symbol} data={pipeline} />)}
      </div>
    </section>
  );
}

const hedgeMeta: Record<V2HedgeGroup['status'], { label: string; cls: string }> = {
  holding: { label: '持仓中', cls: 'holding' },
  closable: { label: '可平仓', cls: 'closable' },
  manual: { label: '人工接管', cls: 'manual' },
  building: { label: '建仓中', cls: 'building' },
};

function HedgeCard({ group }: { group: V2HedgeGroup }) {
  const meta = hedgeMeta[group.status];
  return (
    <div className={`v2-hedge-card ${meta.cls}`}>
      <div className="v2-hedge-title"><strong>{group.symbol}</strong><span>#{group.id}</span></div>
      <div className="v2-hedge-status"><i />{meta.label}</div>
      <div className="v2-hedge-pnl">PnL <strong className={(group.pnl || 0) >= 0 ? 'positive' : 'negative'}>{(group.pnl || 0) >= 0 ? '+' : ''}{(group.pnl || 0).toFixed(2)}</strong></div>
      {group.status === 'building' && <div className="v2-progress"><span style={{ width: '65%' }} /></div>}
      {group.status === 'closable' && <div className="v2-hedge-note">✓ 达退出线 · 价差 {group.currentSpread?.toFixed(1)}</div>}
      {(group.status === 'holding' || group.status === 'manual') && <div className="v2-hedge-note">入场 {group.entrySpread?.toFixed(1)} → 当前 {group.currentSpread?.toFixed(1)}</div>}
    </div>
  );
}

function HedgePoolPanelV2({ hedgeGroups, releasedCount, archivedCount }: { hedgeGroups: V2HedgeGroup[]; releasedCount: number; archivedCount: number }) {
  return (
    <section className="v2-panel v2-pool-panel">
      <div className="v2-panel-header blue">
        <h2>对冲池</h2>
        <span>{hedgeGroups.length} 组在池</span>
      </div>
      <div className="v2-pool-body">
        <div className="v2-pool-entry">
          <div className="v2-entry-circle">候选<br />入口</div>
          <div className="v2-entry-arrow">信号触发</div>
        </div>
        <div className="v2-water-tank">
          <div className="v2-water-label">对冲池</div>
          <svg className="v2-water-wave" viewBox="0 0 200 32" preserveAspectRatio="none">
            <path d="M0 16 Q25 8 50 16 T100 16 T150 16 T200 16 V32 H0 Z" />
          </svg>
          <div className="v2-hedge-grid">{hedgeGroups.map((group) => <HedgeCard key={`${group.symbol}-${group.id}`} group={group} />)}</div>
        </div>
        <div className="v2-pool-exit">
          <div className="v2-exit-gate">平仓<br />闸门</div>
          <div className="v2-exit-arrow" />
          <div className="v2-release-count">已释放<strong>{releasedCount}</strong></div>
          <div className="v2-archive-count">已归档<strong>{archivedCount}</strong></div>
        </div>
      </div>
    </section>
  );
}

const lifecycleStages: Array<{ key: keyof V2LifecycleCounts; label: string; cls: string }> = [
  { key: 'pending', label: '待执行', cls: 'pending' },
  { key: 'building', label: '建仓中', cls: 'building' },
  { key: 'holding', label: '持仓中', cls: 'holding' },
  { key: 'closable', label: '可平仓', cls: 'closable' },
  { key: 'closing', label: '平仓中', cls: 'closing' },
  { key: 'abnormal', label: '异常', cls: 'abnormal' },
];

function LifecycleBarV2({ lifecycle }: { lifecycle: V2LifecycleCounts }) {
  const total = Object.values(lifecycle).reduce((sum, item) => sum + item, 0);
  return (
    <section className="v2-panel v2-lifecycle-panel">
      <div className="v2-panel-header purple"><h2>生命周期</h2><span>共 {total} 组</span></div>
      <div className="v2-lifecycle-bar">
        {lifecycleStages.map((stage) => lifecycle[stage.key] > 0 ? <div key={stage.key} className={stage.cls} style={{ width: `${(lifecycle[stage.key] / Math.max(total, 1)) * 100}%` }}>{lifecycle[stage.key]}</div> : null)}
      </div>
      <div className="v2-lifecycle-grid">
        {lifecycleStages.map((stage) => <div key={stage.key} className={stage.cls}><span>{stage.label}</span><strong>{lifecycle[stage.key]}</strong></div>)}
      </div>
    </section>
  );
}

function BottomStatsV2({ data }: { data: V2DashboardData }) {
  const stats = [
    ['总对冲组', data.stats.totalHedgeGroups, '组'],
    ['占用保证金', data.stats.usedMargin.toLocaleString(undefined, { maximumFractionDigits: 2 }), 'USD'],
    ['浮动盈亏', `${data.stats.floatingPnl >= 0 ? '+' : ''}${data.stats.floatingPnl.toFixed(2)}`, 'USD'],
    ['今日已平仓', data.stats.todayClosed, '组'],
    ['今日已释放', data.stats.todayReleased, '笔'],
  ];
  return (
    <section className="v2-panel v2-bottom-stats">
      {stats.map(([label, value, suffix]) => <div key={label}><span>{label}</span><strong>{value}</strong><em>{suffix}</em></div>)}
    </section>
  );
}

function SummaryCardsV2({ data }: { data: V2DashboardData }) {
  const closable = data.hedgeGroups.filter((item) => item.status === 'closable').length;
  const cards = [
    ['SSE 在线', data.sseStatus.online ? '在线' : '离线', `延迟 ${data.sseStatus.latency}s`, 'green'],
    ['启用品种', data.sseStatus.enabledSymbols, '', 'blue'],
    ['流动正常', data.sseStatus.normalFlow, '', 'green'],
    ['阻塞', data.sseStatus.blockedFlow, '', 'red'],
    ['池中对冲组', data.hedgeGroups.length, '', 'blue'],
    ['可平仓', closable, '', 'orange'],
  ];
  return (
    <div className="v2-summary-grid">
      {cards.map(([label, value, sub, color]) => <div key={label} className={`v2-summary-card ${color}`}><span>{label}</span><strong>{value}</strong>{sub && <em>{sub}</em>}</div>)}
    </div>
  );
}

export function PipelineDashboardV2({
  data,
  autoRefresh,
  onAutoRefreshToggle,
  onRefresh,
  loading,
}: {
  data: V2DashboardData;
  autoRefresh: boolean;
  onAutoRefreshToggle: () => void;
  onRefresh: () => void;
  loading: boolean;
}) {
  return (
    <div className="pipeline-v2">
      <div className="v2-topbar">
        <div className="v2-title"><h1>链路与对冲池</h1><span /><p>SSE 在线</p><em>最后推送：{data.sseStatus.lastPush}s 前</em></div>
        <div className="v2-actions"><Button icon={<ReloadOutlined />} loading={loading} onClick={onRefresh}>刷新</Button><span>自动刷新</span><Switch checked={autoRefresh} onChange={onAutoRefreshToggle} /><Select value="5s" options={[{ value: '5s', label: '5s' }]} /></div>
      </div>
      <SummaryCardsV2 data={data} />
      <div className="v2-main-grid">
        <PipelinePanelV2 pipelines={data.pipelines} />
        <HedgePoolPanelV2 hedgeGroups={data.hedgeGroups} releasedCount={data.releasedCount} archivedCount={data.archivedCount} />
      </div>
      <LifecycleBarV2 lifecycle={data.lifecycle} />
      <BottomStatsV2 data={data} />
    </div>
  );
}
