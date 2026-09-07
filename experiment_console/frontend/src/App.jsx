import { useState } from 'react'
import FaultInjection from './pages/FaultInjection.jsx'
import Evaluation from './pages/Evaluation.jsx'

export default function App() {
  const [tab, setTab] = useState('inject')

  return (
    <div className="app">
      <header className="app-header">
        <div className="header-inner">
          <div className="header-brand">
            <span className="brand-icon">🧪</span>
            <div>
              <h1 className="brand-title">Experiment Console</h1>
              <p className="brand-sub">Fault Injection &amp; Quantitative Evaluation</p>
            </div>
          </div>
          <nav className="tab-nav">
            <button
              className={`tab-btn ${tab === 'inject' ? 'active' : ''}`}
              onClick={() => setTab('inject')}
            >
              ⚡ Fault Injection
            </button>
            <button
              className={`tab-btn ${tab === 'eval' ? 'active' : ''}`}
              onClick={() => setTab('eval')}
            >
              📊 Evaluation
            </button>
          </nav>
        </div>
      </header>

      <main className="app-main">
        {tab === 'inject' && <FaultInjection onGoEval={() => setTab('eval')} />}
        {tab === 'eval'   && <Evaluation />}
      </main>

      <footer className="app-footer">
        <span>Distributed Log Analysis &amp; Self-Healing System — Research Prototype</span>
        <span>
          Dashboard:{' '}
          <a href="http://localhost:3000" target="_blank" rel="noreferrer">
            localhost:3000
          </a>
        </span>
      </footer>
    </div>
  )
}
