import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Button, Card, Space, Table, Tabs, Tag, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useState } from 'react';
import { api } from '../api/client';
import { usePageStream } from '../hooks/useLiveStream';
import { fmtLocalTime } from '../utils/format';

const PAGE_SIZE = 20;
const cellText = (value: any) => {
  if (value === null || value === undefined) return '';
  return typeof value === 'string' ? value : JSON.stringify(value);
};

export function LogsPage() {
  const queryClient = useQueryClient();
  const [logPage, setLogPage] = useState(1);
  const [alertPage, setAlertPage] = useState(1);
  const streamStatus = usePageStream('logs', { page: logPage, alertPage, pageSize: PAGE_SIZE });
  const logs = useQuery({ queryKey: ['logs', logPage], queryFn: async () => (await api.get('/logs', { params: { page: logPage, page_size: PAGE_SIZE } })).data });
  const alerts = useQuery({ queryKey: ['alerts', alertPage], queryFn: async () => (await api.get('/alerts', { params: { page: alertPage, page_size: PAGE_SIZE } })).data });
  const ack = useMutation({
    mutationFn: async (id: number) => (await api.post(`/alerts/${id}/ack`)).data,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['alerts'] })
  });
  const logColumns: ColumnsType<any> = [
    { title: '等级', dataIndex: 'level', width: 92, render: (v) => <Tag>{v}</Tag> },
    { title: '分类', dataIndex: 'category', width: 140 },
    { title: '消息', dataIndex: 'message', width: 420, ellipsis: true, render: (v) => <Typography.Text className="table-cell-ellipsis" title={cellText(v)}>{cellText(v)}</Typography.Text> },
    { title: '上下文', dataIndex: 'context', width: 360, ellipsis: true, render: (v) => <Typography.Text className="table-cell-ellipsis" title={cellText(v)}>{cellText(v)}</Typography.Text> },
    { title: '时间', dataIndex: 'created_at', width: 190, render: fmtLocalTime }
  ];
  const alertColumns: ColumnsType<any> = [
    { title: '等级', dataIndex: 'level', width: 92, render: (v) => <Tag color={v === 'critical' ? 'red' : 'gold'}>{v}</Tag> },
    { title: '标题', dataIndex: 'title', width: 260, ellipsis: true, render: (v) => <Typography.Text className="table-cell-ellipsis" title={cellText(v)}>{cellText(v)}</Typography.Text> },
    { title: '内容', dataIndex: 'message', width: 560, ellipsis: true, render: (v) => <Typography.Text className="table-cell-ellipsis" title={cellText(v)}>{cellText(v)}</Typography.Text> },
    { title: '确认', dataIndex: 'acknowledged', width: 90, render: (v) => (v ? '已确认' : '未确认') },
    { title: '时间', dataIndex: 'created_at', width: 190, render: fmtLocalTime },
    { title: '操作', width: 90, render: (_, row) => <Button size="small" disabled={row.acknowledged} onClick={() => ack.mutate(row.id)}>确认</Button> }
  ];
  return (
    <Space direction="vertical" size={16} className="full-width">
      <Space className="full-width" align="center" style={{ justifyContent: 'space-between' }}>
        <Typography.Title level={3} style={{ margin: 0 }}>日志中心</Typography.Title>
        <Typography.Text type={streamStatus.online ? 'success' : 'secondary'}>{streamStatus.online ? '页面级推送运行中' : '等待页面级推送'}</Typography.Text>
      </Space>
      <Card>
        <Tabs
          items={[
            {
              key: 'logs',
              label: '系统日志',
              children: (
                <Table
                  rowKey="id"
                  columns={logColumns}
                  dataSource={logs.data?.items || []}
                  loading={logs.isLoading}
                  className="logs-table"
                  scroll={{ x: 1202, y: 'calc(100vh - 340px)' }}
                  tableLayout="fixed"
                  pagination={{ current: logPage, pageSize: PAGE_SIZE, total: logs.data?.total || 0, onChange: setLogPage }}
                />
              )
            },
            {
              key: 'alerts',
              label: '站内告警',
              children: (
                <Table
                  rowKey="id"
                  columns={alertColumns}
                  dataSource={alerts.data?.items || []}
                  loading={alerts.isLoading}
                  className="logs-table"
                  scroll={{ x: 1302, y: 'calc(100vh - 340px)' }}
                  tableLayout="fixed"
                  pagination={{ current: alertPage, pageSize: PAGE_SIZE, total: alerts.data?.total || 0, onChange: setAlertPage }}
                />
              )
            }
          ]}
        />
      </Card>
    </Space>
  );
}
