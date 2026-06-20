import { useQuery } from '@tanstack/react-query';
import { Card, Empty, Space, Table, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { api } from '../api/client';
import { fmtMoney, fmtNum } from '../utils/format';

export function PositionsPage() {
  const query = useQuery({ queryKey: ['positions'], queryFn: async () => (await api.get('/positions')).data });
  const columns: ColumnsType<any> = [
    { title: '平台', dataIndex: 'platform' },
    { title: '品种', dataIndex: 'symbol' },
    { title: '方向', dataIndex: 'side' },
    { title: '数量', dataIndex: 'quantity', render: (v) => fmtNum(v, 4) },
    { title: '开仓均价', dataIndex: 'entry_price', render: fmtMoney },
    { title: '当前价', dataIndex: 'mark_price', render: fmtMoney },
    { title: '未实现盈亏', dataIndex: 'unrealized_pnl', render: fmtMoney },
    { title: '强平价', dataIndex: 'liquidation_price', render: fmtMoney }
  ];
  return (
    <Space direction="vertical" size={16} className="full-width">
      <Typography.Title level={3}>仓位</Typography.Title>
      <Card>{query.data?.length ? <Table rowKey="id" columns={columns} dataSource={query.data} pagination={{ pageSize: 10 }} /> : <Empty description="暂无仓位" />}</Card>
    </Space>
  );
}

