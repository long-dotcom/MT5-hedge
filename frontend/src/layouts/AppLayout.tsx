import {
  AlertOutlined,
  ApiOutlined,
  BarChartOutlined,
  DashboardOutlined,
  DatabaseOutlined,
  ExperimentOutlined,
  FundOutlined,
  LineChartOutlined,
  NodeIndexOutlined,
  HistoryOutlined,
  OrderedListOutlined,
  SafetyCertificateOutlined,
  SettingOutlined,
  StockOutlined
} from '@ant-design/icons';
import { Button, Layout, Menu, Space, Typography } from 'antd';
import { Outlet, useLocation, useNavigate } from 'react-router-dom';
import { useLiveStream } from '../hooks/useLiveStream';

const { Header, Sider, Content } = Layout;

const items = [
  { key: '/', icon: <DashboardOutlined />, label: '仪表盘' },
  { key: '/spreads', icon: <BarChartOutlined />, label: '价差扫描' },
  { key: '/analytics', icon: <ExperimentOutlined />, label: '价差研究' },
  { key: '/funding', icon: <LineChartOutlined />, label: '资金费研究' },
  { key: '/lead-lag', icon: <NodeIndexOutlined />, label: '报价领先滞后' },
  { key: '/opportunities', icon: <AlertOutlined />, label: '候选机会' },
  { key: '/hedge-groups', icon: <HistoryOutlined />, label: '对冲组' },
  { key: '/execution', icon: <OrderedListOutlined />, label: '执行记录' },
  { key: '/accounts', icon: <FundOutlined />, label: '账户' },
  { key: '/positions', icon: <StockOutlined />, label: '仓位' },
  { key: '/risk', icon: <SafetyCertificateOutlined />, label: '风控' },
  { key: '/logs', icon: <DatabaseOutlined />, label: '日志' },
  { key: '/settings', icon: <SettingOutlined />, label: '设置' }
];

export function AppLayout() {
  const navigate = useNavigate();
  const location = useLocation();
  useLiveStream();
  const logout = () => {
    localStorage.removeItem('token');
    localStorage.removeItem('user');
    navigate('/login');
  };

  return (
    <Layout className="app-shell">
      <Sider width={224} theme="light" className="side-nav">
        <div className="brand">
          <ApiOutlined />
          <span>MT5 Hedge</span>
        </div>
        <Menu mode="inline" selectedKeys={[location.pathname]} items={items} onClick={(event) => navigate(event.key)} />
      </Sider>
      <Layout>
        <Header className="topbar">
          <Typography.Text strong>Hyperliquid + MT5 套利管理台</Typography.Text>
          <Space>
            <Typography.Text type="secondary">admin</Typography.Text>
            <Button onClick={logout}>退出</Button>
          </Space>
        </Header>
        <Content className="page-content">
          <Outlet />
        </Content>
      </Layout>
    </Layout>
  );
}
