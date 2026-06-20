import { PlayCircleOutlined } from '@ant-design/icons';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Button, Card, Space, Table, Tag, Typography, message } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useState } from 'react';
import { api } from '../api/client';
import { fmtAdaptive, fmtCompact, fmtMoney } from '../utils/format';

export function OpportunitiesPage() {
  const [page, setPage] = useState(1);
  const queryClient = useQueryClient();
  const [messageApi, contextHolder] = message.useMessage();
  const query = useQuery({ queryKey: ['opportunities', page], queryFn: async () => (await api.get('/opportunities', { params: { page, page_size: 20 } })).data });
  const currencyText = (row: any) => {
    const currency = row.notional_currency || 'USD';
    const fx = Number(row.fx_rate_to_usd || 1);
    if (currency === 'USD' && Math.abs(fx - 1) < 0.000001) return '-';
    return `${currency} ${fmtCompact(fx, 6)}`;
  };
  const execute = useMutation({
    mutationFn: async (id: number) => (await api.post(`/opportunities/${id}/execute`)).data,
    onSuccess: () => {
      messageApi.success('已创建对冲组');
      queryClient.invalidateQueries({ queryKey: ['opportunities'] });
      queryClient.invalidateQueries({ queryKey: ['hedge-groups'] });
    },
    onError: (err: any) => messageApi.error(err.response?.data?.detail || '执行失败')
  });
  const columns: ColumnsType<any> = [
    { title: 'ID', dataIndex: 'id', width: 68 },
    { title: '品种', dataIndex: 'symbol', width: 80 },
    { title: '方向', dataIndex: 'direction', ellipsis: true, width: 205 },
    { title: '名义USD', dataIndex: 'notional', width: 96, align: 'right', render: fmtMoney },
    { title: 'MT5量', dataIndex: 'mt5_quantity', width: 86, align: 'right', render: (v) => fmtCompact(v, 4) },
    { title: 'HL量', dataIndex: 'hyperliquid_quantity', width: 96, align: 'right', render: (v) => fmtCompact(v, 6) },
    { title: '币种/汇率', width: 96, render: (_, row) => currencyText(row) },
    { title: '毛差/份', dataIndex: 'gross_spread', width: 92, align: 'right', render: (v) => fmtAdaptive(v) },
    { title: '成本/份', dataIndex: 'unit_cost', width: 92, align: 'right', render: (v) => fmtAdaptive(v) },
    { title: '净利/份', dataIndex: 'unit_net_profit', width: 92, align: 'right', render: (v) => fmtAdaptive(v) },
    { title: '入场线', dataIndex: 'entry_threshold', width: 92, align: 'right', render: (v) => fmtAdaptive(v) },
    { title: '退出线', dataIndex: 'exit_target', width: 92, align: 'right', render: (v) => fmtAdaptive(v) },
    { title: '状态', dataIndex: 'status', width: 110, render: (v) => <Tag color={v === 'executable' ? 'green' : v === 'executed' ? 'blue' : 'gold'}>{v}</Tag> },
    { title: '操作', fixed: 'right', width: 110, render: (_, row) => <Button icon={<PlayCircleOutlined />} size="small" disabled={row.status !== 'executable'} loading={execute.isPending} onClick={() => execute.mutate(row.id)}>执行</Button> }
  ];
  return (
    <Space direction="vertical" size={16} className="full-width">
      {contextHolder}
      <Typography.Title level={3}>候选机会</Typography.Title>
      <Card>
        <Table rowKey="id" columns={columns} dataSource={query.data?.items || []} loading={query.isLoading} scroll={{ x: 1280 }} pagination={{ current: page, pageSize: 20, total: query.data?.total || 0, onChange: setPage }} />
      </Card>
    </Space>
  );
}
