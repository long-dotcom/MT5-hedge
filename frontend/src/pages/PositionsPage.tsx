import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Button, Card, Empty, Space, Table, Tag, Typography, message } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { api } from '../api/client';
import { fmtAdaptive, fmtMoney, fmtPnlColor, fmtPnlSigned } from '../utils/format';

function platformTag(platform?: string) {
  if (platform === 'hyperliquid') return <Tag color="cyan">HL</Tag>;
  if (platform === 'mt5') return <Tag color="geekblue">MT5</Tag>;
  return <Tag>{platform || '-'}</Tag>;
}

function sideTag(side?: string) {
  if (side === 'buy') return <Tag color="green">买</Tag>;
  if (side === 'sell') return <Tag color="red">卖</Tag>;
  return <Tag>{side || '-'}</Tag>;
}

export function PositionsPage() {
  const queryClient = useQueryClient();
  const [messageApi, contextHolder] = message.useMessage();
  const query = useQuery({ queryKey: ['positions'], queryFn: async () => (await api.get('/positions')).data });
  const adopt = useMutation({
    mutationFn: async (id: number) => (await api.post(`/positions/${id}/adopt`, { reason: 'adopt from ui' })).data,
    onSuccess: () => {
      messageApi.success('外部仓位已接管为人工介入对冲组');
      queryClient.invalidateQueries({ queryKey: ['positions'] });
      queryClient.invalidateQueries({ queryKey: ['hedge-groups'] });
      queryClient.invalidateQueries({ queryKey: ['settings-live-readiness'] });
    },
    onError: (err: any) => messageApi.error(err.response?.data?.detail || '接管失败')
  });
  const columns: ColumnsType<any> = [
    { title: '平台', dataIndex: 'platform', width: 82, render: platformTag },
    { title: '品种', dataIndex: 'symbol' },
    { title: '方向', dataIndex: 'side', width: 76, render: sideTag },
    { title: '数量', dataIndex: 'quantity', render: (v) => fmtAdaptive(v, 2, 6) },
    { title: '开仓均价', dataIndex: 'entry_price', render: fmtMoney },
    { title: '当前价', dataIndex: 'mark_price', render: fmtMoney },
    { title: '未实现盈亏', dataIndex: 'unrealized_pnl', align: 'right', render: (v) => <span style={{ color: fmtPnlColor(v) }}>{fmtPnlSigned(v)}</span> },
    { title: '强平价', dataIndex: 'liquidation_price', render: (v) => (v == null ? '-' : fmtMoney(v)) },
    { title: '操作', fixed: 'right', width: 100, render: (_, row) => <Button size="small" loading={adopt.isPending} disabled={!!row.hedge_group_id} onClick={() => adopt.mutate(row.id)}>接管</Button> }
  ];
  return (
    <Space direction="vertical" size={16} className="full-width">
      {contextHolder}
      <Typography.Title level={3}>仓位</Typography.Title>
      <Card>{query.data?.length ? <Table rowKey="id" columns={columns} dataSource={query.data} scroll={{ x: 900 }} pagination={{ pageSize: 10 }} /> : <Empty description="暂无仓位" />}</Card>
    </Space>
  );
}
