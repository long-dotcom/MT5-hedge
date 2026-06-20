import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Button, Card, Space, Table, Tabs, Tag, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { api } from '../api/client';
import { fmtLocalTime } from '../utils/format';

export function LogsPage() {
  const queryClient = useQueryClient();
  const logs = useQuery({ queryKey: ['logs'], queryFn: async () => (await api.get('/logs')).data });
  const alerts = useQuery({ queryKey: ['alerts'], queryFn: async () => (await api.get('/alerts')).data });
  const ack = useMutation({
    mutationFn: async (id: number) => (await api.post(`/alerts/${id}/ack`)).data,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['alerts'] })
  });
  const logColumns: ColumnsType<any> = [
    { title: '等级', dataIndex: 'level', render: (v) => <Tag>{v}</Tag> },
    { title: '分类', dataIndex: 'category' },
    { title: '消息', dataIndex: 'message', ellipsis: true },
    { title: '上下文', dataIndex: 'context', ellipsis: true },
    { title: '时间', dataIndex: 'created_at', width: 190, render: fmtLocalTime }
  ];
  const alertColumns: ColumnsType<any> = [
    { title: '等级', dataIndex: 'level', render: (v) => <Tag color={v === 'critical' ? 'red' : 'gold'}>{v}</Tag> },
    { title: '标题', dataIndex: 'title' },
    { title: '内容', dataIndex: 'message', ellipsis: true },
    { title: '确认', dataIndex: 'acknowledged', render: (v) => (v ? '已确认' : '未确认') },
    { title: '时间', dataIndex: 'created_at', width: 190, render: fmtLocalTime },
    { title: '操作', render: (_, row) => <Button size="small" disabled={row.acknowledged} onClick={() => ack.mutate(row.id)}>确认</Button> }
  ];
  return (
    <Space direction="vertical" size={16} className="full-width">
      <Typography.Title level={3}>日志中心</Typography.Title>
      <Card>
        <Tabs
          items={[
            { key: 'logs', label: '系统日志', children: <Table rowKey="id" columns={logColumns} dataSource={logs.data?.items || []} loading={logs.isLoading} pagination={{ pageSize: 20, total: logs.data?.total || 0 }} /> },
            { key: 'alerts', label: '站内告警', children: <Table rowKey="id" columns={alertColumns} dataSource={alerts.data?.items || []} loading={alerts.isLoading} pagination={{ pageSize: 20, total: alerts.data?.total || 0 }} /> }
          ]}
        />
      </Card>
    </Space>
  );
}
