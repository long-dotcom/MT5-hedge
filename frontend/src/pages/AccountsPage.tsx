import { useQuery } from '@tanstack/react-query';
import { Card, Space, Typography } from 'antd';
import { api } from '../api/client';
import { AccountTable } from '../components/AccountTable';
import { usePageStream } from '../hooks/useLiveStream';

export function AccountsPage() {
  const streamStatus = usePageStream('accounts');
  const query = useQuery({ queryKey: ['accounts'], queryFn: async () => (await api.get('/accounts')).data });
  return (
    <Space direction="vertical" size={16} className="full-width">
      <Space className="full-width" align="center" style={{ justifyContent: 'space-between' }}>
        <Typography.Title level={3} style={{ margin: 0 }}>账户</Typography.Title>
        <Typography.Text type={streamStatus.online ? 'success' : 'secondary'}>{streamStatus.online ? '页面级推送运行中' : '等待页面级推送'}</Typography.Text>
      </Space>
      <Card><AccountTable data={query.data || []} loading={query.isLoading} /></Card>
    </Space>
  );
}
