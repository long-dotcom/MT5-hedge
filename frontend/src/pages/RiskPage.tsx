import { ThunderboltOutlined } from '@ant-design/icons';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Button, Card, Descriptions, Space, Table, Typography, message } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { api } from '../api/client';
import { fmtLocalTime, fmtMoney, fmtPct } from '../utils/format';

export function RiskPage() {
  const queryClient = useQueryClient();
  const [messageApi, contextHolder] = message.useMessage();
  const status = useQuery({ queryKey: ['risk-status'], queryFn: async () => (await api.get('/risk/status')).data });
  const events = useQuery({ queryKey: ['risk-events'], queryFn: async () => (await api.get('/risk/events')).data });
  const stop = useMutation({
    mutationFn: async () => (await api.post('/risk/emergency-stop')).data,
    onSuccess: () => {
      messageApi.success('已触发紧急停止');
      queryClient.invalidateQueries({ queryKey: ['risk-status'] });
    }
  });
  const columns: ColumnsType<any> = [
    { title: '等级', dataIndex: 'level' },
    { title: '规则', dataIndex: 'rule' },
    { title: '品种', dataIndex: 'symbol' },
    { title: '消息', dataIndex: 'message', ellipsis: true },
    { title: '时间', dataIndex: 'created_at', width: 190, render: fmtLocalTime }
  ];
  const risk = status.data || {};
  return (
    <Space direction="vertical" size={16} className="full-width">
      {contextHolder}
      <div className="page-title-row">
        <Typography.Title level={3}>风控中心</Typography.Title>
        <Button danger icon={<ThunderboltOutlined />} loading={stop.isPending} onClick={() => stop.mutate()}>紧急停止</Button>
      </div>
      <Card>
        <Descriptions column={4} size="small">
          <Descriptions.Item label="模式">{risk.mode}</Descriptions.Item>
          <Descriptions.Item label="单笔上限">{fmtMoney(risk.max_order_notional)}</Descriptions.Item>
          <Descriptions.Item label="品种敞口">{fmtMoney(risk.max_symbol_exposure)}</Descriptions.Item>
          <Descriptions.Item label="总杠杆">{risk.max_total_leverage}</Descriptions.Item>
          <Descriptions.Item label="单笔可用资金比例">{fmtPct(risk.max_new_margin_fraction)}</Descriptions.Item>
          <Descriptions.Item label="下单杠杆估算">{risk.new_order_leverage}x</Descriptions.Item>
          <Descriptions.Item label="最低保证金率">{fmtPct(risk.min_margin_ratio)}</Descriptions.Item>
          <Descriptions.Item label="最大滑点">{risk.max_slippage_bps} bps</Descriptions.Item>
          <Descriptions.Item label="行情延迟">{risk.max_market_age_seconds}s</Descriptions.Item>
          <Descriptions.Item label="API 错误">{risk.max_api_errors}</Descriptions.Item>
        </Descriptions>
      </Card>
      <Card title="风控事件">
        <Table rowKey="id" columns={columns} dataSource={events.data?.items || []} loading={events.isLoading} pagination={{ pageSize: 10, total: events.data?.total || 0 }} />
      </Card>
    </Space>
  );
}
