import { useState, useEffect } from 'react'

export default function FaultInjection({ onGoEval }) {
  const [service, setService] = useState('auth-service')
  const [faultType, setFaultType] = useState('ERROR_INJECTION')
  const [intensity, setIntensity] = useState(0.20)
  const [latencyMs, setLatencyMs] = useState(1000)
  const [durationSec, setDurationSec] = useState(60)

  const [activeExps, setActiveExps] = useState([])
  const [history, setHistory] = useState([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  const fetchState = async () => {
    try {
      const [actRes, histRes] = await Promise.all([
        fetch('/api/experiments/active'),
        fetch('/api/experiments')
      ])
      if (actRes.ok) setActiveExps(await actRes.json())
      if (histRes.ok) setHistory(await histRes.json())
    } catch (err) {
      console.error('Fetch error:', err)
    }
  }

  useEffect(() => {
    fetchState()
    const timer = setInterval(fetchState, 3000)
    return () => clearInterval(timer)
  }, [])

  const handleStart = async (e) => {
    e.preventDefault()
    setLoading(true)
    setError(null)
    try {
      const res = await fetch('/api/experiments/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          service,
          fault_type: faultType,
          intensity: (faultType === 'ERROR_INJECTION' || faultType === 'TRAFFIC_SPIKE') ? parseFloat(intensity) : 0,
          latency_ms: faultType === 'LATENCY_SPIKE' ? parseInt(latencyMs, 10) : 0,
          duration_sec: parseInt(durationSec, 10)
        })
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.error || 'Failed to start experiment')
      await fetchState()
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  const handleStop = async (expId) => {
    try {
      const res = await fetch(`/api/experiments/${expId}/stop`, { method: 'POST' })
      if (!res.ok) {
        const data = await res.json()
        throw new Error(data.error || 'Failed to stop experiment')
      }
      await fetchState()
    } catch (err) {
      alert(err.message)
    }
  }

  return (
    <div className="fault-injection-page">
      <div className="guide-box">
        <h3>📖 Experiment Guide — Controlled Fault Injection</h3>
        <p>Inject synthetic faults into microservices to test the predictive failure detection & self-healing framework under controlled conditions.</p>
        <ul>
          <li><b>ERROR_INJECTION:</b> Injects errors/warnings into microservice log stream at specified probability (e.g., 20%).</li>
          <li><b>LATENCY_SPIKE:</b> Adds extra latency (e.g., +1000ms) to log response times.</li>
          <li><b>TRAFFIC_SPIKE:</b> Generates high volume log requests for a controlled period.</li>
          <li><b>SERVICE_FAILURE:</b> Simulates total service failure (100% error rate + high latency).</li>
        </ul>
      </div>

      {error && (
        <div style={{ padding: '0.8rem 1rem', background: 'rgba(239,68,68,0.15)', border: '1px solid #ef4444', color: '#f87171', borderRadius: '0.375rem', marginBottom: '1.5rem' }}>
          ⚠️ {error}
        </div>
      )}

      <div className="grid-2">
        {/* Left: Configuration Form */}
        <div className="card">
          <h2 className="card-title">⚡ Launch Experiment</h2>
          <form onSubmit={handleStart}>
            <div className="form-group">
              <label>Target Service</label>
              <select className="form-control" value={service} onChange={e => setService(e.target.value)}>
                <option value="auth-service">auth-service</option>
                <option value="order-service">order-service</option>
                <option value="payment-service">payment-service</option>
              </select>
            </div>

            <div className="form-group">
              <label>Fault Type</label>
              <select className="form-control" value={faultType} onChange={e => setFaultType(e.target.value)}>
                <option value="ERROR_INJECTION">ERROR_INJECTION</option>
                <option value="LATENCY_SPIKE">LATENCY_SPIKE</option>
                <option value="TRAFFIC_SPIKE">TRAFFIC_SPIKE</option>
                <option value="SERVICE_FAILURE">SERVICE_FAILURE</option>
                <option value="NORMAL">NORMAL (Baseline Run)</option>
              </select>
            </div>

            {faultType === 'ERROR_INJECTION' && (
              <div className="form-group">
                <label>Fault Intensity (Error Rate: {(intensity * 100).toFixed(0)}%)</label>
                <input
                  type="range"
                  min="0.05"
                  max="0.50"
                  step="0.05"
                  className="form-control"
                  value={intensity}
                  onChange={e => setIntensity(e.target.value)}
                />
              </div>
            )}

            {faultType === 'LATENCY_SPIKE' && (
              <div className="form-group">
                <label>Added Latency (ms)</label>
                <select className="form-control" value={latencyMs} onChange={e => setLatencyMs(e.target.value)}>
                  <option value={200}>+200 ms</option>
                  <option value={500}>+500 ms</option>
                  <option value={1000}>+1000 ms</option>
                  <option value={2000}>+2000 ms</option>
                </select>
              </div>
            )}

            <div className="form-group">
              <label>Duration (Seconds)</label>
              <select className="form-control" value={durationSec} onChange={e => setDurationSec(e.target.value)}>
                <option value={40}>30 Seconds</option>
                <option value={70}>60 Seconds (Recommended)</option>
                <option value={120}>120 Seconds</option>
                <option value={180}>180 Seconds</option>
              </select>
            </div>

            <button type="submit" className="btn btn-primary" style={{ width: '100%', marginTop: '1rem' }} disabled={loading}>
              {loading ? 'Launching...' : '🚀 Start Experiment'}
            </button>
          </form>
        </div>

        {/* Right: Active Experiment Status */}
        <div className="card">
          <h2 className="card-title">📡 Active Experiments ({activeExps.length})</h2>
          {activeExps.length === 0 ? (
            <p style={{ color: 'var(--text-muted)', fontSize: '0.9rem', marginTop: '1rem' }}>
              No fault experiments currently running. System operating under normal conditions.
            </p>
          ) : (
            activeExps.map(exp => (
              <div key={exp.experiment_id} style={{ background: 'var(--bg-dark)', padding: '1rem', borderRadius: '0.375rem', marginBottom: '1rem', border: '1px solid var(--border-color)' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '0.5rem' }}>
                  <span className="code-inline">{exp.experiment_id}</span>
                  <span className="badge badge-running">RUNNING</span>
                </div>
                <div style={{ fontSize: '0.85rem', color: 'var(--text-muted)', display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0.5rem', marginBottom: '1rem' }}>
                  <div>Service: <strong style={{ color: '#fff' }}>{exp.service}</strong></div>
                  <div>Fault: <strong style={{ color: '#fff' }}>{exp.fault_type}</strong></div>
                  <div>Intensity: <strong style={{ color: '#fff' }}>{exp.intensity ? `${exp.intensity * 100}%` : exp.latency_ms ? `+${exp.latency_ms}ms` : 'Default'}</strong></div>
                  <div>Duration: <strong style={{ color: '#fff' }}>{exp.duration_sec}s</strong></div>
                </div>
                <button onClick={() => handleStop(exp.experiment_id)} className="btn btn-danger" style={{ width: '100%' }}>
                  🛑 Stop Experiment
                </button>
              </div>
            ))
          )}
        </div>
      </div>

      {/* Experiment History */}
      <div className="card">
        <h2 className="card-title">📜 Experiment History</h2>
        {history.length === 0 ? (
          <p style={{ color: 'var(--text-muted)', fontSize: '0.9rem' }}>No past experiments recorded in database.</p>
        ) : (
          <div style={{ overflowX: 'auto' }}>
            <table className="data-table">
              <thead>
                <tr>
                  <th>Experiment ID</th>
                  <th>Service</th>
                  <th>Fault Type</th>
                  <th>Intensity / Config</th>
                  <th>Duration</th>
                  <th>Start Time</th>
                  <th>Status</th>
                  <th>Action</th>
                </tr>
              </thead>
              <tbody>
                {history.map(exp => (
                  <tr key={exp.experiment_id}>
                    <td className="code-inline">{exp.experiment_id}</td>
                    <td>{exp.service}</td>
                    <td>{exp.fault_type}</td>
                    <td>{exp.intensity ? `${exp.intensity * 100}%` : exp.latency_ms ? `+${exp.latency_ms}ms` : '—'}</td>
                    <td>{exp.duration_sec}s</td>
                    <td>{new Date(exp.start_time).toLocaleTimeString()}</td>
                    <td>
                      <span className={`badge badge-${exp.status.toLowerCase()}`}>
                        {exp.status}
                      </span>
                    </td>
                    <td>
                      <button onClick={onGoEval} className="btn btn-primary" style={{ padding: '0.2rem 0.5rem', fontSize: '0.75rem' }}>
                        📊 Evaluate
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}
