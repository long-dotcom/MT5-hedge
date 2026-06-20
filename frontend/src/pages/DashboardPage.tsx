import ReactECharts from 'echarts-for-react';
import { useQuery } from '@tanstack/react-query';
import { Alert, Card, Col, Row, Space, Typography } from 'antd';
import { api } from '../api/client';
import { DataCard } from '../components/DataCard';
import { fmtChartTime, fmtMoney } from '../utils/format';

export function DashboardPage() {
  const summary = useQuery({ queryKey: ['dashboard-summary'], queryFn: async () => (await api.get('/dashboard/summary')).data });
  const curve = useQuery({ queryKey: ['equity-curve'], queryFn: async () => (await api.get('/dashboard/equity-curve')).data });
  const data = summary.data || {};
  const curveData = curve.data || [];

  return (
    <Space direction="vertical" size={16} className="full-width">
      <Typography.Title level={3}>仪表盘</Typography.Title>
      {data.risk_mode === 'emergency_stop' && <Alert type="error" showIcon message="系统处于紧急停止模式" />}
      <Row gutter={[16, 16]}>
        <Col xs={24} md={8} xl={4}><DataCard title="总权益" value={fmtMoney(data.equity)} /></Col>
        <Col xs={24} md={8} xl={4}><DataCard title="今日盈亏" value={fmtMoney(data.today_pnl)} /></Col>
        <Col xs={24} md={8} xl={4}><DataCard title="已实现盈亏" value={fmtMoney(data.realized_pnl)} /></Col>
        <Col xs={24} md={8} xl={4}><DataCard title="未实现盈亏" value={fmtMoney(data.unrealized_pnl)} /></Col>
        <Col xs={24} md={8} xl={4}><DataCard title="对冲组" value={data.open_hedge_groups ?? 0} /></Col>
        <Col xs={24} md={8} xl={4}><DataCard title="未读告警" value={data.unread_alerts ?? 0} /></Col>
      </Row>
      <Card title="权益曲线" className="chart-card">
        <ReactECharts
          style={{ height: 320 }}
          option={{
            tooltip: { trigger: 'axis' },
            xAxis: { type: 'category', data: curveData.map((item: any) => fmtChartTime(item.time)) },
            yAxis: { type: 'value', scale: true },
            series: [{ type: 'line', smooth: true, data: curveData.map((item: any) => item.equity) }]
          }}
        />
      </Card>
    </Space>
  );
}
