import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Line } from 'react-chartjs-2';
import { Chart as ChartJS, CategoryScale, LinearScale, LineElement, PointElement, Tooltip, Legend } from 'chart.js';
import './EvalDashboard.css';

ChartJS.register(CategoryScale, LinearScale, LineElement, PointElement, Tooltip, Legend);

const API = 'http://localhost:8080/api/v1/eval';
const METRICS = ['precision', 'recall', 'faithfulness', 'relevancy'] as const;
const COLORS: Record<string, string> = {
    precision: '#4f9cf9', recall: '#34c38f', faithfulness: '#f1b44c', relevancy: '#a78bfa',
};

type Point = { run_id: string; ts: string; value: number };
type Series = Record<(typeof METRICS)[number], Point[]>;
interface Run {
    run_id: string; started_at: string; label: string | null; k: number; n_items: number;
    metrics: Record<string, number>; regressed: boolean; regressions: string[];
}

const fmt = (v: number | undefined) => (v === undefined ? '–' : v.toFixed(3));

export default function EvalDashboard() {
    const qc = useQueryClient();
    const series = useQuery<Series>({
        queryKey: ['eval-metrics'],
        queryFn: () => fetch(`${API}/metrics`).then(r => r.json()),
    });
    const runs = useQuery<{ runs: Run[] }>({
        queryKey: ['eval-runs'],
        queryFn: () => fetch(`${API}/runs`).then(r => r.json()),
    });
    const trigger = useMutation({
        mutationFn: () => fetch(`${API}/run?label=dashboard`, { method: 'POST' }).then(r => r.json()),
        onSuccess: () => {
            qc.invalidateQueries({ queryKey: ['eval-metrics'] });
            qc.invalidateQueries({ queryKey: ['eval-runs'] });
        },
    });

    const latest = runs.data?.runs?.[0];
    const labels = (series.data?.precision ?? []).map(p => new Date(p.ts).toLocaleString());
    const chartData = {
        labels,
        datasets: METRICS.map(m => ({
            label: m,
            data: (series.data?.[m] ?? []).map(p => p.value),
            borderColor: COLORS[m],
            backgroundColor: COLORS[m],
            tension: 0.25,
        })),
    };

    return (
        <div className="eval-dashboard">
            <div className="eval-header">
                <h2>SQL Generation Evaluation</h2>
                <button className="eval-run-btn" onClick={() => trigger.mutate()} disabled={trigger.isPending}>
                    {trigger.isPending ? 'Running…' : 'Run evaluation'}
                </button>
            </div>

            {latest?.regressed && (
                <div className="eval-banner">
                    Regression in latest run: {latest.regressions.join(', ')}
                </div>
            )}

            <div className="eval-cards">
                {METRICS.map(m => (
                    <div className="eval-card" key={m} style={{ borderTopColor: COLORS[m] }}>
                        <span className="eval-card-label">{m}</span>
                        <span className="eval-card-value">{fmt(latest?.metrics?.[m])}</span>
                    </div>
                ))}
            </div>

            <div className="eval-chart">
                {labels.length === 0 ? (
                    <p className="eval-empty">No runs yet. Click “Run evaluation”.</p>
                ) : (
                    <Line data={chartData} options={{
                        responsive: true,
                        scales: { y: { min: 0, max: 1 } },
                        plugins: { legend: { position: 'bottom' } },
                    }} />
                )}
            </div>

            <table className="eval-table">
                <thead>
                    <tr><th>Run</th><th>Started</th><th>Items</th><th>P</th><th>R</th><th>Faith.</th><th>Relev.</th><th>Status</th></tr>
                </thead>
                <tbody>
                    {(runs.data?.runs ?? []).map(r => (
                        <tr key={r.run_id} className={r.regressed ? 'regressed' : ''}>
                            <td>{r.label ?? r.run_id.slice(0, 8)}</td>
                            <td>{new Date(r.started_at).toLocaleString()}</td>
                            <td>{r.n_items}</td>
                            <td>{fmt(r.metrics.precision)}</td>
                            <td>{fmt(r.metrics.recall)}</td>
                            <td>{fmt(r.metrics.faithfulness)}</td>
                            <td>{fmt(r.metrics.relevancy)}</td>
                            <td>{r.regressed ? `Regressed (${r.regressions.join(', ')})` : 'OK'}</td>
                        </tr>
                    ))}
                </tbody>
            </table>
        </div>
    );
}
