import type { LaneLayout } from '../types';

// Alternating subtle background colours for lanes
const LANE_COLORS = [
  '#f8fafc',
  '#f1f5f9',
  '#f0f9ff',
  '#f0fdf4',
  '#fefce8',
  '#fdf4ff',
];

interface Props {
  lanes: LaneLayout[];
  totalWidth: number;
  totalHeight: number;
  processName: string;
  poolHeaderH?: number;
  laneHeaderW?: number;
}

export default function SwimlaneBackground({
  lanes,
  totalWidth,
  totalHeight,
  processName,
  poolHeaderH = 40,
  laneHeaderW = 120,
}: Props) {
  if (lanes.length === 0) return null;

  return (
    <div
      style={{
        position: 'absolute',
        top: 0,
        left: 0,
        width: totalWidth,
        height: totalHeight,
        pointerEvents: 'none',
        zIndex: 0,
      }}
    >
      {/* Pool header bar */}
      <div
        style={{
          position: 'absolute',
          top: 0,
          left: 0,
          width: totalWidth,
          height: poolHeaderH,
          background: '#1e3a5f',
          borderRadius: '6px 6px 0 0',
          display: 'flex',
          alignItems: 'center',
          paddingLeft: 16,
        }}
      >
        <span style={{ color: '#fff', fontWeight: 700, fontSize: 13, letterSpacing: '0.02em' }}>
          {processName}
        </span>
      </div>

      {/* Outer border */}
      <div
        style={{
          position: 'absolute',
          top: 0,
          left: 0,
          width: totalWidth,
          height: totalHeight,
          border: '1.5px solid #cbd5e1',
          borderRadius: 6,
          boxSizing: 'border-box',
        }}
      />

      {/* Lane rows */}
      {lanes.map((lane, i) => (
        <div
          key={lane.slug}
          style={{
            position: 'absolute',
            top: lane.y,
            left: 0,
            width: totalWidth,
            height: lane.height,
            background: LANE_COLORS[i % LANE_COLORS.length],
            borderTop: i > 0 ? '1px solid #e2e8f0' : 'none',
            boxSizing: 'border-box',
          }}
        >
          {/* Lane header strip */}
          <div
            style={{
              position: 'absolute',
              top: 0,
              left: 0,
              width: laneHeaderW,
              height: '100%',
              background: 'rgba(30,58,95,0.06)',
              borderRight: '1px solid #cbd5e1',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              boxSizing: 'border-box',
            }}
          >
            <span
              style={{
                writingMode: 'vertical-lr',
                transform: 'rotate(180deg)',
                fontSize: 11,
                fontWeight: 600,
                color: '#374151',
                letterSpacing: '0.05em',
                textTransform: 'uppercase',
                maxHeight: lane.height - 16,
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
              }}
            >
              {lane.actor}
            </span>
          </div>
        </div>
      ))}
    </div>
  );
}
