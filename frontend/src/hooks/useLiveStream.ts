import { useEffect, useRef } from 'react';
import { useQueryClient } from '@tanstack/react-query';

type StreamSnapshot = {
  spreads?: { total: number; items: any[] };
  opportunities?: { total: number; items: any[] };
  accounts?: any[];
  latest_bucket_id?: number;
  pipeline?: any;
};

export function useLiveStream() {
  const queryClient = useQueryClient();
  const latestBucketId = useRef<number>(0);

  useEffect(() => {
    const token = localStorage.getItem('token');
    if (!token) return;

    const source = new EventSource(`/api/stream?token=${encodeURIComponent(token)}`);
    source.addEventListener('snapshot', (event) => {
      const data = JSON.parse((event as MessageEvent).data) as StreamSnapshot;
      if (data.spreads) {
        queryClient.setQueriesData({ queryKey: ['spreads'] }, data.spreads);
      }
      if (data.opportunities) {
        queryClient.setQueriesData({ queryKey: ['opportunities'] }, data.opportunities);
      }
      if (data.accounts) {
        queryClient.setQueryData(['accounts'], data.accounts);
      }
      if (data.pipeline) {
        queryClient.setQueryData(['pipeline-diagnostics'], data.pipeline);
      }
      if (data.latest_bucket_id && data.latest_bucket_id !== latestBucketId.current) {
        latestBucketId.current = data.latest_bucket_id;
        queryClient.invalidateQueries({ queryKey: ['spread-analytics'] });
      }
    });
    source.onerror = () => {
      if (source.readyState === EventSource.CLOSED) {
        source.close();
      }
    };
    return () => source.close();
  }, [queryClient]);
}
