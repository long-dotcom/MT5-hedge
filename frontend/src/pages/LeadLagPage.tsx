import { useQuery } from '@tanstack/react-query';
import { Card, Empty, Form, InputNumber, Select, Space, Table, Tag, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import ReactECharts from 'echarts-for-react';
import { useMemo, useState } from 'react';
import { api } from '../api/client';
import { usePageStream } from '../hooks/useLiveStream';
import { fmtAdaptive, fmtChartTime, fmtLocalTime } from '../utils/format';

function fmtMs(value?: number | null) {
  if (value === undefined || value === null || Number.isNaN(value)) return '-';
  return `${value.toFixed(0)} ms`;
}

function fmtPct(value?: number | null) {
  if (value === undefined || value === null || Number.isNaN(value)) return '-';
  return `${(value * 100).toFixed(1)}%`;
}

function fmtMid(value?: number | null) {
  if (value === undefined || value === null || Number.isNaN(value) || value === 0) return '-';
  return value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 6 });
}

function platformLabel(platform?: string) {
  if (platform === 'hyperliquid') return 'HL';
  if (platform === 'mt5') return 'MT5';
  return platform || '-';
}

function DirectionPanel({
  title,
  leader,
  follower,
  data,
}: {
  title: string;
  leader: string;
  follower: string;
  data: any;
}) {
  return (
    <div className="leadlag-direction-card">
      <div className="leadlag-direction-head">
        <strong>{title}</strong>
        <span><Tag color="cyan">{leader}</Tag><i /> <Tag color="geekblue">{follower}</Tag></span>
      </div>
      <div className="leadlag-direction-metrics">
        <div><span>平均滞后</span><strong>{fmtMs(data.avg_lag_ms)}</strong></div>
        <div><span>P90 滞后</span><strong>{fmtMs(data.p90_lag_ms)}</strong></div>
        <div><span>跟随率</span><strong>{fmtPct(data.follow_rate)}</strong></div>
        <div><span>样本数</span><strong>{data.count ?? '-'}</strong></div>
      </div>
    </div>
  );
}

export function LeadLagPage() {
  const symbols = useQuery({ queryKey: ['symbols'], queryFn: async () => (await api.get('/markets/symbols')).data });
  const [symbol, setSymbol] = useState<string>('');
  const [windowSeconds, setWindowSeconds] = useState(300);
  const [thresholdBps, setThresholdBps] = useState(3);
  const [minMove, setMinMove] = useState(0);
  const [maxLagMs, setMaxLagMs] = useState(2000);
  const activeSymbol = symbol || symbols.data?.[0]?.symbol || '';
  const leadLagQueryKey = ['lead-lag', activeSymbol, windowSeconds, thresholdBps, minMove, maxLagMs];
  const streamStatus = usePageStream('lead-lag', {
    params: {
      symbol: activeSymbol,
      window_seconds: windowSeconds,
      threshold_bps: thresholdBps,
      min_move: minMove,
      max_lag_ms: maxLagMs,
    },
    cacheKey: leadLagQueryKey,
    enabled: Boolean(activeSymbol),
  });
  const query = useQuery({
    queryKey: leadLagQueryKey,
    enabled: Boolean(activeSymbol),
    queryFn: async () => (await api.get('/analytics/lead-lag', { params: { symbol: activeSymbol, window_seconds: windowSeconds, threshold_bps: thresholdBps, min_move: minMove, max_lag_ms: maxLagMs } })).data
  });
  const series = query.data?.series || [];
  const summary = query.data?.summary || {};
  const latest = query.data?.latest || {};
  const option = useMemo(() => {
    const times = series.map((row: any) => fmtChartTime(row.time));
    const hlBase = series.find((row: any) => row.hyperliquid_mid)?.hyperliquid_mid || 1;
    const mt5Base = series.find((row: any) => row.mt5_mid)?.mt5_mid || 1;
    return {
      tooltip: { trigger: 'axis' },
      legend: { top: 0, data: ['HL 标准化', 'MT5 标准化', 'Mid差'] },
      grid: { left: 48, right: 52, top: 48, bottom: 64, containLabel: true },
      xAxis: {
        type: 'category',
        data: times,
        name: '时间',
        nameLocation: 'middle',
        nameGap: 30,
        axisLabel: { margin: 8 }
      },
      yAxis: [
        { type: 'value', scale: true, name: '标准化价格', nameLocation: 'middle', nameGap: 44, axisLabel: { formatter: (v: number) => v.toFixed(2), margin: 8 } },
        { type: 'value', scale: true, name: 'Mid 差', nameLocation: 'middle', nameGap: 46, axisLabel: { margin: 8 } }
      ],
      dataZoom: [{ type: 'inside' }, { type: 'slider', height: 20, bottom: 10 }],
      series: [
        { name: 'HL 标准化', type: 'line', showSymbol: false, data: series.map((row: any) => row.hyperliquid_mid ? row.hyperliquid_mid / hlBase * 10000 : null), lineStyle: { color: '#1677ff', width: 2 } },
        { name: 'MT5 标准化', type: 'line', showSymbol: false, data: series.map((row: any) => row.mt5_mid ? row.mt5_mid / mt5Base * 10000 : null), lineStyle: { color: '#52c41a', width: 2 } },
        { name: 'Mid差', type: 'line', yAxisIndex: 1, showSymbol: false, data: series.map((row: any) => row.mid_diff), lineStyle: { color: '#fa8c16', width: 1, type: 'dashed' } }
      ]
    };
  }, [series]);
  const eventColumns: ColumnsType<any> = [
    { title: '时间', dataIndex: 'leader_time', width: 190, render: fmtLocalTime },
    { title: '领先方', dataIndex: 'leader_platform', width: 120, render: (v) => <Tag color={v === 'hyperliquid' ? 'cyan' : 'geekblue'}>{platformLabel(v)}</Tag> },
    { title: '跟随方', dataIndex: 'follower_platform', width: 120, render: (v) => <Tag color={v === 'hyperliquid' ? 'cyan' : 'geekblue'}>{platformLabel(v)}</Tag> },
    { title: '方向', dataIndex: 'direction', width: 80 },
    { title: '跟随', dataIndex: 'followed', width: 80, render: (v) => <Tag color={v ? 'green' : 'gold'}>{v ? '是' : '否'}</Tag> },
    { title: '滞后', dataIndex: 'lag_ms', width: 100, render: fmtMs },
    { title: '领先跳动', dataIndex: 'leader_move', width: 110, render: (v) => fmtAdaptive(v, 2, 6) },
    { title: '领先bps', dataIndex: 'leader_move_bps', width: 110, render: (v) => fmtAdaptive(v, 2, 6) },
    { title: '跟随跳动', dataIndex: 'follower_move', width: 110, render: (v) => fmtAdaptive(v, 2, 6) },
    { title: '期间最大Mid差', dataIndex: 'max_mid_diff', width: 130, render: (v) => fmtAdaptive(v, 2, 6) }
  ];
  const hlToMt5 = summary.hyperliquid_to_mt5 || {};
  const mt5ToHl = summary.mt5_to_hyperliquid || {};
  return (
    <div className="leadlag-page">
      <div className="leadlag-header">
        <div>
          <Typography.Title level={3}>报价时差分析</Typography.Title>
          <Typography.Text type="secondary">观察同一品种两边报价谁先动、另一边多久跟随</Typography.Text>
        </div>
        <Typography.Text type={streamStatus.online ? 'success' : 'secondary'}>{streamStatus.online ? '页面级推送运行中' : '等待页面级推送'}</Typography.Text>
      </div>

      <Card className="leadlag-toolbar">
        <Form layout="inline">
          <Form.Item label="品种">
            <Select value={activeSymbol} onChange={setSymbol} className="leadlag-symbol-select" loading={symbols.isLoading} options={(symbols.data || []).map((row: any) => ({ value: row.symbol, label: row.symbol }))} />
          </Form.Item>
          <Form.Item label="窗口">
            <Select value={windowSeconds} onChange={setWindowSeconds} className="leadlag-window-select" options={[{ value: 60, label: '1分钟' }, { value: 300, label: '5分钟' }, { value: 900, label: '15分钟' }, { value: 1800, label: '30分钟' }]} />
          </Form.Item>
          <Form.Item label="阈值 bps">
            <InputNumber min={0.1} step={0.5} value={thresholdBps} onChange={(v) => setThresholdBps(Number(v || 0))} className="leadlag-number-input" />
          </Form.Item>
          <Form.Item label="最小跳动">
            <InputNumber min={0} step={1} value={minMove} onChange={(v) => setMinMove(Number(v || 0))} className="leadlag-number-input" />
          </Form.Item>
          <Form.Item label="最大跟随">
            <InputNumber min={100} step={100} value={maxLagMs} onChange={(v) => setMaxLagMs(Number(v || 0))} className="leadlag-number-input" addonAfter="ms" />
          </Form.Item>
        </Form>
      </Card>

      <div className="leadlag-main-grid">
        <Card className="leadlag-chart-card">
          <div className="leadlag-card-head">
            <strong>{activeSymbol || '-'}</strong>
            <Space size={8}>
              <Tag color="cyan">HL Mid {fmtMid(latest.hyperliquid?.mid)}</Tag>
              <Tag color="geekblue">MT5 Mid {fmtMid(latest.mt5?.mid)}</Tag>
            </Space>
          </div>
          {series.length ? <ReactECharts option={option} style={{ height: 430 }} /> : <Empty description="暂无报价历史" />}
        </Card>

        <aside className="leadlag-side">
          <Card className="leadlag-latest-card">
            <div className="leadlag-latest-row">
              <span>HL 来源</span>
              <strong>{latest.hyperliquid?.source || '-'}</strong>
            </div>
            <div className="leadlag-latest-row">
              <span>MT5 来源</span>
              <strong>{latest.mt5?.source || '-'}</strong>
            </div>
            <div className="leadlag-latest-row">
              <span>样本点</span>
              <strong>{series.length}</strong>
            </div>
          </Card>
          <DirectionPanel title="HL 领先 MT5" leader="HL" follower="MT5" data={hlToMt5} />
          <DirectionPanel title="MT5 领先 HL" leader="MT5" follower="HL" data={mt5ToHl} />
        </aside>
      </div>

      <Card title="跳动事件" className="leadlag-events-card">
        <Table rowKey={(_, index) => String(index)} columns={eventColumns} dataSource={query.data?.items || []} loading={query.isLoading} scroll={{ x: 1200, y: 260 }} pagination={{ pageSize: 20, size: 'small' }} />
      </Card>
    </div>
  );
}
