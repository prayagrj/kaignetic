import { Handle, Position } from '@xyflow/react';

interface GatewayNodeData {
  label: string;
  gateway_type: string | null;
  gateway_direction: string | null;
  needs_review: boolean;
}

const SYMBOL: Record<string, string> = {
  EXCLUSIVE: '✕',
  PARALLEL: '+',
  EVENT_BASED: '◎',
};

const COLOR: Record<string, string> = {
  EXCLUSIVE: '#fef3c7',
  PARALLEL: '#dbeafe',
  EVENT_BASED: '#f0fdf4',
};

const BORDER: Record<string, string> = {
  EXCLUSIVE: '#d97706',
  PARALLEL: '#2563eb',
  EVENT_BASED: '#16a34a',
};

export default function GatewayNode({ data }: { data: GatewayNodeData }) {
  const type = data.gateway_type ?? 'EXCLUSIVE';
  const symbol = SYMBOL[type] ?? '?';
  const bg = COLOR[type] ?? '#fef9c3';
  const border = data.needs_review ? '#ef4444' : (BORDER[type] ?? '#ca8a04');

  return (
    <div
      title={data.label || type}
      style={{
        width: '100%',
        height: '100%',
        transform: 'rotate(45deg)',
        background: bg,
        border: `2px solid ${border}`,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        boxSizing: 'border-box',
        borderRadius: 2,
      }}
    >
      <span style={{ transform: 'rotate(-45deg)', fontSize: 16, fontWeight: 700, color: border }}>
        {symbol}
      </span>
      {/* Handles rotated back so React Flow connects them on straight sides */}
      <Handle
        type="target"
        position={Position.Left}
        style={{ background: '#9ca3af', transform: 'rotate(-45deg)' }}
      />
      <Handle
        type="source"
        position={Position.Right}
        style={{ background: '#9ca3af', transform: 'rotate(-45deg)' }}
      />
      <Handle
        type="source"
        id="bottom"
        position={Position.Bottom}
        style={{ background: '#9ca3af', transform: 'rotate(-45deg)' }}
      />
    </div>
  );
}
