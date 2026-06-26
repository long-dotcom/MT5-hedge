import { useQuery } from '@tanstack/react-query';
import { Alert, Space } from 'antd';
import { api } from '../api/client';
import { usePageStream } from '../hooks/useLiveStream';
import { PipelineDashboardV2 } from './pipeline/PipelineDashboardV2';
import { toV2DashboardData } from './pipeline/v2Adapter';
import type { PipelineDiagnostics } from './pipeline/types';

export function PipelinePage() {
  const streamStatus = usePageStream('pipeline');
  const query = useQuery<PipelineDiagnostics>({
    queryKey: ['pipeline-diagnostics'],
    queryFn: async () => (await api.get('/diagnostics/pipeline')).data
  });

  const data = query.data;
  return (
    <Space direction="vertical" size={16} className="full-width pipeline-page">
      {query.isError && <Alert type="error" showIcon message="链路诊断加载失败" />}
      {data && <PipelineDashboardV2 data={toV2DashboardData(data, streamStatus)} />}
    </Space>
  );
}
