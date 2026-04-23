import { useRef, useState } from 'react';
import type { JobGraph } from './types';

const API = 'http://localhost:8000';

type State =
  | { phase: 'idle' }
  | { phase: 'dragging' }
  | { phase: 'processing'; filename: string }
  | { phase: 'error'; message: string };

interface Props {
  onJobLoaded: (data: JobGraph) => void;
}

export default function UploadScreen({ onJobLoaded }: Props) {
  const [state, setState] = useState<State>({ phase: 'idle' });
  const inputRef = useRef<HTMLInputElement>(null);

  async function submitFile(file: File) {
    const ext = file.name.split('.').pop()?.toLowerCase();
    if (!ext || !['pdf', 'docx', 'doc'].includes(ext)) {
      setState({ phase: 'error', message: 'Only PDF and DOCX files are supported.' });
      return;
    }

    setState({ phase: 'processing', filename: file.name });

    const form = new FormData();
    form.append('file', file);

    try {
      const res = await fetch(`${API}/api/upload`, { method: 'POST', body: form });
      const data = await res.json();
      if (!res.ok) {
        setState({ phase: 'error', message: data.detail ?? `Server error (${res.status})` });
        return;
      }
      onJobLoaded(data as JobGraph);
    } catch {
      setState({ phase: 'error', message: 'Could not reach the API server. Make sure server.py is running.' });
    }
  }

  function onInputChange(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (file) submitFile(file);
    e.target.value = '';
  }

  function onDrop(e: React.DragEvent) {
    e.preventDefault();
    setState({ phase: 'idle' });
    const file = e.dataTransfer.files?.[0];
    if (file) submitFile(file);
  }

  const isDragging = state.phase === 'dragging';
  const isProcessing = state.phase === 'processing';

  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        height: '100vh',
        background: '#f8fafc',
        fontFamily: 'system-ui, sans-serif',
        gap: 24,
      }}
    >
      {/* Header */}
      <div style={{ textAlign: 'center', marginBottom: 8 }}>
        <h1 style={{ margin: 0, fontSize: 26, fontWeight: 700, color: '#1e3a5f' }}>BPMN Viewer</h1>
        <p style={{ margin: '6px 0 0', fontSize: 14, color: '#6b7280' }}>
          Upload a PDF or DOCX to generate a process flow diagram
        </p>
      </div>

      {/* Drop zone */}
      <div
        onDragOver={(e) => { e.preventDefault(); setState({ phase: 'dragging' }); }}
        onDragLeave={() => setState({ phase: 'idle' })}
        onDrop={onDrop}
        onClick={() => !isProcessing && inputRef.current?.click()}
        style={{
          width: 420,
          maxWidth: '90vw',
          height: 220,
          border: `2px dashed ${isDragging ? '#2563eb' : '#cbd5e1'}`,
          borderRadius: 12,
          background: isDragging ? '#eff6ff' : '#fff',
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          gap: 12,
          cursor: isProcessing ? 'default' : 'pointer',
          transition: 'border-color 0.15s, background 0.15s',
          boxShadow: '0 1px 4px rgba(0,0,0,0.07)',
        }}
      >
        <input
          ref={inputRef}
          type="file"
          accept=".pdf,.docx,.doc"
          style={{ display: 'none' }}
          onChange={onInputChange}
        />

        {isProcessing ? (
          <>
            <Spinner />
            <p style={{ margin: 0, fontSize: 14, color: '#374151', fontWeight: 500 }}>
              Processing {state.filename}…
            </p>
            <p style={{ margin: 0, fontSize: 12, color: '#9ca3af' }}>
              This can take a minute or two
            </p>
          </>
        ) : (
          <>
            <UploadIcon color={isDragging ? '#2563eb' : '#94a3b8'} />
            <p style={{ margin: 0, fontSize: 14, color: '#374151', fontWeight: 500 }}>
              {isDragging ? 'Drop file here' : 'Drag & drop a file, or click to browse'}
            </p>
            <p style={{ margin: 0, fontSize: 12, color: '#9ca3af' }}>PDF, DOCX, DOC</p>
          </>
        )}
      </div>

      {/* Error message */}
      {state.phase === 'error' && (
        <div
          style={{
            width: 420,
            maxWidth: '90vw',
            padding: '10px 14px',
            background: '#fef2f2',
            border: '1px solid #fecaca',
            borderRadius: 8,
            color: '#dc2626',
            fontSize: 13,
            display: 'flex',
            alignItems: 'flex-start',
            gap: 8,
          }}
        >
          <span style={{ flexShrink: 0, marginTop: 1 }}>⚠</span>
          <span>{state.message}</span>
        </div>
      )}
    </div>
  );
}

function UploadIcon({ color }: { color: string }) {
  return (
    <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="16 16 12 12 8 16" />
      <line x1="12" y1="12" x2="12" y2="21" />
      <path d="M20.39 18.39A5 5 0 0 0 18 9h-1.26A8 8 0 1 0 3 16.3" />
    </svg>
  );
}

function Spinner() {
  return (
    <div
      style={{
        width: 36,
        height: 36,
        border: '3px solid #e2e8f0',
        borderTop: '3px solid #2563eb',
        borderRadius: '50%',
        animation: 'spin 0.8s linear infinite',
      }}
    />
  );
}
