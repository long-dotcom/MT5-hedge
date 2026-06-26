import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Alert, Button, Card, Collapse, Form, Input, InputNumber, List, Modal, Popconfirm, Select, Space, Switch, Table, Tabs, Tag, Typography, message } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useState } from 'react';
import { api } from '../api/client';
import { RISK_MODE_MAP } from '../utils/format';

export function SettingsPage() {
  const queryClient = useQueryClient();
  const [messageApi, contextHolder] = message.useMessage();
  const [symbolForm] = Form.useForm();
  const [sessionForm] = Form.useForm();
  const [editingSymbol, setEditingSymbol] = useState<any | null>(null);
  const [editingSession, setEditingSession] = useState<any | null>(null);
  const [symbolModalOpen, setSymbolModalOpen] = useState(false);
  const [sessionModalOpen, setSessionModalOpen] = useState(false);
  const strategy = useQuery({ queryKey: ['settings-strategy'], queryFn: async () => (await api.get('/settings/strategy')).data });
  const risk = useQuery({ queryKey: ['settings-risk'], queryFn: async () => (await api.get('/settings/risk')).data });
  const symbols = useQuery({ queryKey: ['settings-symbols'], queryFn: async () => (await api.get('/settings/symbol-mappings')).data });
  const sessionTemplates = useQuery({ queryKey: ['settings-mt5-session-templates'], queryFn: async () => (await api.get('/settings/mt5-session-templates')).data });
  const live = useQuery({ queryKey: ['settings-live'], queryFn: async () => (await api.get('/settings/live-trading')).data });
  const liveReadiness = useQuery({ queryKey: ['settings-live-readiness'], queryFn: async () => (await api.get('/settings/live-readiness')).data });
  const saveStrategy = useMutation({ mutationFn: async (v: any) => (await api.put('/settings/strategy', v)).data, onSuccess: () => { messageApi.success('策略已保存'); queryClient.invalidateQueries({ queryKey: ['settings-strategy'] }); } });
  const saveRisk = useMutation({ mutationFn: async (v: any) => (await api.put('/settings/risk', v)).data, onSuccess: () => { messageApi.success('风控已保存'); queryClient.invalidateQueries({ queryKey: ['settings-risk'] }); } });
  const saveLive = useMutation({ mutationFn: async (v: any) => (await api.put('/settings/live-trading', v)).data, onSuccess: () => { messageApi.success('实盘开关已保存'); queryClient.invalidateQueries({ queryKey: ['settings-live'] }); queryClient.invalidateQueries({ queryKey: ['settings-live-readiness'] }); } });
  const saveSymbol = useMutation({
    mutationFn: async (v: any) => editingSymbol ? (await api.put(`/settings/symbol-mappings/${editingSymbol.id}`, v)).data : (await api.post('/settings/symbol-mappings', v)).data,
    onSuccess: () => {
      messageApi.success('品种映射已保存');
      setSymbolModalOpen(false);
      setEditingSymbol(null);
      symbolForm.resetFields();
      queryClient.invalidateQueries({ queryKey: ['settings-symbols'] });
      queryClient.invalidateQueries({ queryKey: ['market-symbols'] });
      queryClient.invalidateQueries({ queryKey: ['spreads'] });
      queryClient.invalidateQueries({ queryKey: ['opportunities'] });
    },
    onError: (err: any) => messageApi.error(err.response?.data?.detail || '保存失败')
  });
  const deleteSymbol = useMutation({
    mutationFn: async (id: number) => (await api.delete(`/settings/symbol-mappings/${id}`)).data,
    onSuccess: () => {
      messageApi.success('品种映射已删除');
      queryClient.invalidateQueries({ queryKey: ['settings-symbols'] });
      queryClient.invalidateQueries({ queryKey: ['market-symbols'] });
      queryClient.invalidateQueries({ queryKey: ['spreads'] });
      queryClient.invalidateQueries({ queryKey: ['opportunities'] });
    }
  });
  const syncBroker = useMutation({
    mutationFn: async (id: number) => (await api.post(`/settings/symbol-mappings/${id}/sync-broker`)).data,
    onSuccess: (data) => {
      messageApi.success(`已同步 MT5 规格：最小量 ${data.min_order_size}`);
      queryClient.invalidateQueries({ queryKey: ['settings-symbols'] });
    },
    onError: (err: any) => messageApi.error(err.response?.data?.detail || '同步失败')
  });
  const syncSessions = useMutation({
    mutationFn: async (id: number) => (await api.post(`/settings/symbol-mappings/${id}/sync-sessions`)).data,
    onSuccess: (data) => {
      messageApi.success(`已同步交易时段：${data.mt5_session_template}`);
      queryClient.invalidateQueries({ queryKey: ['settings-symbols'] });
    },
    onError: (err: any) => messageApi.error(err.response?.data?.detail || '同步失败')
  });
  const saveSession = useMutation({
    mutationFn: async (v: any) => (await api.put(`/settings/symbol-mappings/${editingSession.id}`, { ...editingSession, ...v })).data,
    onSuccess: () => {
      messageApi.success('交易时段已保存');
      setSessionModalOpen(false);
      setEditingSession(null);
      sessionForm.resetFields();
      queryClient.invalidateQueries({ queryKey: ['settings-symbols'] });
    },
    onError: (err: any) => messageApi.error(err.response?.data?.detail || '保存失败')
  });
  const openSymbolModal = (row?: any) => {
    setEditingSymbol(row || null);
    symbolForm.setFieldsValue(row || {
      symbol: '',
      hyperliquid_symbol: '',
      mt5_symbol: '',
      base_asset: '',
      quote_asset: 'USD',
      contract_multiplier: 1,
      min_order_size: 0.01,
      min_entry_spread: 0,
      max_close_spread: 0,
      mt5_min_lot: 0,
      mt5_volume_step: 0,
      mt5_contract_size: 1,
      mt5_currency_base: '',
      mt5_currency_profit: 'USD',
      mt5_currency_margin: 'USD',
      mt5_calc_mode: 0,
      mt5_min_base_size: 0,
      hyperliquid_min_base_size: 0,
      hyperliquid_min_notional: 10,
      execution_style: 'taker_taker',
      hl_open_order_type: 'market',
      hl_close_order_type: 'market',
      hl_post_only: false,
      hl_maker_offset_bps: 1,
      hl_order_ttl_seconds: 3,
      hl_unfilled_action: 'cancel',
      single_leg_action: 'manual_intervention',
      mt5_open_order_type: 'market',
      mt5_close_order_type: 'market',
      mt5_session_enabled: true,
      mt5_session_auto_sync: true,
      mt5_session_template: 'auto',
      mt5_session_timezone: 'UTC',
      mt5_regular_sessions_json: '[]',
      mt5_close_only_sessions_json: '[]',
      mt5_quote_only_sessions_json: '[]',
      mt5_session_source: 'manual',
      mt5_pre_close_no_open_minutes: 15,
      mt5_post_open_cooldown_minutes: 10,
      allow_hold_through_mt5_close: false,
      quantity_precision: 2,
      price_precision: 2,
      min_tick: 0.01,
      max_slippage_bps: 8,
      enabled: true
    });
    setSymbolModalOpen(true);
  };
  const openSessionModal = (row: any) => {
    setEditingSession(row);
    sessionForm.setFieldsValue({
      mt5_session_enabled: row.mt5_session_enabled ?? true,
      mt5_session_auto_sync: row.mt5_session_auto_sync ?? true,
      mt5_session_template: row.mt5_session_template || 'auto',
      mt5_session_timezone: row.mt5_session_timezone || 'UTC',
      mt5_regular_sessions_json: row.mt5_regular_sessions_json || '[]',
      mt5_close_only_sessions_json: row.mt5_close_only_sessions_json || '[]',
      mt5_quote_only_sessions_json: row.mt5_quote_only_sessions_json || '[]',
      mt5_pre_close_no_open_minutes: row.mt5_pre_close_no_open_minutes ?? 15,
      mt5_post_open_cooldown_minutes: row.mt5_post_open_cooldown_minutes ?? 10,
      allow_hold_through_mt5_close: row.allow_hold_through_mt5_close ?? false
    });
    setSessionModalOpen(true);
  };
  const columns: ColumnsType<any> = [
    { title: '内部品种', dataIndex: 'symbol' },
    { title: 'Hyperliquid', dataIndex: 'hyperliquid_symbol' },
    { title: 'MT5', dataIndex: 'mt5_symbol' },
    { title: 'MT5最小手数', dataIndex: 'mt5_min_lot' },
    { title: 'MT5步进', dataIndex: 'mt5_volume_step' },
    { title: '合约大小', dataIndex: 'mt5_contract_size' },
    { title: '盈亏币种', dataIndex: 'mt5_currency_profit' },
    { title: '买入价差下限', dataIndex: 'min_entry_spread', width: 130 },
    { title: '卖出价差上限', dataIndex: 'max_close_spread', width: 130 },
    { title: '执行方式', dataIndex: 'execution_style', ellipsis: true, width: 170 },
    { title: '启用', dataIndex: 'enabled', render: (v) => (v ? '是' : '否') },
    {
      title: '操作',
      fixed: 'right',
      width: 230,
      render: (_, row) => (
        <Space>
          <Button size="small" onClick={() => openSymbolModal(row)}>编辑</Button>
          <Button size="small" loading={syncBroker.isPending} onClick={() => syncBroker.mutate(row.id)}>同步MT5</Button>
          <Popconfirm title="确认删除该映射？" onConfirm={() => deleteSymbol.mutate(row.id)}>
            <Button size="small" danger>删除</Button>
          </Popconfirm>
        </Space>
      )
    }
  ];
  const sessionColumns: ColumnsType<any> = [
    { title: '内部品种', dataIndex: 'symbol', width: 100 },
    { title: 'MT5', dataIndex: 'mt5_symbol', width: 130 },
    { title: '模板', dataIndex: 'mt5_session_template', width: 170, render: (v) => <Tag>{v || 'auto'}</Tag> },
    { title: '时区', dataIndex: 'mt5_session_timezone', width: 100 },
    { title: '自动同步', dataIndex: 'mt5_session_auto_sync', width: 100, render: (v) => (v ? '是' : '否') },
    { title: '启用', dataIndex: 'mt5_session_enabled', width: 80, render: (v) => (v ? '是' : '否') },
    { title: '来源', dataIndex: 'mt5_session_source', width: 140, ellipsis: true },
    { title: '最后同步', dataIndex: 'mt5_session_last_synced_at', width: 180, render: (v) => v ? new Date(v).toLocaleString() : '-' },
    {
      title: '操作',
      fixed: 'right',
      width: 190,
      render: (_, row) => (
        <Space>
          <Button size="small" onClick={() => openSessionModal(row)}>编辑</Button>
          <Button size="small" loading={syncSessions.isPending} onClick={() => syncSessions.mutate(row.id)}>同步模板</Button>
        </Space>
      )
    }
  ];

  return (
    <Space direction="vertical" size={16} className="full-width">
      {contextHolder}
      <Typography.Title level={3}>设置</Typography.Title>
      <Card>
        <Tabs
          items={[
            {
              key: 'strategy',
              label: '策略参数',
              children: (
                <Form key={strategy.data?.updated_at || 'strategy-loading'} layout="vertical" className="settings-form" initialValues={strategy.data} onFinish={(v) => saveStrategy.mutate({ ...strategy.data, ...v })}>
                  <Alert type="info" showIcon message="第一版按单次目标 USD 名义价值触发；两边数量由品种规格、计价币种和实时汇率自动计算。" className="form-alert" />
                  <Form.Item name="signal_mode" label="信号模式">
                    <Select options={[{ value: 'statistical', label: '统计可达入场线' }, { value: 'fixed_profit', label: '固定净利润' }]} />
                  </Form.Item>
                  <Form.Item name="default_notional" label="单次目标名义价值 USD"><InputNumber min={1} /></Form.Item>
                  <Form.Item name="statistical_lookback_range" label="统计窗口">
                    <Select options={[{ value: '15m' }, { value: '1h' }, { value: '4h' }, { value: '24h' }]} />
                  </Form.Item>
                  <Form.Item name="statistical_min_samples" label="最小样本数"><InputNumber min={20} step={10} /></Form.Item>
                  <Form.Item name="reachable_entry_percentile" label="可达入场分位数"><InputNumber min={0.5} max={0.95} step={0.01} /></Form.Item>
                  <Form.Item name="cost_guard_percentile" label="成本保护分位数"><InputNumber min={0.5} max={0.99} step={0.01} /></Form.Item>
                  <Form.Item name="min_total_profit" label="最小总净利润 USD"><InputNumber min={0} step={0.1} /></Form.Item>
                  <Form.Item name="execution_mode" label="执行模式"><Select options={[{ value: 'dry_run' }, { value: 'paper' }, { value: 'live' }]} /></Form.Item>
                  <Form.Item name="auto_execute_enabled" label="自动执行" valuePropName="checked"><Switch /></Form.Item>
                  <Form.Item name="auto_close_enabled" label="自动平仓" valuePropName="checked"><Switch /></Form.Item>
                  <Form.Item name="auto_close_live_enabled" label="Live 自动平仓" valuePropName="checked"><Switch /></Form.Item>
                  <Form.Item name="exit_target_percentile" label="平仓价差退出低分位数"><InputNumber min={0.05} max={0.5} step={0.01} /></Form.Item>
                  <Form.Item name="auto_close_unit_profit_buffer" label="每份平仓利润缓冲"><InputNumber min={0} step={0.01} /></Form.Item>
                  <Form.Item name="auto_close_min_profit" label="自动平仓最小利润 USD"><InputNumber min={0} step={0.1} /></Form.Item>
                  <Form.Item name="auto_execute_confirm_ticks" label="确认次数"><InputNumber min={1} step={1} /></Form.Item>
                  <Form.Item name="auto_execute_min_hold_ms" label="最小持续毫秒"><InputNumber min={0} step={50} /></Form.Item>
                  <Form.Item name="auto_execute_cooldown_seconds" label="冷却秒"><InputNumber min={0} step={1} /></Form.Item>
                  <Collapse
                    items={[
                      {
                        key: 'advanced-strategy',
                        label: '高级策略与 Paper 模拟',
                        children: (
                          <>
                            <Form.Item name="min_annualized_return" label="最小年化收益"><InputNumber min={0} step={0.01} /></Form.Item>
                            <Form.Item name="min_net_profit" label="固定净利润模式阈值 USD"><InputNumber min={0} step={0.1} /></Form.Item>
                            <Form.Item name="reachable_entry_zscore" label="可达入场 Z 倍数"><InputNumber min={0} step={0.1} /></Form.Item>
                            <Form.Item name="min_unit_edge" label="最小每份边际"><InputNumber min={0} step={0.1} /></Form.Item>
                            <Form.Item name="max_holding_minutes" label="最大持仓分钟"><InputNumber min={1} /></Form.Item>
                            <Form.Item name="paper_use_live_account_risk" label="Paper 使用真实账户资金风控" valuePropName="checked"><Switch /></Form.Item>
                            <Form.Item name="auto_execute_paper_only" label="仅允许纸面自动执行" valuePropName="checked"><Switch /></Form.Item>
                            <Form.Item name="auto_execute_max_per_symbol_open_groups" label="单品种未平对冲组上限"><InputNumber min={1} step={1} /></Form.Item>
                            <Form.Item name="auto_execute_max_global_open_groups" label="全局未平对冲组上限"><InputNumber min={1} step={1} /></Form.Item>
                            <Form.Item name="auto_execute_min_net_profit" label="自动执行额外最小净利润"><InputNumber min={0} step={0.1} /></Form.Item>
                            <Form.Item name="paper_decision_delay_ms_min" label="Paper 决策延迟最小毫秒"><InputNumber min={0} step={10} /></Form.Item>
                            <Form.Item name="paper_decision_delay_ms_max" label="Paper 决策延迟最大毫秒"><InputNumber min={0} step={10} /></Form.Item>
                            <Form.Item name="paper_hyperliquid_latency_ms_min" label="Paper Hyperliquid 延迟最小毫秒"><InputNumber min={0} step={10} /></Form.Item>
                            <Form.Item name="paper_hyperliquid_latency_ms_max" label="Paper Hyperliquid 延迟最大毫秒"><InputNumber min={0} step={10} /></Form.Item>
                            <Form.Item name="paper_mt5_latency_ms_min" label="Paper MT5 延迟最小毫秒"><InputNumber min={0} step={10} /></Form.Item>
                            <Form.Item name="paper_mt5_latency_ms_max" label="Paper MT5 延迟最大毫秒"><InputNumber min={0} step={10} /></Form.Item>
                          </>
                        )
                      }
                    ]}
                  />
                  <Button type="primary" htmlType="submit">保存策略</Button>
                </Form>
              )
            },
            {
              key: 'risk',
              label: '风控参数',
              children: (
                <Form key={risk.data?.updated_at || 'risk-loading'} layout="vertical" className="settings-form" initialValues={risk.data} onFinish={(v) => saveRisk.mutate({ ...risk.data, ...v })}>
                  <Form.Item name="mode" label="系统模式"><Select options={Object.entries(RISK_MODE_MAP).map(([value, { label }]) => ({ value, label }))} /></Form.Item>
                  <Form.Item name="max_order_notional" label="单笔名义价值上限 USD"><InputNumber min={1} /></Form.Item>
                  <Form.Item name="max_slippage_bps" label="最大滑点 bps"><InputNumber min={0} /></Form.Item>
                  <Form.Item name="max_market_age_seconds" label="最大行情延迟秒"><InputNumber min={1} /></Form.Item>
                  <Collapse
                    items={[
                      {
                        key: 'advanced-risk',
                        label: '高级资金与账户风控',
                        children: (
                          <>
                            <Form.Item name="max_symbol_exposure" label="品种敞口"><InputNumber min={1} /></Form.Item>
                            <Form.Item name="max_total_leverage" label="总杠杆"><InputNumber min={0} step={0.1} /></Form.Item>
                            <Form.Item name="max_new_margin_fraction" label="单笔可用资金比例"><InputNumber min={0} max={1} step={0.05} /></Form.Item>
                            <Form.Item name="new_order_leverage" label="下单杠杆估算"><InputNumber min={1} step={1} /></Form.Item>
                            <Form.Item name="min_margin_ratio" label="最低保证金率"><InputNumber min={0} step={0.01} /></Form.Item>
                            <Form.Item name="max_api_errors" label="最大 API 错误次数"><InputNumber min={1} /></Form.Item>
                          </>
                        )
                      }
                    ]}
                  />
                  <Button type="primary" htmlType="submit">保存风控</Button>
                </Form>
              )
            },
            {
              key: 'symbols',
              label: '品种映射',
              children: (
                <Space direction="vertical" size={12} className="full-width">
                  <Alert type="info" showIcon message="同步 MT5 会写入最小手数、步进、合约大小和计价币种；扫描时按目标 USD 名义价值自动计算 MT5 手数和 HL 数量。" />
                  <Button type="primary" onClick={() => openSymbolModal()}>新增映射</Button>
                  <Table rowKey="id" columns={columns} dataSource={symbols.data || []} loading={symbols.isLoading} pagination={{ pageSize: 10 }} scroll={{ x: 1200 }} />
                </Space>
              )
            },
            {
              key: 'mt5-sessions',
              label: 'MT5 交易时段',
              children: (
                <Space direction="vertical" size={12} className="full-width">
                  <Alert type="info" showIcon message="本地交易时段用于补充经纪商的 close-only / quote-only 规则：只平仓窗口禁止新增、允许平仓；仅报价和休市窗口禁止所有交易动作。" />
                  <Table rowKey="id" columns={sessionColumns} dataSource={symbols.data || []} loading={symbols.isLoading} pagination={{ pageSize: 10 }} scroll={{ x: 1300 }} />
                </Space>
              )
            },
            {
              key: 'live',
              label: '实盘开关',
              children: (
                <Space direction="vertical" size={12} className="full-width">
                  <Alert type="warning" showIcon message="真实下单默认关闭。开启前请确认 API 凭证、MT5 登录、风控参数和品种映射。" className="form-alert" />
                  <Card size="small" title="实盘就绪检查">
                    <Space direction="vertical" size={8} className="full-width">
                      <Tag color={liveReadiness.data?.status === 'ready' ? 'green' : liveReadiness.data?.status === 'warning' ? 'gold' : 'red'}>
                        {liveReadiness.data?.status || 'loading'}
                      </Tag>
                      <List
                        size="small"
                        loading={liveReadiness.isLoading}
                        dataSource={liveReadiness.data?.checks || []}
                        renderItem={(item: any) => (
                          <List.Item>
                            <Space>
                              <Tag color={item.status === 'ok' ? 'green' : item.status === 'warn' ? 'gold' : 'red'}>{item.status}</Tag>
                              <Typography.Text>{item.message}</Typography.Text>
                            </Space>
                          </List.Item>
                        )}
                      />
                    </Space>
                  </Card>
                  <Form key={String(live.data?.enabled)} layout="vertical" className="settings-form" initialValues={{ enabled: live.data?.enabled, confirmation: '' }} onFinish={(v) => saveLive.mutate(v)}>
                    <Form.Item name="enabled" label="允许实盘" valuePropName="checked"><Switch /></Form.Item>
                    <Form.Item name="confirmation" label="确认短语"><Input placeholder="ENABLE LIVE TRADING" /></Form.Item>
                    <Button danger htmlType="submit">保存实盘开关</Button>
                  </Form>
                </Space>
              )
            }
          ]}
        />
      </Card>
      <Modal
        title={editingSymbol ? '编辑品种映射' : '新增品种映射'}
        open={symbolModalOpen}
        onCancel={() => setSymbolModalOpen(false)}
        onOk={() => symbolForm.submit()}
        confirmLoading={saveSymbol.isPending}
        destroyOnClose
      >
        <Form form={symbolForm} layout="vertical" onFinish={(v) => saveSymbol.mutate(v)}>
          <Form.Item name="symbol" label="内部品种" rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item name="hyperliquid_symbol" label="Hyperliquid 品种" rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item name="mt5_symbol" label="MT5 品种" rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item name="base_asset" label="基础资产"><Input /></Form.Item>
          <Form.Item name="quote_asset" label="报价资产"><Input /></Form.Item>
          <Form.Item name="contract_multiplier" label="合约乘数"><InputNumber min={0} step={0.01} /></Form.Item>
          <Form.Item name="min_order_size" label="最终最小量"><InputNumber min={0} step={0.001} disabled /></Form.Item>
          <Form.Item name="min_entry_spread" label="最小买入价差"><InputNumber min={0} step={0.01} /></Form.Item>
          <Form.Item name="max_close_spread" label="最大卖出价差"><InputNumber step={0.01} /></Form.Item>
          <Form.Item name="mt5_min_lot" label="MT5 最小手数"><InputNumber min={0} step={0.01} disabled /></Form.Item>
          <Form.Item name="mt5_volume_step" label="MT5 手数步进"><InputNumber min={0} step={0.01} disabled /></Form.Item>
          <Form.Item name="mt5_contract_size" label="MT5 合约大小"><InputNumber min={0} step={0.01} disabled /></Form.Item>
          <Form.Item name="mt5_currency_base" label="MT5 基础币种"><Input disabled /></Form.Item>
          <Form.Item name="mt5_currency_profit" label="MT5 盈亏币种"><Input disabled /></Form.Item>
          <Form.Item name="mt5_currency_margin" label="MT5 保证金币种"><Input disabled /></Form.Item>
          <Form.Item name="mt5_calc_mode" label="MT5 计算模式"><InputNumber disabled /></Form.Item>
          <Form.Item name="mt5_min_base_size" label="MT5 基础最小量"><InputNumber min={0} step={0.001} disabled /></Form.Item>
          <Form.Item name="hyperliquid_min_base_size" label="Hyperliquid 最小基础量"><InputNumber min={0} step={0.001} /></Form.Item>
          <Form.Item name="hyperliquid_min_notional" label="Hyperliquid 最小名义额"><InputNumber min={0} step={1} /></Form.Item>
          <Form.Item name="quantity_precision" label="数量精度"><InputNumber min={0} max={8} /></Form.Item>
          <Form.Item name="price_precision" label="价格精度"><InputNumber min={0} max={8} /></Form.Item>
          <Form.Item name="min_tick" label="最小价格跳动"><InputNumber min={0} step={0.0001} /></Form.Item>
          <Form.Item name="max_slippage_bps" label="最大滑点 bps"><InputNumber min={0} /></Form.Item>
          <Form.Item name="execution_style" label="执行方式">
            <Select options={[{ value: 'taker_taker', label: '双边市价' }, { value: 'hyper_maker_mt5_taker', label: 'HL挂单成交后MT5市价' }]} />
          </Form.Item>
          <Form.Item name="hl_open_order_type" label="HL 开仓订单">
            <Select options={[{ value: 'market', label: '市价/taker' }, { value: 'limit', label: '限价/maker' }]} />
          </Form.Item>
          <Form.Item name="hl_close_order_type" label="HL 平仓订单">
            <Select options={[{ value: 'market', label: '市价/taker' }, { value: 'limit', label: '限价/maker' }]} />
          </Form.Item>
          <Form.Item name="hl_post_only" label="HL post-only" valuePropName="checked"><Switch /></Form.Item>
          <Form.Item name="hl_maker_offset_bps" label="HL 挂单偏移 bps"><InputNumber min={0} step={0.1} /></Form.Item>
          <Form.Item name="hl_order_ttl_seconds" label="HL 挂单 TTL 秒"><InputNumber min={0} /></Form.Item>
          <Form.Item name="hl_unfilled_action" label="HL 未成交动作">
            <Select options={[{ value: 'cancel', label: '撤单放弃' }, { value: 'taker_fallback', label: '转市价兜底' }]} />
          </Form.Item>
          <Form.Item name="single_leg_action" label="单腿异常动作">
            <Select options={[{ value: 'manual_intervention', label: '人工介入' }, { value: 'auto_close', label: '自动回滚' }]} />
          </Form.Item>
          <Form.Item name="mt5_open_order_type" label="MT5 开仓订单">
            <Select options={[{ value: 'market', label: '市价' }]} />
          </Form.Item>
          <Form.Item name="mt5_close_order_type" label="MT5 平仓订单">
            <Select options={[{ value: 'market', label: '市价' }]} />
          </Form.Item>
          <Form.Item name="mt5_pre_close_no_open_minutes" label="MT5 盘尾禁止新开仓分钟">
            <InputNumber min={0} max={240} />
          </Form.Item>
          <Form.Item name="mt5_post_open_cooldown_minutes" label="MT5 开盘冷却分钟">
            <InputNumber min={0} max={240} />
          </Form.Item>
          <Form.Item name="allow_hold_through_mt5_close" label="允许跨 MT5 休市持仓" valuePropName="checked">
            <Switch />
          </Form.Item>
          <Form.Item name="enabled" label="启用" valuePropName="checked"><Switch /></Form.Item>
        </Form>
      </Modal>
      <Modal
        title={editingSession ? `MT5 交易时段：${editingSession.symbol}` : 'MT5 交易时段'}
        open={sessionModalOpen}
        onCancel={() => setSessionModalOpen(false)}
        onOk={() => sessionForm.submit()}
        confirmLoading={saveSession.isPending}
        width={760}
        destroyOnClose
      >
        <Form form={sessionForm} layout="vertical" onFinish={(v) => saveSession.mutate(v)}>
          <Form.Item name="mt5_session_enabled" label="启用本地时段保护" valuePropName="checked"><Switch /></Form.Item>
          <Form.Item name="mt5_session_auto_sync" label="允许自动同步模板" valuePropName="checked"><Switch /></Form.Item>
          <Form.Item name="mt5_session_template" label="交易时段模板">
            <Select
              loading={sessionTemplates.isLoading}
              options={(sessionTemplates.data || []).map((item: any) => ({ value: item.value, label: `${item.label} (${item.value})` }))}
            />
          </Form.Item>
          <Form.Item name="mt5_session_timezone" label="时区"><Input placeholder="UTC" /></Form.Item>
          <Form.Item name="mt5_regular_sessions_json" label="正常交易窗口 JSON">
            <Input.TextArea rows={5} />
          </Form.Item>
          <Form.Item name="mt5_close_only_sessions_json" label="只平仓窗口 JSON">
            <Input.TextArea rows={5} />
          </Form.Item>
          <Form.Item name="mt5_quote_only_sessions_json" label="仅报价窗口 JSON">
            <Input.TextArea rows={4} />
          </Form.Item>
          <Form.Item name="mt5_pre_close_no_open_minutes" label="盘尾禁止新开仓分钟"><InputNumber min={0} max={240} /></Form.Item>
          <Form.Item name="mt5_post_open_cooldown_minutes" label="开盘冷却分钟"><InputNumber min={0} max={240} /></Form.Item>
          <Form.Item name="allow_hold_through_mt5_close" label="允许跨 MT5 休市持仓" valuePropName="checked"><Switch /></Form.Item>
        </Form>
      </Modal>
    </Space>
  );
}
