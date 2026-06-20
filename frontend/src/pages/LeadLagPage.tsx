import { useQuery } from '@tanstack/react-query';
import { Card, Col, Empty, Form, InputNumber, Row, Select, Space, Statistic, Table, Tag, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import ReactECharts from 'echarts-for-react';
import { useMemo, useState } from 'react';
import { api } from '../api/client';
import { fmtChartTime, fmtLocalTime, fmtNum } from '../utils/format';

function fmtMs(value?: number | null) {
  if (value === undefined || value === null || Number.isNaN(value)) return '-';
  return `${value.toFixed(0)} ms`;
}

function fmtPct(value?: number | null) {
  if (value === undefined || value === null || Number.isNaN(value)) return '-';
  return `${(value * 100).toFixed(1)}%`;
}

export function LeadLagPage() {
  const [symbol, setSymbol] = useState('JP225');
  const [windowSeconds, setWindowSeconds] = useState(300);
  const [thresholdBps, setThresholdBps] = useState(3);
  const [minMove, setMinMove] = useState(0);
  const [maxLagMs, setMaxLagMs] = useState(2000);
  const query = useQuery({
    queryKey: ['lead-lag', symbol, windowSeconds, thresholdBps, minMove, maxLagMs],
    queryFn: async () => (await api.get('/analytics/lead-lag', { params: { symbol, window_seconds: windowSeconds, threshold_bps: thresholdBps, min_move: minMove, max_lag_ms: maxLagMs } })).data,
    refetchInterval: 2000
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
      grid: { left: 48, right: 42, top: 48, bottom: 60 },
      xAxis: { type: 'category', data: times },
      yAxis: [
        { type: 'value', scale: true },
        { type: 'value', scale: true }
      ],
      dataZoom: [{ type: 'inside' }, { type: 'slider', height: 24 }],
      series: [
        { name: 'HL 标准化', type: 'line', showSymbol: false, data: series.map((row: any) => row.hyperliquid_mid ? row.hyperliquid_mid / hlBase * 10000 : null), lineStyle: { color: '#1677ff', width: 2 } },
        { name: 'MT5 标准化', type: 'line', showSymbol: false, data: series.map((row: any) => row.mt5_mid ? row.mt5_mid / mt5Base * 10000 : null), lineStyle: { color: '#52c41a', width: 2 } },
        { name: 'Mid差', type: 'line', yAxisIndex: 1, showSymbol: false, data: series.map((row: any) => row.mid_diff), lineStyle: { color: '#fa8c16', width: 1, type: 'dashed' } }
      ]
    };
  }, [series]);
  const eventColumns: ColumnsType<any> = [
    { title: '时间', dataIndex: 'leader_time', width: 190, render: fmtLocalTime },
    { title: '领先方', dataIndex: 'leader_platform', width: 120, render: (v) => <Tag color={v === 'hyperliquid' ? 'blue' : 'green'}>{v}</Tag> },
    { title: '跟随方', dataIndex: 'follower_platform', width: 120 },
    { title: '方向', dataIndex: 'direction', width: 80 },
    { title: '跟随', dataIndex: 'followed', width: 80, render: (v) => <Tag color={v ? 'green' : 'gold'}>{v ? '是' : '否'}</Tag> },
    { title: '滞后', dataIndex: 'lag_ms', width: 100, render: fmtMs },
    { title: '领先跳动', dataIndex: 'leader_move', width: 110, render: (v) => fmtNum(v, 2) },
    { title: '领先bps', dataIndex: 'leader_move_bps', width: 110, render: (v) => fmtNum(v, 2) },
    { title: '跟随跳动', dataIndex: 'follower_move', width: 110, render: (v) => fmtNum(v, 2) },
    { title: '期间最大Mid差', dataIndex: 'max_mid_diff', width: 130, render: (v) => fmtNum(v, 2) }
  ];
  const hlToMt5 = summary.hyperliquid_to_mt5 || {};
  const mt5ToHl = summary.mt5_to_hyperliquid || {};
  return (
    <Space direction="vertical" size={16} className="full-width">
      <div>
        <Typography.Title level={3}>报价领先滞后</Typography.Title>
        <Typography.Text type="secondary">观察同一品种两边报价谁先动、另一边多久跟随，以及滞后期间价差是否可交易</Typography.Text>
      </div>
      <Card>
        <Form layout="inline">
          <Form.Item label="品种">
            <Select value={symbol} onChange={setSymbol} style={{ width: 160 }} options={['JP225', 'SP500', 'BTC', 'ETH', 'OIL', 'SPCX'].map((value) => ({ value }))} />
          </Form.Item>
          <Form.Item label="窗口">
            <Select value={windowSeconds} onChange={setWindowSeconds} style={{ width: 120 }} options={[{ value: 60, label: '1分钟' }, { value: 300, label: '5分钟' }, { value: 900, label: '15分钟' }, { value: 1800, label: '30分钟' }]} />
          </Form.Item>
          <Form.Item label="跳动阈值bps">
            <InputNumber min={0.1} step={0.5} value={thresholdBps} onChange={(v) => setThresholdBps(Number(v || 0))} />
          </Form.Item>
          <Form.Item label="最小跳动">
            <InputNumber min={0} step={1} value={minMove} onChange={(v) => setMinMove(Number(v || 0))} />
          </Form.Item>
          <Form.Item label="最大跟随毫秒">
            <InputNumber min={100} step={100} value={maxLagMs} onChange={(v) => setMaxLagMs(Number(v || 0))} />
          </Form.Item>
        </Form>
      </Card>
      <Row gutter={[16, 16]}>
        <Col xs={24} lg={6}><Card><Statistic title="HL 最新Mid" value={latest.hyperliquid?.mid || 0} precision={2} suffix={latest.hyperliquid?.source ? ` ${latest.hyperliquid.source}` : ''} /></Card></Col>
        <Col xs={24} lg={6}><Card><Statistic title="MT5 最新Mid" value={latest.mt5?.mid || 0} precision={2} suffix={latest.mt5?.source ? ` ${latest.mt5.source}` : ''} /></Card></Col>
        <Col xs={24} lg={6}><Card><Statistic title="HL->MT5 平均滞后" value={fmtMs(hlToMt5.avg_lag_ms)} /></Card></Col>
        <Col xs={24} lg={6}><Card><Statistic title="MT5->HL 平均滞后" value={fmtMs(mt5ToHl.avg_lag_ms)} /></Card></Col>
      </Row>
      <Row gutter={[16, 16]}>
        <Col xs={24} lg={6}><Card><Statistic title="HL->MT5 跟随率" value={fmtPct(hlToMt5.follow_rate)} /></Card></Col>
        <Col xs={24} lg={6}><Card><Statistic title="HL->MT5 P90滞后" value={fmtMs(hlToMt5.p90_lag_ms)} /></Card></Col>
        <Col xs={24} lg={6}><Card><Statistic title="MT5->HL 跟随率" value={fmtPct(mt5ToHl.follow_rate)} /></Card></Col>
        <Col xs={24} lg={6}><Card><Statistic title="MT5->HL P90滞后" value={fmtMs(mt5ToHl.p90_lag_ms)} /></Card></Col>
      </Row>
      <Card>
        {series.length ? <ReactECharts option={option} style={{ height: 420 }} /> : <Empty description="暂无报价历史" />}
      </Card>
      <Card title="跳动事件">
        <Table rowKey={(_, index) => String(index)} columns={eventColumns} dataSource={query.data?.items || []} loading={query.isLoading} scroll={{ x: 1200 }} pagination={{ pageSize: 20 }} />
      </Card>
    </Space>
  );
}
