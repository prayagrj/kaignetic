import { Handle, Position } from '@xyflow/react';

interface EventNodeData {
  label: string;
  bpmn_type: 'START_EVENT' | 'END_EVENT' | 'BOUNDARY_EVENT';
}

export default function EventNode({ data }: { data: EventNodeData }) {
  const isEnd = data.bpmn_type === 'END_EVENT';
  const isBoundary = data.bpmn_type === 'BOUNDARY_EVENT';

  return (
    <div
      title={data.label}
      style={{
        width: '100%',
        height: '100%',
        borderRadius: '50%',
        background: isEnd ? '#fee2e2' : isBoundary ? '#fef9c3' : '#dcfce7',
        border: `${isEnd ? 3 : 1.5}px solid ${isEnd ? '#ef4444' : isBoundary ? '#ca8a04' : '#16a34a'}`,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        boxSizing: 'border-box',
        fontSize: 9,
        textAlign: 'center',
        color: '#374151',
        overflow: 'hidden',
        padding: 2,
      }}
    >
      {isEnd ? '●' : isBoundary ? '⚡' : ''}
      {!isEnd && <Handle type="source" position={Position.Right} style={{ background: '#9ca3af' }} />}
      {!data.bpmn_type.startsWith('START') && (
        <Handle type="target" position={Position.Left} style={{ background: '#9ca3af' }} />
      )}
    </div>
  );
}
