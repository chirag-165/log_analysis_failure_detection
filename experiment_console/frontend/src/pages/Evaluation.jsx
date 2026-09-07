import { useState, useEffect } from 'react'
import {
  Chart as ChartJS,
  CategoryScale,
  LinearScale,
  PointElement,
  LineElement,
  Title,
  Tooltip,
  Legend,
  Filler
} from 'chart.js'
import { Line } from 'react-chartjs-2'

ChartJS.register(
  CategoryScale,
  LinearScale,
  PointElement,
  LineElement,
  Title,
  Tooltip,
  Legend,
  Filler
)

export default function Evaluation() {
  const [experiments, setExperiments] = useState([])
  const [selectedExpId, setSelectedExpId] = useState('')
  const [evalData, setEvalData] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  useEffect(() => {
    fetch('/api/experiments')
      .then(res => res.json())
      .then(data => {
        setExperiments(data)
        if (data.length > 0) {
          setSelectedExpId(data[0].experiment_id)
        }
      })
      .catch(err => console.error(err))
  }, [])

  useEffect(() => {
    if (!selectedExpId) return
    setLoading(true)
    setError(null)
    fetch(`/api/evaluate/${selectedExpId}`)
      .then(res => {
        if (!res.ok) throw new Error('Evaluation data not available for this experiment')
        return res.json()
      })
      .then(data => setEvalData(data))
      .catch(err => setError(err.message))
      .finally(() => setLoading(false))
  }, [selectedExpId])

  if (!experiments.length) {
    return (
      <div className="card">
        <h2>📊 Quantitative Evaluation</h2>
        <p style={{ color: 'var(--text-muted)', marginTop: '1rem' }}>
          No experiments available to evaluate. Run an experiment first in the ⚡ Fault Injection tab.
        </p>
      </div>
    )
  }

  const { experiment, detection, detection_latency, prediction_stats, healing, baselines, ablation, timeseries } = evalData || {}

  // Chart Data Preparation
  const chartData = timeseries ? {
    labels: timeseries.map(t => new Date(t.timestamp).toLocaleTimeString()),
    datasets: [
      {
        label: 'ML Failure Prob P(Failure)',
        data: timeseries.map(t => t.probability),
        borderColor: '#3b82f6',
        backgroundColor: 'rgba(59, 130, 246, 0.1)',
        fill: true,
        yAxisID: 'yProb',
      },
      {
        label: 'Weighted Error Rate',
        data: timeseries.map(t => t.weighted_error_rate),
        borderColor: '#ef4444',
        borderDash: [5, 5],
        yAxisID: 'yProb',
      },
      {
        label: 'P95 Latency (ms)',
        data: timeseries.map(t => t.p95_latency),
        borderColor: '#f59e0b',
        yAxisID: 'yLat',
      }
    ]
  } : null

  const chartOptions = {
    responsive: true,
    interaction: { mode: 'index', intersect: false },
    scales: {
      yProb: { type: 'linear', display: true, position: 'left', min: 0, max: 1.0, title: { display: true, text: 'Probability / Rate' } },
      yLat: { type: 'linear', display: true, position: 'right', grid: { drawOnChartArea: false }, title: { display: true, text: 'Latency (ms)' } },
    }
  }

  return (
    <div className="evaluation-page">
      {/* Top Selector */}
      <div className="card" style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <div>
          <h2 className="card-title" style={{ margin: 0 }}>📊 Select Experiment for Evaluation</h2>
          <p style={{ fontSize: '0.85rem', color: 'var(--text-muted)', marginTop: '0.2rem' }}>
            Ground Truth is loaded directly from <span className="code-inline">fault_experiments</span> collection.
          </p>
        </div>
        <select
          className="form-control"
          style={{ width: '320px' }}
          value={selectedExpId}
          onChange={e => setSelectedExpId(e.target.value)}
        >
          {experiments.map(exp => (
            <option key={exp.experiment_id} value={exp.experiment_id}>
              {exp.experiment_id} ({exp.service} - {exp.fault_type})
            </option>
          ))}
        </select>
      </div>

      {loading && <div className="card"><p>Loading evaluation metrics...</p></div>}
      {error && <div className="card" style={{ border: '1px solid #ef4444', color: '#f87171' }}><p>⚠️ {error}</p></div>}

      {evalData && !loading && (
        <>
          {/* Summary Cards */}
          <div className="grid-4" style={{ marginBottom: '1.5rem' }}>
            <div className="metric-card">
              <div className="metric-lbl">Precision</div>
              <div className="metric-val">{(detection.precision * 100).toFixed(1)}%</div>
            </div>
            <div className="metric-card">
              <div className="metric-lbl">Recall</div>
              <div className="metric-val">{(detection.recall * 100).toFixed(1)}%</div>
            </div>
            <div className="metric-card">
              <div className="metric-lbl">F1-Score</div>
              <div className="metric-val">{(detection.f1 * 100).toFixed(1)}%</div>
            </div>
            <div className="metric-card">
              <div className="metric-lbl">Detection Latency</div>
              <div className="metric-val">{detection_latency.latency_sec !== null ? `${detection_latency.latency_sec}s` : 'N/A'}</div>
            </div>
            <div className="metric-card">
              <div className="metric-lbl">Recovery Action</div>
              <div className="metric-val" style={{ fontSize: '1rem' }}>{healing.action || 'None'}</div>
            </div>
            <div className="metric-card">
              <div className="metric-lbl">MTTR</div>
              <div className="metric-val">{healing.mttr_sec !== null ? `${healing.mttr_sec}s` : 'N/A'}</div>
            </div>
            <div className="metric-card">
              <div className="metric-lbl">Avg ML Prob</div>
              <div className="metric-val">{prediction_stats.avg !== null ? prediction_stats.avg : 'N/A'}</div>
            </div>
            <div className="metric-card">
              <div className="metric-lbl">False Positive Rate</div>
              <div className="metric-val">{(detection.fpr * 100).toFixed(1)}%</div>
            </div>
          </div>

          {/* Detailed Metric Sections */}
          <div className="grid-2">
            <div className="card">
              <h3 className="card-title">🎯 Confusion Matrix (Failure Detection)</h3>
              <table className="data-table">
                <tbody>
                  <tr>
                    <th>True Positives (TP):</th>
                    <td><strong style={{ color: '#34d399' }}>{detection.tp}</strong></td>
                    <th>False Positives (FP):</th>
                    <td><strong style={{ color: '#f87171' }}>{detection.fp}</strong></td>
                  </tr>
                  <tr>
                    <th>True Negatives (TN):</th>
                    <td><strong style={{ color: '#34d399' }}>{detection.tn}</strong></td>
                    <th>False Negatives (FN):</th>
                    <td><strong style={{ color: '#f87171' }}>{detection.fn}</strong></td>
                  </tr>
                  <tr>
                    <th>Accuracy:</th>
                    <td>{(detection.accuracy * 100).toFixed(1)}%</td>
                    <th>FPR:</th>
                    <td>{(detection.fpr * 100).toFixed(1)}%</td>
                  </tr>
                </tbody>
              </table>
              <p style={{ fontSize: '0.8rem', color: 'var(--text-muted)', marginTop: '0.75rem' }}>
                Decision Rule: Window classified as positive if <span className="code-inline">risk == HIGH</span> or <span className="code-inline">COOLDOWN</span> during injected fault duration.
              </p>
            </div>

            <div className="card">
              <h3 className="card-title">⚙️ Self-Healing Performance (MTTR)</h3>
              <table className="data-table">
                <tbody>
                  <tr>
                    <th>Recovery Triggered:</th>
                    <td>{healing.triggered ? 'Yes' : 'No'}</td>
                  </tr>
                  <tr>
                    <th>Recovery Action:</th>
                    <td><span className="code-inline">{healing.action || 'None'}</span></td>
                  </tr>
                  <tr>
                    <th>Action Status:</th>
                    <td><span className={`badge badge-${(healing.status || '').toLowerCase()}`}>{healing.status || 'N/A'}</span></td>
                  </tr>
                  <tr>
                    <th>MTTR Reference:</th>
                    <td><small>T_success - T_fault_start</small></td>
                  </tr>
                  <tr>
                    <th>MTTR Duration:</th>
                    <td><strong style={{ color: 'var(--primary)' }}>{healing.mttr_sec !== null ? `${healing.mttr_sec} seconds` : 'No recovery'}</strong></td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>

          {/* Timeseries Visualizations */}
          {chartData && (
            <div className="card">
              <h3 className="card-title">📈 ML Probability, Latency & Error Rate Timeline</h3>
              <div style={{ height: '320px' }}>
                <Line data={chartData} options={chartOptions} />
              </div>
            </div>
          )}

          {/* Baseline Comparison Table */}
          <div className="card">
            <h3 className="card-title">⚖️ Baseline Comparison</h3>
            <p style={{ fontSize: '0.85rem', color: 'var(--text-muted)', marginBottom: '1rem' }}>
              Comparison calculated offline using the same experiment window data against production rules.
            </p>
            <div style={{ overflowX: 'auto' }}>
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Method</th>
                    <th>Precision</th>
                    <th>Recall</th>
                    <th>F1-Score</th>
                    <th>FPR</th>
                    <th>Detection Latency</th>
                  </tr>
                </thead>
                <tbody>
                  {baselines.map((b, idx) => (
                    <tr key={idx} style={b.method.includes('Hybrid') ? { fontWeight: 'bold', background: 'rgba(59, 130, 246, 0.1)' } : {}}>
                      <td>{b.method} <br/><small style={{ color: 'var(--text-muted)', fontWeight: 'normal' }}>{b.description}</small></td>
                      <td>{(b.precision * 100).toFixed(1)}%</td>
                      <td>{(b.recall * 100).toFixed(1)}%</td>
                      <td>{(b.f1 * 100).toFixed(1)}%</td>
                      <td>{(b.fpr * 100).toFixed(1)}%</td>
                      <td>{b.latency_sec !== null ? `${b.latency_sec}s` : 'N/A'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          {/* Ablation Analysis */}
          <div className="card">
            <h3 className="card-title">🔬 Ablation & Component Evaluation</h3>
            <div style={{ overflowX: 'auto' }}>
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Configuration</th>
                    <th>Precision</th>
                    <th>Recall</th>
                    <th>F1-Score</th>
                    <th>Detection Latency</th>
                  </tr>
                </thead>
                <tbody>
                  {ablation.map((a, idx) => (
                    <tr key={idx} style={a.config.includes('Full Hybrid') ? { fontWeight: 'bold', background: 'rgba(16, 185, 129, 0.1)' } : {}}>
                      <td>{a.config}</td>
                      <td>{(a.precision * 100).toFixed(1)}%</td>
                      <td>{(a.recall * 100).toFixed(1)}%</td>
                      <td>{(a.f1 * 100).toFixed(1)}%</td>
                      <td>{a.latency_sec !== null ? `${a.latency_sec}s` : 'N/A'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </>
      )}
    </div>
  )
}
