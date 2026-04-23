import { Handle, Position } from '@xyflow/react';

interface TaskNodeData {
  label: string;
  needs_review: boolean;
  review_reasons: string[];
  bpmn_type: string;
}

export default function TaskNode({ data }: { data: TaskNodeData }) {
  const isSubprocess = data.bpmn_type === 'SUBPROCESS';

  return (
    <div
      title={data.needs_review ? data.review_reasons.join('; ') : data.label}
      style={{
        width: '100%',
        height: '100%',
        background: data.needs_review ? '#fff8e1' : '#fff',
        border: `1.5px solid ${data.needs_review ? '#f59e0b' : '#374151'}`,
        borderRadius: isSubprocess ? 8 : 4,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        padding: '8px 12px',
        boxSizing: 'border-box',
        fontSize: 12,
        lineHeight: '1.4',
        textAlign: 'center',
        color: '#111827',
        wordBreak: 'break-word',
        overflow: 'hidden',
        position: 'relative',
        boxShadow: '0 1px 3px rgba(0,0,0,0.08)',
      }}
    >
      {data.needs_review && (
        <span
          style={{
            position: 'absolute',
            top: 3,
            right: 5,
            fontSize: 10,
            color: '#f59e0b',
          }}
        >
          ⚠
        </span>
      )}
      {isSubprocess && (
        <span
          style={{
            position: 'absolute',
            bottom: 3,
            fontSize: 10,
            color: '#6b7280',
          }}
        >
          ⊕
        </span>
      )}
      <span style={{ maxWidth: '100%' }}>{data.label}</span>
      <Handle type="target" position={Position.Left} style={{ background: '#9ca3af' }} />
      <Handle type="source" position={Position.Right} style={{ background: '#9ca3af' }} />
    </div>
  );
}
