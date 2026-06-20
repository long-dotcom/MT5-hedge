import { Card, Statistic } from 'antd';

type Props = {
  title: string;
  value: string | number;
  suffix?: string;
};

export function DataCard({ title, value, suffix }: Props) {
  return (
    <Card className="metric-card" size="small">
      <Statistic title={title} value={value} suffix={suffix} />
    </Card>
  );
}

