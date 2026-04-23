import { useState } from 'react';
import BPMNViewer from './BPMNViewer';
import UploadScreen from './UploadScreen';
import type { JobGraph, ProcessGraph } from './types';

export default function App() {
  const [jobData, setJobData] = useState<JobGraph | null>(null);
  const [selectedProcess, setSelectedProcess] = useState<ProcessGraph | null>(null);

  function handleJobLoaded(data: JobGraph) {
    setJobData(data);
    setSelectedProcess(data.processes[0] ?? null);
  }

  function handleReset() {
    setJobData(null);
    setSelectedProcess(null);
  }

  if (!jobData) {
    return <UploadScreen onJobLoaded={handleJobLoaded} />;
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100vh', fontFamily: 'system-ui, sans-serif' }}>
      {/* Toolbar */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 12,
          padding: '10px 16px',
          background: '#1e3a5f',
          color: '#fff',
          flexShrink: 0,
          boxShadow: '0 2px 6px rgba(0,0,0,0.2)',
        }}
      >
        <span style={{ fontWeight: 700, fontSize: 15, letterSpacing: '0.03em' }}>BPMN Viewer</span>

        <span
          style={{
            fontSize: 13,
            opacity: 0.75,
            maxWidth: 260,
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
          }}
        >
          {jobData.source_file}
        </span>

        {jobData.processes.length > 1 && (
          <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
            {jobData.processes.map((p) => (
              <button
                key={p.process_id}
                onClick={() => setSelectedProcess(p)}
                style={{
                  padding: '4px 10px',
                  borderRadius: 4,
                  border: '1.5px solid rgba(255,255,255,0.4)',
                  background:
                    selectedProcess?.process_id === p.process_id
                      ? 'rgba(255,255,255,0.25)'
                      : 'transparent',
                  color: '#fff',
                  fontSize: 12,
                  cursor: 'pointer',
                  fontWeight: selectedProcess?.process_id === p.process_id ? 700 : 400,
                }}
              >
                {p.name.length > 40 ? p.name.slice(0, 40) + '…' : p.name}
              </button>
            ))}
          </div>
        )}

        <div style={{ marginLeft: 'auto' }}>
          <button
            onClick={handleReset}
            style={{
              padding: '5px 12px',
              borderRadius: 4,
              border: '1.5px solid rgba(255,255,255,0.5)',
              background: 'transparent',
              color: '#fff',
              fontSize: 12,
              cursor: 'pointer',
            }}
          >
            Upload new file
          </button>
        </div>
      </div>

      {/* Canvas */}
      <div style={{ flex: 1, overflow: 'hidden' }}>
        {selectedProcess && (
          <BPMNViewer key={selectedProcess.process_id} proc={selectedProcess} />
        )}
      </div>
    </div>
  );
}
