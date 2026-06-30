import { useQuery } from '@tanstack/react-query';
import { Card, Col, Empty, Row, Select, Space, Statistic, Divider } from 'antd';
import ReactECharts from 'echarts-for-react';
import { useMemo, useState } from 'react';
import { api } from '../api/client';
import { fmtAdaptive, fmtChartTime, fmtNum } from '../utils/format';

const ranges = [
  { value: '15m', label: '15分钟' },
  { value: '1h', label: '1小时' },
  { value: '4h', label: '4小时' },
  { value: '24h', label: '24小时' },
  { value: '7d', label: '7天' }
];

function fmtChartValue(value: unknown) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return '-';
  return fmtAdaptive(numeric, 2, 6);
}

export function VenueSpreadsPage() {
  const symbols = useQuery({ queryKey: ['symbols'], queryFn: async () => (await api.get('/markets/symbols')).data });
  const defaultSymbol = symbols.data?.[0]?.symbol || 'BTC';
  const [symbol, setSymbol] = useState(defaultSymbol);
  const [range, setRange] = useState('1h');

  const activeSymbol = symbol || defaultSymbol;
  const query = useQuery({
    queryKey: ['venue-spreads', activeSymbol, range],
    enabled: Boolean(activeSymbol),
    queryFn: async () => (await api.get('/analytics/venue-spreads', { params: { symbol: activeSymbol, range } })).data,
    refetchInterval: 5000,
  });

  const summary = query.data?.summary;
  const series = query.data?.series || [];

  const option = useMemo(() => {
    const times = series.map((row: any) => fmtChartTime(row.time));
    const hlData = series.map((row: any) => row.hl_avg);
    const mt5Data = series.map((row: any) => row.mt5_avg);

    const hlMean = summary?.hl?.mean ?? 0;
    const mt5Mean = summary?.mt5?.mean ?? 0;
    const hlMeanArr = series.map(() => hlMean);
    const mt5MeanArr = series.map(() => mt5Mean);

    const hlStd = summary?.hl?.std ?? 0;
    const mt5Std = summary?.mt5?.std ?? 0;
    const hlUpper3 = hlMean + hlStd * 3;
    const mt5Upper3 = mt5Mean + mt5Std * 3;
    const hlUpper3Arr = series.map(() => hlUpper3);
    const mt5Upper3Arr = series.map(() => mt5Upper3);

    // Build markArea data for anomaly regions (value > mean + 3σ)
    const hlAnomalyAreas: any[] = [];
    const mt5AnomalyAreas: any[] = [];
    let hlStart: any = null;
    let mt5Start: any = null;
    for (let i = 0; i < series.length; i++) {
      const hlExceed = series[i].hl_avg > hlUpper3;
      const mt5Exceed = series[i].mt5_avg > mt5Upper3;
      if (hlExceed && hlStart === null) hlStart = i;
      if (!hlExceed && hlStart !== null) {
        hlAnomalyAreas.push([{ xAxis: hlStart }, { xAxis: i - 1 }]);
        hlStart = null;
      }
      if (mt5Exceed && mt5Start === null) mt5Start = i;
      if (!mt5Exceed && mt5Start !== null) {
        mt5AnomalyAreas.push([{ xAxis: mt5Start }, { xAxis: i - 1 }]);
        mt5Start = null;
      }
    }
    if (hlStart !== null) hlAnomalyAreas.push([{ xAxis: hlStart }, { xAxis: series.length - 1 }]);
    if (mt5Start !== null) mt5AnomalyAreas.push([{ xAxis: mt5Start }, { xAxis: series.length - 1 }]);

    return {
      animation: false,
      tooltip: {
        trigger: 'axis',
        valueFormatter: (value: unknown) => fmtChartValue(value)
      },
      legend: { top: 0, data: ['HL 点差', 'MT5 点差', 'HL 均值', 'MT5 均值', '+3σ'] },
      grid: { left: 48, right: 28, top: 48, bottom: 42 },
      xAxis: { type: 'category', data: times, boundaryGap: false },
      yAxis: { type: 'value', scale: true, axisLabel: { formatter: (value: number) => fmtChartValue(value) } },
      dataZoom: [{ type: 'inside' }, { type: 'slider', height: 18, bottom: 8 }],
      series: [
        {
          name: 'HL 点差', type: 'line', data: hlData, showSymbol: false,
          lineStyle: { width: 2, color: '#1677ff' },
          itemStyle: { color: '#1677ff' },
          markArea: hlAnomalyAreas.length ? { silent: true, data: hlAnomalyAreas.map((pair) => pair.map((p: any) => ({ ...p, itemStyle: { color: 'rgba(255,77,79,0.08)' } }))) } : undefined,
        },
        {
          name: 'MT5 点差', type: 'line', data: mt5Data, showSymbol: false,
          lineStyle: { width: 2, color: '#52c41a' },
          itemStyle: { color: '#52c41a' },
          markArea: mt5AnomalyAreas.length ? { silent: true, data: mt5AnomalyAreas.map((pair) => pair.map((p: any) => ({ ...p, itemStyle: { color: 'rgba(255,77,79,0.08)' } }))) } : undefined,
        },
        { name: 'HL 均值', type: 'line', data: hlMeanArr, showSymbol: false, lineStyle: { width: 1.5, type: 'dashed', color: '#1677ff' } },
        { name: 'MT5 均值', type: 'line', data: mt5MeanArr, showSymbol: false, lineStyle: { width: 1.5, type: 'dashed', color: '#52c41a' } },
        { name: '+3σ', type: 'line', data: hlUpper3Arr, showSymbol: false, lineStyle: { width: 1, type: 'dotted', color: '#ff4d4f' } },
        { name: '+3σ(mt5)', type: 'line', data: mt5Upper3Arr, showSymbol: false, lineStyle: { width: 1, type: 'dotted', color: '#ff4d4f' }, tooltip: { show: false } },
      ]
    };
  }, [series, summary]);

  const symbolOptions = (symbols.data || []).map((row: any) => ({ value: row.symbol, label: row.symbol }));

  const renderStats = (stats: any, label: string) => (
    <Space direction="vertical" size={4} className="full-width">
      <div style={{ fontWeight: 600, fontSize: 14, marginBottom: 4 }}>{label}</div>
      <div className="analytics-line"><span>样本数</span><strong>{stats?.sample_count ?? 0}</strong></div>
      <div className="analytics-line"><span>均值 / 标准差</span><strong>{fmtChartValue(stats?.mean)} / {fmtChartValue(stats?.std)}</strong></div>
      <div className="analytics-line"><span>中位数</span><strong>{fmtChartValue(stats?.median)}</strong></div>
      <div className="analytics-line"><span>最小值 / 最大值</span><strong>{fmtChartValue(stats?.min)} / {fmtChartValue(stats?.max)}</strong></div>
      <div className="analytics-line"><span>P95</span><strong>{fmtChartValue(stats?.p95)}</strong></div>
      <div className="analytics-line"><span>变异系数 (CV)</span><strong>{stats?.cv != null ? fmtNum(stats.cv, 4) : '-'}</strong></div>
      <div className="analytics-line"><span>异常占比 (&gt;3σ)</span><strong>{stats?.anomaly_pct != null ? `${fmtNum(stats.anomaly_pct, 2)}%` : '-'}</strong></div>
    </Space>
  );

  return (
    <Space direction="vertical" size={16} className="full-width">
      <Card>
        <Space wrap>
          <Select className="analytics-control" value={activeSymbol} options={symbolOptions} loading={symbols.isLoading} onChange={setSymbol} />
          <Select className="analytics-control" value={range} options={ranges} onChange={setRange} />
        </Space>
      </Card>

      <Row gutter={[16, 16]}>
        <Col xs={24} sm={12} lg={6}>
          <Card className="metric-card">
            <Statistic title="HL 当前点差" value={summary?.hl?.current || 0} formatter={(value) => fmtChartValue(value)} />
            <div style={{ marginTop: 4, color: '#888', fontSize: 12 }}>均值: {fmtChartValue(summary?.hl?.mean)}</div>
          </Card>
        </Col>
        <Col xs={24} sm={12} lg={6}>
          <Card className="metric-card">
            <Statistic title="MT5 当前点差" value={summary?.mt5?.current || 0} formatter={(value) => fmtChartValue(value)} />
            <div style={{ marginTop: 4, color: '#888', fontSize: 12 }}>均值: {fmtChartValue(summary?.mt5?.mean)}</div>
          </Card>
        </Col>
        <Col xs={24} sm={12} lg={6}>
          <Card className="metric-card">
            <Statistic title="HL 变异系数 (CV)" value={summary?.hl?.cv || 0} formatter={(value) => fmtNum(value as number, 4)} />
          </Card>
        </Col>
        <Col xs={24} sm={12} lg={6}>
          <Card className="metric-card">
            <Statistic title="MT5 变异系数 (CV)" value={summary?.mt5?.cv || 0} formatter={(value) => fmtNum(value as number, 4)} />
          </Card>
        </Col>
      </Row>

      <Row gutter={[16, 16]}>
        <Col xs={24} lg={16}>
          <Card className="analytics-chart-card">
            {series.length ? <ReactECharts option={option} style={{ height: 420 }} /> : <Empty description="暂无点差数据" />}
          </Card>
        </Col>
        <Col xs={24} lg={8}>
          <Card className="analytics-side-card">
            {renderStats(summary?.hl, 'HL 统计')}
            <Divider style={{ margin: '12px 0' }} />
            {renderStats(summary?.mt5, 'MT5 统计')}
          </Card>
        </Col>
      </Row>
    </Space>
  );
}
