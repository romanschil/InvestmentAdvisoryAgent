import React, {useEffect, useState} from 'react'

const API = (path, opts = {}) => fetch('/api' + path, opts).then(r => r.json())

export default function App(){
  const [state, setState] = useState(null)
  const [loading, setLoading] = useState(false)
  const [output, setOutput] = useState('')

  const load = async () => {
    setLoading(true)
    const res = await API('/state')
    setState(res.state)
    setLoading(false)
  }

  useEffect(()=>{ load() }, [])

  const runAgent = async () =>{
    setLoading(true)
    const res = await fetch('/api/run-agent', {method: 'POST'})
    const body = await res.json()
    setOutput(body.output || JSON.stringify(body))
    await load()
    setLoading(false)
  }

  const restart = async () =>{
    if(!confirm('Portfolio sofort zurücksetzen und neue Picks kaufen?')) return
    setLoading(true)
    const res = await fetch('/api/restart', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({auto_buy:true})})
    const body = await res.json()
    setState(body.state)
    setLoading(false)
  }

  const confirmAction = async (action_id, type) =>{
    if(!confirm('Aktion jetzt ausführen?')) return
    setLoading(true)
    const res = await fetch('/api/confirm-action', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({action_id, type})})
    const body = await res.json()
    await load()
    setLoading(false)
    alert('Aktion ausgeführt')
  }

  if(loading) return <div style={{padding:20}}>Loading...</div>
  if(!state) return <div style={{padding:20}}>Keine Daten</div>

  const total = state.cash_chf + (state.positions || []).reduce((s,p)=> s + (p.current_value_chf||0), 0)

  return (
    <div style={{fontFamily:'Arial,Helvetica,sans-serif',padding:20,maxWidth:1000,margin:'0 auto'}}>
      <h1>Investment Advisory — WebUI</h1>
      <div style={{display:'flex',gap:12}}>
        <div style={{flex:1,background:'#f3f4f6',padding:12,borderRadius:8}}>
          <div>Portfolio-Wert</div>
          <div style={{fontSize:20,fontWeight:700}}>{total.toFixed(2)} CHF</div>
          <div>Cash: {state.cash_chf.toFixed(2)} CHF</div>
        </div>
        <div style={{width:240,background:'#eef2ff',padding:12,borderRadius:8}}>
          <button onClick={runAgent} style={{width:'100%',padding:8}}>Run Agent now</button>
          <button onClick={restart} style={{width:'100%',padding:8,marginTop:8,background:'#ef4444',color:'#fff'}}>Restart Portfolio</button>
        </div>
      </div>

      <h2 style={{marginTop:20}}>Pending Actions</h2>
      {state.pending_actions && state.pending_actions.length ? (
        <div>
          {state.pending_actions.map(a=> (
            <div key={a.id} style={{padding:8,background:'#fff',marginBottom:8,borderRadius:6,display:'flex',justifyContent:'space-between'}}>
              <div>
                <div><strong>{a.name}</strong> ({a.ticker}) — {a.type.toUpperCase()}</div>
                <div style={{color:'#6b7280'}}>{a.rationale || ''}</div>
              </div>
              <div>
                <button onClick={()=>confirmAction(a.id,a.type)}>Execute now</button>
              </div>
            </div>
          ))}
        </div>
      ) : <div>Keine offenen Vorschläge</div>}

      <h2 style={{marginTop:20}}>Positions</h2>
      {state.positions && state.positions.length ? (
        <table style={{width:'100%',borderCollapse:'collapse'}}>
          <thead><tr><th>Name</th><th>Ticker</th><th>Shares</th><th>Value CHF</th><th>Profit CHF</th></tr></thead>
          <tbody>
            {state.positions.map(p=> (
              <tr key={p.ticker}><td>{p.name}</td><td>{p.ticker}</td><td>{p.shares}</td><td>{p.current_value_chf||0}</td><td>{p.profit_chf||0}</td></tr>
            ))}
          </tbody>
        </table>
      ) : <div>Keine Positionen</div>}

      <h2 style={{marginTop:20}}>Raw state</h2>
      <pre style={{background:'#0f172a',color:'#fff',padding:12,borderRadius:6,maxHeight:300,overflow:'auto'}}>{JSON.stringify(state,null,2)}</pre>

      <h2 style={{marginTop:20}}>Agent output</h2>
      <pre style={{background:'#111827',color:'#d1fae5',padding:12,borderRadius:6,maxHeight:200,overflow:'auto'}}>{output}</pre>
    </div>
  )
}
