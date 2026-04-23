import { useMemo } from 'react';
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  useNodesState,
  useEdgesState,
  BackgroundVariant,
  ReactFlowProvider,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';

import TaskNode from './nodes/TaskNode';
import GatewayNode from './nodes/GatewayNode';
import EventNode from './nodes/EventNode';
import SwimlaneBackground from './nodes/SwimlaneBackground';
import { computeLayout } from './layout';
import type { ProcessGraph } from './types';

const nodeTypes = {
  task: TaskNode,
  gateway: GatewayNode,
  startEvent: EventNode,
  endEvent: EventNode,
};

interface Props {
  proc: ProcessGraph;
}

function FlowInner({ proc }: Props) {
  const layout = useMemo(() => computeLayout(proc), [proc]);
  const [nodes, , onNodesChange] = useNodesState(layout.nodes);
  const [edges, , onEdgesChange] = useEdgesState(layout.edges);

  return (
    <div style={{ position: 'relative', width: '100%', height: '100%' }}>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        nodeTypes={nodeTypes}
        fitView
        fitViewOptions={{ padding: 0.1 }}
        minZoom={0.2}
        maxZoom={2}
        attributionPosition="bottom-right"
        proOptions={{ hideAttribution: false }}
      >
        {/* Swimlane bands rendered behind the nodes */}
        <SwimlaneBackground
          lanes={layout.laneLayouts}
          totalWidth={layout.totalWidth}
          totalHeight={layout.totalHeight}
          processName={proc.name}
        />
        <Background variant={BackgroundVariant.Dots} gap={20} size={1} color="#e5e7eb" />
        <Controls />
        <MiniMap
          nodeStrokeWidth={2}
          zoomable
          pannable
          style={{ background: '#f8fafc' }}
        />
      </ReactFlow>
    </div>
  );
}

export default function BPMNViewer({ proc }: Props) {
  return (
    <ReactFlowProvider>
      <FlowInner proc={proc} />
    </ReactFlowProvider>
  );
}
