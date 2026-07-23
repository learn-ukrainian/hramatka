import React, { useEffect, useState } from 'react';

interface MonitorStatus {
  host: string;
  timestamp: number;
  severity: 'healthy' | 'warning' | 'critical';
  load_1m_5m_15m: number[];
  ram: {
    total_mb: number;
    available_mb: number;
    used_percent: number;
  };
  capacity_reservations: {
    active_leases_count: number;
    total_reserved_ram_mb: number;
    host_reserved_ram_mb: number;
  };
  active_leases: Array<{
    lease_token: string;
    agent_id: string;
    task_name: string;
    reserved_ram_mb: number;
  }>;
}

interface AgentMonitorWidgetProps {
  apiFetch?: (path: string) => Promise<Response>;
}

export const AgentMonitorWidget: React.FC<AgentMonitorWidgetProps> = ({ apiFetch }) => {
  const [status, setStatus] = useState<MonitorStatus | null>(null);
  const [error, setError] = useState<boolean>(false);
  const [isOpen, setIsOpen] = useState<boolean>(false);

  useEffect(() => {
    let timer: ReturnType<typeof setInterval> | undefined;

    const fetchStatus = async () => {
      try {
        const fetcher = apiFetch || ((url: string) => fetch(url, { credentials: 'include' }));
        const res = await fetcher('/api/agent-monitor/status');
        if (res.ok) {
          const data: MonitorStatus = await res.json();
          setStatus(data);
          setError(false);
        } else {
          setError(true);
        }
      } catch {
        setError(true);
      }
    };

    fetchStatus();
    timer = setInterval(fetchStatus, 15000);
    return () => {
      if (timer) clearInterval(timer);
    };
  }, [apiFetch]);

  if (error || !status) {
    return (
      <div className="vps-monitor-chip error" title="VPS Monitor Offline">
        <span className="dot offline" /> VPS: Offline
      </div>
    );
  }

  const severityClass = status.severity === 'critical' || status.severity === 'warning' ? 'warning' : 'healthy';

  return (
    <div className="vps-monitor-container">
      <button
        type="button"
        className={`vps-monitor-chip ${severityClass}`}
        onClick={() => setIsOpen(!isOpen)}
        aria-expanded={isOpen}
        aria-label="Toggle VPS Agent Monitor Status"
      >
        <span className={`dot ${severityClass}`} />
        <span>VPS: {status.ram.used_percent.toFixed(0)}% RAM</span>
        <span className="badge">{status.capacity_reservations.active_leases_count} jobs</span>
      </button>

      {isOpen && (
        <div className="vps-monitor-popover" role="dialog" aria-label="VPS Resource Status Details">
          <div className="vps-popover-header">
            <h4>VPS Monitor ({status.host})</h4>
            <button
              type="button"
              className="close-btn"
              onClick={() => setIsOpen(false)}
              aria-label="Close Popover"
            >
              ×
            </button>
          </div>
          <div className="vps-popover-body">
            <div className="metric-row">
              <span>System Load (1m/5m):</span>
              <strong>{status.load_1m_5m_15m.slice(0, 2).map(n => n.toFixed(2)).join(' / ')}</strong>
            </div>
            <div className="metric-row">
              <span>Available RAM:</span>
              <strong>{status.ram.available_mb} MB / {status.ram.total_mb} MB</strong>
            </div>
            <div className="metric-row">
              <span>Agent Reserved RAM:</span>
              <strong>{status.capacity_reservations.total_reserved_ram_mb} MB</strong>
            </div>

            <h5 className="section-title">Active Agent Leases</h5>
            {status.active_leases.length === 0 ? (
              <p className="empty-msg">No active agent jobs registered.</p>
            ) : (
              <ul className="lease-list">
                {status.active_leases.map(lease => (
                  <li key={lease.lease_token} className="lease-item">
                    <span className="agent-tag">{lease.agent_id}</span>
                    <span className="task-name">{lease.task_name}</span>
                    <span className="ram-tag">{lease.reserved_ram_mb} MB</span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      )}
    </div>
  );
};
