import { useQuery } from '@tanstack/react-query';
import { Card, Space, Table, Tabs, Tag, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useState } from 'react';
import { api } from '../api/client';
import { ellipsis, fmtLocalTime, fmtMoney, fmtNum } from '../utils/format';

function statusColor(status?: string) {
  if (status === 'filled') return 'green';
  if (status === 'partially_filled' || status === 'accepted' || status === 'submitted') return 'blue';
  if (status === 'rejected' || status === 'failed' || status === 'canceled') return 'red';
  return 'default';
}

export function ExecutionPage() {
  const [orderPage, setOrderPage] = useState(1);
  const [fillPage, setFillPage] = useState(1);
  const orders = useQuery({ queryKey: ['orders', orderPage], queryFn: async () => (await api.get('/orders', { params: { page: orderPage, page_size: 20 } })).data });
  const fills = useQuery({ queryKey: ['fills', fillPage], queryFn: async () => (await api.get('/fills', { params: { page: fillPage, page_size: 20 } })).data });

  const orderColumns: ColumnsType<any> = [
    { title: 'ID', dataIndex: 'id', width: 70 },
    { title: '组ID', dataIndex: 'hedge_group_id', width: 80 },
    { title: '平台', dataIndex: 'platform', width: 110 },
    { title: '品种', dataIndex: 'symbol', width: 110 },
    { title: '方向', dataIndex: 'side', width: 80 },
    { title: '状态', dataIndex: 'status', width: 130, render: (v) => <Tag color={statusColor(v)}>{v}</Tag> },
    { title: '类型', dataIndex: 'order_type', width: 90 },
    { title: '数量', dataIndex: 'quantity', width: 110, render: (v) => fmtNum(v, 6) },
    { title: '价格', dataIndex: 'price', width: 110, render: fmtMoney },
    { title: 'Post', dataIndex: 'post_only', width: 80, render: (v) => (v ? <Tag color="purple">post</Tag> : '-') },
    { title: 'Reduce', dataIndex: 'reduce_only', width: 90, render: (v) => (v ? <Tag color="orange">reduce</Tag> : '-') },
    { title: 'TTL', dataIndex: 'ttl_seconds', width: 80 },
    { title: '外部单号', dataIndex: 'external_order_id', width: 180, render: (v) => ellipsis(v, 24) },
    { title: '错误', dataIndex: 'error_message', width: 220, ellipsis: true },
    { title: '创建时间', dataIndex: 'created_at', width: 180, render: fmtLocalTime }
  ];

  const fillColumns: ColumnsType<any> = [
    { title: 'ID', dataIndex: 'id', width: 70 },
    { title: '订单ID', dataIndex: 'order_id', width: 90 },
    { title: '平台', dataIndex: 'platform', width: 110 },
    { title: '品种', dataIndex: 'symbol', width: 110 },
    { title: '方向', dataIndex: 'side', width: 80 },
    { title: '数量', dataIndex: 'quantity', width: 120, render: (v) => fmtNum(v, 6) },
    { title: '价格', dataIndex: 'price', width: 120, render: fmtMoney },
    { title: '手续费', dataIndex: 'fee', width: 120, render: fmtMoney },
    { title: '成交时间', dataIndex: 'created_at', width: 180, render: fmtLocalTime }
  ];

  return (
    <Space direction="vertical" size={16} className="full-width">
      <Typography.Title level={3}>执行记录</Typography.Title>
      <Card>
        <Tabs
          items={[
            {
              key: 'orders',
              label: '订单',
              children: (
                <Table
                  rowKey="id"
                  columns={orderColumns}
                  dataSource={orders.data?.items || []}
                  loading={orders.isLoading}
                  scroll={{ x: 1700 }}
                  pagination={{ current: orderPage, pageSize: 20, total: orders.data?.total || 0, onChange: setOrderPage }}
                />
              )
            },
            {
              key: 'fills',
              label: '成交',
              children: (
                <Table
                  rowKey="id"
                  columns={fillColumns}
                  dataSource={fills.data?.items || []}
                  loading={fills.isLoading}
                  scroll={{ x: 980 }}
                  pagination={{ current: fillPage, pageSize: 20, total: fills.data?.total || 0, onChange: setFillPage }}
                />
              )
            }
          ]}
        />
      </Card>
    </Space>
  );
}
