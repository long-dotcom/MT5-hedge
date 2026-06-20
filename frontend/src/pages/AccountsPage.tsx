import { useQuery } from '@tanstack/react-query';
import { Card, Space, Table, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { api } from '../api/client';
import { fmtLocalTime, fmtMoney, fmtPct } from '../utils/format';

export function AccountsPage() {
  const query = useQuery({ queryKey: ['accounts'], queryFn: async () => (await api.get('/accounts')).data });
  const columns: ColumnsType<any> = [
    { title: '平台', dataIndex: 'platform' },
    { title: '展示权益', dataIndex: 'equity', render: fmtMoney },
    { title: 'Perp权益', dataIndex: 'perp_equity', render: fmtMoney },
    { title: 'Spot余额', dataIndex: 'spot_balance', render: fmtMoney },
    { title: 'Spot锁定', dataIndex: 'spot_hold', render: fmtMoney },
    { title: '可用保证金', dataIndex: 'free_collateral', render: fmtMoney },
    { title: '可提取', dataIndex: 'withdrawable', render: fmtMoney },
    { title: '占用保证金', dataIndex: 'margin_used', render: fmtMoney },
    { title: '保证金率', dataIndex: 'margin_ratio', render: fmtPct },
    { title: '币种', dataIndex: 'currency' },
    { title: '来源', dataIndex: 'data_source' },
    { title: '更新时间', dataIndex: 'created_at', width: 190, render: fmtLocalTime }
  ];
  return (
    <Space direction="vertical" size={16} className="full-width">
      <Typography.Title level={3}>账户</Typography.Title>
      <Card><Table rowKey="platform" columns={columns} dataSource={query.data || []} loading={query.isLoading} pagination={false} scroll={{ x: 1300 }} /></Card>
    </Space>
  );
}
