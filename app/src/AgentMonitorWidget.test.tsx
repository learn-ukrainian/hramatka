import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import React from 'react';
import { AgentMonitorWidget } from './AgentMonitorWidget';

describe('AgentMonitorWidget', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('renders offline state on fetch error', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('Network error')));
    render(<AgentMonitorWidget />);
    
    await waitFor(() => {
      expect(screen.getByText(/VPS: Offline/i)).toBeDefined();
    });
  });

  it('renders status chip and opens popover on click', async () => {
    const mockStatus = {
      host: 'hramatka',
      timestamp: 1700000000,
      severity: 'healthy',
      load_1m_5m_15m: [0.12, 0.05, 0.01],
      ram: { total_mb: 4000, available_mb: 2800, used_percent: 30.0 },
      capacity_reservations: { active_leases_count: 1, total_reserved_ram_mb: 512, host_reserved_ram_mb: 1250 },
      active_leases: [
        { lease_token: 'l1', agent_id: 'gemini/2156', task_name: 'scraping', reserved_ram_mb: 512 }
      ]
    };

    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      json: async () => mockStatus,
    }));

    render(<AgentMonitorWidget />);

    await waitFor(() => {
      expect(screen.getByText(/VPS: 30% RAM/i)).toBeDefined();
    });

    const button = screen.getByRole('button', { name: /Toggle VPS Agent Monitor Status/i });
    fireEvent.click(button);

    expect(screen.getByText(/VPS Monitor \(hramatka\)/i)).toBeDefined();
    expect(screen.getByText(/gemini\/2156/i)).toBeDefined();
  });
});
