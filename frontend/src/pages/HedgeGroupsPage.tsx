import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Button, Card, Descriptions, Space, Table, Tag, message } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useState } from 'react';
import { api } from '../api/client';
import { EllipsisCell } from '../components/EllipsisCell';
import { useHeaderStreamStatus } from '../components/HeaderStreamStatus';
import { usePageStream } from '../hooks/useLiveStream';
import { executionModeLabel, fmtAdaptive, fmtMoney, fmtSpread } from '../utils/format';
import { tableScrollAutoY } from '../utils/tableScroll';

function statusTag(status: string) {
  const map: Record<string, { label: string; color: string }> = {
    open: { label: '持仓', color: 'green' },
    open_partial: { label: '部分', color: 'gold' },
    closed: { label: '已平', color: 'blue' },
    opening: { label: '开仓中', color: 'processing' },
    closing: { label: '平仓中', color: 'processing' },
    manual_intervention: { label: '人工', color: 'red' },
    failed: { label: '失败', color: 'red' }
  };
  const item = map[status] || { label: status || '-', color: 'default' };
  return <Tag color={item.color}>{item.label}</Tag>;
}

function directionTags(direction: string) {
  if (direction === 'long_hyperliquid_short_mt5') {
    return (
      <Space size={4}>
        <Tag color="green">HL 多</Tag>
        <Tag color="red">MT5 空</Tag>
      </Space>
    );
  }
  if (direction === 'long_mt5_short_hyperliquid') {
    return (
      <Space size={4}>
        <Tag color="green">MT5 多</Tag>
        <Tag color="red">HL 空</Tag>
      </Space>
    );
  }
  return <Tag>{direction || '-'}</Tag>;
}

function fmtCarryCost(value?: number) {
  if (value === undefined || value === null || Number.isNaN(Number(value))) return '-';
  return fmtMoney(-Number(value));
}

function hasTriggerPrices(row: any) {
  return ['trigger_hyperliquid_bid', 'trigger_hyperliquid_ask', 'trigger_mt5_bid', 'trigger_mt5_ask'].some((key) => Number(row[key] || 0) !== 0);
}

function detailItems(row: any) {
  return [
    { key: 'mt5_quantity', label: 'MT5 数量', children: fmtAdaptive(row.mt5_quantity, 2, 6) },
    { key: 'hyperliquid_quantity', label: 'HL 数量', children: fmtAdaptive(row.hyperliquid_quantity, 4, 8) },
    { key: 'trigger_spread', label: '触发价差', children: fmtSpread(row.trigger_spread) },
    { key: 'trigger_hl_bid', label: '触发 HL Bid', children: hasTriggerPrices(row) ? fmtAdaptive(row.trigger_hyperliquid_bid, 2, 8) : '-' },
    { key: 'trigger_hl_ask', label: '触发 HL Ask', children: hasTriggerPrices(row) ? fmtAdaptive(row.trigger_hyperliquid_ask, 2, 8) : '-' },
    { key: 'trigger_mt5_bid', label: '触发 MT5 Bid', children: hasTriggerPrices(row) ? fmtAdaptive(row.trigger_mt5_bid, 2, 8) : '-' },
    { key: 'trigger_mt5_ask', label: '触发 MT5 Ask', children: hasTriggerPrices(row) ? fmtAdaptive(row.trigger_mt5_ask, 2, 8) : '-' },
    { key: 'entry_spread', label: '真实开仓价差', children: fmtSpread(row.entry_spread) },
    { key: 'current_entry_spread', label: '当前重新入场价差', children: row.current_entry_spread == null ? '-' : fmtSpread(row.current_entry_spread) },
    { key: 'current_close_spread', label: '当前平仓价差', children: row.current_close_spread == null ? '-' : fmtSpread(row.current_close_spread) },
    { key: 'quote_time_diff_ms', label: '报价时间差', children: row.quote_time_diff_ms == null ? '-' : `${Math.round(row.quote_time_diff_ms)}ms` },
    { key: 'quote_age_ms', label: '报价年龄', children: row.quote_age_ms == null ? '-' : `${Math.round(row.quote_age_ms)}ms` },
    { key: 'entry_threshold', label: '入场线', children: fmtSpread(row.entry_threshold) },
    { key: 'exit_target', label: '退出线（平仓价差分位）', children: fmtSpread(row.exit_target) },
    { key: 'open_cost', label: '开仓成本', children: fmtMoney(row.open_cost) },
    { key: 'fees', label: '手续费成本', children: fmtMoney(row.fees) },
    { key: 'funding', label: 'HL 资金费', children: fmtCarryCost(row.funding) },
    { key: 'swap', label: 'MT5 隔夜费', children: fmtCarryCost(row.swap) },
    { key: 'realized_pnl', label: '已实现', children: fmtMoney(row.realized_pnl) },
    { key: 'unrealized_pnl', label: '未实现', children: fmtMoney(row.unrealized_pnl) },
    { key: 'source', label: '来源', children: <EllipsisCell value={row.source} /> },
    { key: 'close_reason', label: '平仓原因', children: <EllipsisCell value={row.close_reason} /> }
  ];
}

export function HedgeGroupsPage() {
  const [page, setPage] = useState(1);
  const streamStatus = usePageStream('hedge-groups', { page, pageSize: 20 });
  useHeaderStreamStatus(streamStatus.online);
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
    { title: 'ID', dataIndex: 'id', width: 64, align: 'right' },
    { title: '品种', dataIndex: 'symbol', width: 82, ellipsis: true, render: (v) => <EllipsisCell value={v} /> },
    { title: '方向', dataIndex: 'direction', width: 162, render: directionTags },
    { title: '状态', dataIndex: 'status', width: 82, render: statusTag },
    { title: '模式', dataIndex: 'execution_mode', width: 74, render: executionModeLabel },
    { title: '名义价值', dataIndex: 'notional', width: 104, align: 'right', render: (v) => <EllipsisCell value={fmtMoney(v)} align="right" /> },
    { title: '数量', dataIndex: 'quantity', width: 80, align: 'right', render: (v) => <EllipsisCell value={fmtAdaptive(v, 2, 6)} align="right" /> },
    { title: '触发价差', dataIndex: 'trigger_spread', width: 100, align: 'right', render: (v) => <EllipsisCell value={fmtSpread(v)} align="right" /> },
    { title: '开仓价差', dataIndex: 'entry_spread', width: 100, align: 'right', render: (v) => <EllipsisCell value={fmtSpread(v)} align="right" /> },
    { title: '平仓价差', dataIndex: 'current_close_spread', width: 100, align: 'right', render: (v) => <EllipsisCell value={v == null ? '-' : fmtSpread(v)} align="right" /> },
    { title: '资金费', dataIndex: 'funding', width: 92, align: 'right', render: (v) => <EllipsisCell value={fmtCarryCost(v)} align="right" /> },
    { title: '隔夜费', dataIndex: 'swap', width: 92, align: 'right', render: (v) => <EllipsisCell value={fmtCarryCost(v)} align="right" /> },
    { title: 'PnL', width: 92, align: 'right', render: (_, row) => <EllipsisCell value={fmtMoney(Number(row.realized_pnl || 0) + Number(row.unrealized_pnl || 0))} align="right" /> },
    { title: '操作', fixed: 'right', width: 86, render: (_, row) => <Button size="small" disabled={!['open', 'open_partial', 'manual_intervention'].includes(row.status)} onClick={() => close.mutate(row.id)}>平仓</Button> }
  ];
  const rows = query.data?.items || [];
  return (
    <div className="page-fill page-stack">
      {contextHolder}
      <Card
        title="对冲组"
        className="fill-card"
        extra={<Button loading={reconcile.isPending} onClick={() => reconcile.mutate()}>同步执行状态</Button>}
      >
        <Table
          rowKey="id"
          columns={columns}
          dataSource={rows}
          loading={query.isLoading}
          className="compact-data-table hedge-groups-table"
          tableLayout="fixed"
          scroll={tableScrollAutoY(1226, rows.length, 'calc(100vh - 314px)', 8)}
          pagination={{ current: page, pageSize: 20, total: query.data?.total || 0, onChange: setPage }}
          expandable={{
            expandedRowRender: (row) => <Descriptions size="small" column={{ xs: 1, sm: 2, lg: 4 }} items={detailItems(row)} />,
            rowExpandable: () => true
          }}
        />
      </Card>
    </div>
  );
}
