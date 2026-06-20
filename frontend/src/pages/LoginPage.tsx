import { LockOutlined, UserOutlined } from '@ant-design/icons';
import { Button, Card, Form, Input, Typography, message } from 'antd';
import { useNavigate } from 'react-router-dom';
import { api } from '../api/client';

export function LoginPage() {
  const navigate = useNavigate();
  const [messageApi, contextHolder] = message.useMessage();

  const onFinish = async (values: { username: string; password: string }) => {
    try {
      const { data } = await api.post('/auth/login', values);
      localStorage.setItem('token', data.access_token);
      localStorage.setItem('user', JSON.stringify(data.user));
      navigate('/');
    } catch {
      messageApi.error('用户名或密码错误');
    }
  };

  return (
    <div className="login-page">
      {contextHolder}
      <Card className="login-card">
        <Typography.Title level={3}>MT5 Hedge</Typography.Title>
        <Form layout="vertical" onFinish={onFinish} initialValues={{ username: 'admin', password: 'admin123' }}>
          <Form.Item name="username" label="用户名" rules={[{ required: true }]}>
            <Input prefix={<UserOutlined />} />
          </Form.Item>
          <Form.Item name="password" label="密码" rules={[{ required: true }]}>
            <Input.Password prefix={<LockOutlined />} />
          </Form.Item>
          <Button type="primary" htmlType="submit" block>
            登录
          </Button>
        </Form>
      </Card>
    </div>
  );
}

