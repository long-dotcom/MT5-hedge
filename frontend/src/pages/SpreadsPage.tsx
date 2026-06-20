import { ReloadOutlined } from '@ant-design/icons';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Button, Card, Space, Table, Tag, Typography, message } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useState } from 'react';
import { api } from '../api/client';
import { fmtAdaptive, fmtCompact, fmtNum } from '../utils/format';

type TradingSessionState = {
  symbol: string;
  status: string;
  reason: string;
};

export function SpreadsPage() {
  const [page, setPage] = useState(1);
  const queryClient = useQueryClient();
  const [messageApi, contextHolder] = message.useMessage();
  const spreads = useQuery({ queryKey: ['spreads', page], queryFn: async () => (await api.get('/markets/spreads', { params: { page, page_size: 20 } })).data });
  const sessions = useQuery({ queryKey: ['trading-sessions'], queryFn: async () => (await api.get('/markets/trading-sessions')).data, refetchInterval: 30000 });
  const symbols = useQuery({ queryKey: ['market-symbols'], queryFn: async () => (await api.get('/markets/symbols')).data });
  const sessionBySymbol = new Map<string, TradingSessionState>((sessions.data || []).map((row: TradingSessionState) => [row.symbol, row]));
  const precisionBySymbol = new Map<string, number>((symbols.data || []).map((row: any) => [row.symbol, row.price_precision ?? 2]));
  const priceDigits = (row: any) => Math.max(precisionBySymbol.get(row.symbol) ?? 2, 2);
  const currencyText = (row: any) => {
    const currency = row.notional_currency || 'USD';
    const fx = Number(row.fx_rate_to_usd || 1);
    if (currency === 'USD' && Math.abs(fx - 1) < 0.000001) return '-';
    return `${currency} ${fmtCompact(fx, 6)}`;
  };
  const scan = useMutation({
    mutationFn: async () => (await api.post('/markets/scan')).data,
    onSuccess: () => {
      messageApi.success('扫描完成');
      queryClient.invalidateQueries({ queryKey: ['spreads'] });
      queryClient.invalidateQueries({ queryKey: ['opportunities'] });
    }
  });

  const columns: ColumnsType<any> = [
    { title: '品种', dataIndex: 'symbol', fixed: 'left', width: 80 },
    {
      title: 'MT5状态',
      dataIndex: 'symbol',
      width: 145,
      render: (symbol) => {
        const state = sessionBySymbol.get(symbol);
        const status = state?.status || 'unknown';
        const color = status === 'normal_trade' ? 'green' : status === 'pre_close_no_open' || status === 'post_open_cooldown' ? 'gold' : 'red';
        return <Tag color={color}>{status}</Tag>;
      }
    },
    { title: '方向', dataIndex: 'direction', ellipsis: true, width: 205 },
    { title: 'HL Bid', dataIndex: 'hyperliquid_bid', width: 96, align: 'right', render: (v, row) => fmtNum(v, priceDigits(row)) },
    { title: 'HL Ask', dataIndex: 'hyperliquid_ask', width: 96, align: 'right', render: (v, row) => fmtNum(v, priceDigits(row)) },
    { title: 'MT5 Bid', dataIndex: 'mt5_bid', width: 96, align: 'right', render: (v, row) => fmtNum(v, priceDigits(row)) },
    { title: 'MT5 Ask', dataIndex: 'mt5_ask', width: 96, align: 'right', render: (v, row) => fmtNum(v, priceDigits(row)) },
    { title: 'MT5量', dataIndex: 'mt5_quantity', width: 86, align: 'right', render: (v) => fmtCompact(v, 4) },
    { title: 'HL量', dataIndex: 'hyperliquid_quantity', width: 96, align: 'right', render: (v) => fmtCompact(v, 6) },
    { title: '币种/汇率', width: 96, render: (_, row) => currencyText(row) },
    { title: '毛差/份', dataIndex: 'gross_spread', width: 92, align: 'right', render: (v) => fmtAdaptive(v) },
    { title: '成本/份', dataIndex: 'unit_cost', width: 92, align: 'right', render: (v) => fmtAdaptive(v) },
    { title: '净利/份', dataIndex: 'unit_net_profit', width: 92, align: 'right', render: (v) => fmtAdaptive(v) }
  ];

  return (
    <Space direction="vertical" size={16} className="full-width">
      {contextHolder}
      <div className="page-title-row">
        <Typography.Title level={3}>价差扫描</Typography.Title>
        <Button icon={<ReloadOutlined />} loading={scan.isPending} onClick={() => scan.mutate()}>
          立即扫描
        </Button>
      </div>
      <Card>
        <Table
          rowKey="id"
          columns={columns}
          dataSource={spreads.data?.items || []}
          loading={spreads.isLoading}
          scroll={{ x: 1280 }}
          pagination={{ current: page, pageSize: 20, total: spreads.data?.total || 0, onChange: setPage }}
        />
      </Card>
    </Space>
  );
}
