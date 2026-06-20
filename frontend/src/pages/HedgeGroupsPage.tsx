import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Button, Card, Space, Table, Tag, Typography, message } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useState } from 'react';
import { api } from '../api/client';
import { fmtMoney, fmtNum } from '../utils/format';

export function HedgeGroupsPage() {
  const [page, setPage] = useState(1);
  const queryClient = useQueryClient();
  const [messageApi, contextHolder] = message.useMessage();
  const query = useQuery({ queryKey: ['hedge-groups', page], queryFn: async () => (await api.get('/hedge-groups', { params: { page, page_size: 20 } })).data });
  const close = useMutation({
    mutationFn: async (id: number) => (await api.post(`/hedge-groups/${id}/close`, { reason: 'manual close from ui' })).data,
    onSuccess: () => {
      messageApi.success('对冲组已平仓');
      queryClient.invalidateQueries({ queryKey: ['hedge-groups'] });
    },
    onError: (err: any) => messageApi.error(err.response?.data?.detail || '平仓失败')
  });
  const reconcile = useMutation({
    mutationFn: async () => (await api.post('/execution/reconcile')).data,
    onSuccess: (data) => {
      messageApi.success(`执行状态已同步，变更 ${data.changed || 0} 项`);
      queryClient.invalidateQueries({ queryKey: ['hedge-groups'] });
      queryClient.invalidateQueries({ queryKey: ['alerts'] });
      queryClient.invalidateQueries({ queryKey: ['positions'] });
      queryClient.invalidateQueries({ queryKey: ['orders'] });
      queryClient.invalidateQueries({ queryKey: ['fills'] });
    },
    onError: (err: any) => messageApi.error(err.response?.data?.detail || '同步失败')
  });
  const columns: ColumnsType<any> = [
    { title: 'ID', dataIndex: 'id', width: 70 },
    { title: '品种', dataIndex: 'symbol' },
    { title: '方向', dataIndex: 'direction', ellipsis: true, width: 220 },
    { title: '状态', dataIndex: 'status', render: (v) => <Tag color={v === 'open' ? 'green' : v === 'manual_intervention' ? 'red' : 'blue'}>{v}</Tag> },
    { title: '模式', dataIndex: 'execution_mode' },
    { title: '名义价值', dataIndex: 'notional', render: fmtMoney },
    { title: '数量', dataIndex: 'quantity', render: (v) => fmtNum(v, 4) },
    { title: '开仓价差', dataIndex: 'entry_spread', render: fmtMoney },
    { title: '入场线', dataIndex: 'entry_threshold', render: fmtMoney },
    { title: '退出线', dataIndex: 'exit_target', render: fmtMoney },
    { title: '成本', dataIndex: 'open_cost', render: fmtMoney },
    { title: '已实现', dataIndex: 'realized_pnl', render: fmtMoney },
    { title: '未实现', dataIndex: 'unrealized_pnl', render: fmtMoney },
    { title: '平仓原因', dataIndex: 'close_reason', ellipsis: true, width: 180 },
    { title: '操作', fixed: 'right', width: 100, render: (_, row) => <Button size="small" disabled={!['open', 'open_partial', 'manual_intervention'].includes(row.status)} onClick={() => close.mutate(row.id)}>平仓</Button> }
  ];
  return (
    <Space direction="vertical" size={16} className="full-width">
      {contextHolder}
      <Space className="full-width" align="center" style={{ justifyContent: 'space-between' }}>
        <Typography.Title level={3} style={{ margin: 0 }}>对冲组</Typography.Title>
        <Button loading={reconcile.isPending} onClick={() => reconcile.mutate()}>同步执行状态</Button>
      </Space>
      <Card>
        <Table rowKey="id" columns={columns} dataSource={query.data?.items || []} loading={query.isLoading} scroll={{ x: 1200 }} pagination={{ current: page, pageSize: 20, total: query.data?.total || 0, onChange: setPage }} />
      </Card>
    </Space>
  );
}
