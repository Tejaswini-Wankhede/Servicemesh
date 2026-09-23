import React, { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../lib/api.js'
import { ErrorBox } from '../components/ui.jsx'

const ISSUE_GROUPS = [
  { label: 'Power & battery', values: ['BATTERY_FAILURE', 'BATTERY_SWELLING', 'BATTERY_DRAIN', 'NO_POWER'] },
  { label: 'Screen & display', values: ['SCREEN_DAMAGE', 'SCREEN_FLICKER', 'DISPLAY_FAILURE'] },
  { label: 'Performance & cooling', values: ['OVERHEATING', 'FAN_NOISE', 'PERFORMANCE'] },
  { label: 'Inputs & storage', values: ['KEYBOARD_FAILURE', 'STORAGE_FAILURE'] },
  { label: 'Connectivity & software', values: ['SOFTWARE_OS', 'NETWORK_CONNECTIVITY'] },
  { label: 'Other hardware', values: ['CAMERA_FAILURE', 'AUDIO_FAILURE', 'PHYSICAL_DAMAGE', 'OTHER'] },
]

const ISSUE_LABELS = {
  BATTERY_FAILURE: 'Battery not charging or failing', BATTERY_SWELLING: 'Swollen battery',
  BATTERY_DRAIN: 'Battery drains quickly', NO_POWER: 'Device will not turn on',
  SCREEN_DAMAGE: 'Cracked or damaged screen', SCREEN_FLICKER: 'Screen flickering',
  DISPLAY_FAILURE: 'No display / black screen', KEYBOARD_FAILURE: 'Keyboard or buttons',
  STORAGE_FAILURE: 'Storage or drive problem', OVERHEATING: 'Overheating',
  FAN_NOISE: 'Unusual fan noise', PERFORMANCE: 'Slow performance',
  SOFTWARE_OS: 'Operating system or software', NETWORK_CONNECTIVITY: 'Wi-Fi or connectivity',
  CAMERA_FAILURE: 'Camera problem', AUDIO_FAILURE: 'Audio or microphone',
  PHYSICAL_DAMAGE: 'Other physical damage', OTHER: 'Something else',
}

const URGENCY_HELP = {
  LOW: 'A minor issue; the device is still usable.',
  NORMAL: 'Most repair requests. The device is usable with a workaround.',
  HIGH: 'The device is hard to use or work is being disrupted.',
  URGENT: 'Safety risk, such as smoke, heat or a swollen battery. Stop using it and unplug it.',
}
const PRESETS = [
  { label: 'Laptop overheating', issue_type: 'OVERHEATING', issue_description: 'My laptop gets very hot and shuts down after about 20 minutes.', urgency: 'HIGH' },
  { label: 'Phone will not turn on', issue_type: 'NO_POWER', issue_description: 'My phone will not turn on even after charging it overnight.', urgency: 'NORMAL' },
  { label: 'Cracked screen', issue_type: 'SCREEN_DAMAGE', issue_description: 'The screen is cracked and touch input is not working correctly.', urgency: 'NORMAL' },
]

export default function NewRequest() {
  const [mode, setMode] = useState('describe')
  const [form, setForm] = useState({
    order_ref: '', serial_number: '',
    issue_type: 'BATTERY_FAILURE',
    issue_description: '',
    urgency: 'NORMAL', requested_component_sku: '',
  })
  const [text, setText] = useState(
    ''
  )
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [success, setSuccess] = useState(null)
  const navigate = useNavigate()

  function set(k, v) { setForm((f) => ({ ...f, [k]: v })) }
  function applyPreset(preset) {
    setMode('structured')
    setForm((f) => ({ ...f, ...preset }))
  }

  async function submit(e) {
    e.preventDefault()
    setBusy(true); setError(null)
    try {
      const payload = mode === 'structured' ? { ...form } : {
        text,
        ...(form.order_ref ? { order_ref: form.order_ref } : {}),
        ...(form.serial_number ? { serial_number: form.serial_number } : {}),
      }
      if (mode === 'structured' && !payload.requested_component_sku) delete payload.requested_component_sku
      const txn = mode === 'structured'
        ? await api.createTransaction(payload)
        : await api.createFromText(payload)
      setSuccess('Request received. We are coordinating the right repair partners now.')
      window.setTimeout(() => navigate(`/transactions/${txn.id}`), 650)
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <div className="page-head">
        <h1>New service request</h1>
        <p>Get your electronics repair moving with one intake for diagnosis, warranty, parts and an authorised service centre.</p>
      </div>

      <div className="card" style={{ maxWidth: 640 }}>
        <div className="journey" aria-label="Repair journey"><span className="current">1 Describe</span><span>2 We check coverage</span><span>3 Provider repairs</span><span>4 You get updates</span></div>
        <div className="notice" style={{ marginBottom: 16, padding: 12 }}><strong>What happens next</strong><div className="muted">We check your order and warranty context, find compatible parts, then coordinate an authorised repair provider.</div></div>
        <div className="preset-row"><span className="muted">Try a demo preset:</span>{PRESETS.map((p) => <button key={p.label} type="button" className="secondary" onClick={() => applyPreset(p)}>{p.label}</button>)}</div>
        <div className="btn-row" style={{ marginBottom: 16 }}>
          <button type="button" className={mode === 'describe' ? '' : 'secondary'}
                  onClick={() => setMode('describe')}>Describe the problem</button>
          <button type="button" className={mode === 'structured' ? '' : 'secondary'}
                  onClick={() => setMode('structured')}>Enter details</button>
        </div>

        {success && <div className="ok-box" role="status">{success} Opening your repair timeline…</div>}
        <ErrorBox error={error} />

        <form onSubmit={submit}>
          {mode === 'describe' ? (
            <>
              <label><span>Describe the problem</span>
                <textarea value={text} onChange={(e) => setText(e.target.value)}
                          required minLength={10} placeholder="Example: My laptop overheats after 20 minutes and shuts down." />
              </label>
              <div className="grid cols-2">
                <label><span>Order reference <em>(recommended)</em></span>
                  <input value={form.order_ref} placeholder="e.g. ORD-100001 or any reference"
                    onChange={(e) => set('order_ref', e.target.value)} /></label>
                <label><span>Device serial number <em>(recommended)</em></span>
                  <input value={form.serial_number} placeholder="e.g. SN-AX14-0001 or any serial"
                    onChange={(e) => set('serial_number', e.target.value)} /></label>
              </div>
              <p className="muted" style={{ fontSize: 12.5 }}>
                Demo mode accepts any order reference and serial number. Use the
                identifiers from your own receipt when connecting real records.
              </p>
              <p className="muted" style={{ fontSize: 12.5 }}>
                The description is parsed into a suggested issue type. That suggestion
                is advisory: warranty, coverage, compatibility and provider
                authorization are all decided by the deterministic engines against
                each organization&apos;s own records. If the text is unclear you will be
                asked to pick the issue type yourself rather than have it guessed.
              </p>
            </>
          ) : (
            <>
              <div className="grid cols-2">
              <label><span>Order reference</span>
                <input value={form.order_ref} required placeholder="e.g. ORD-100001 or any reference"
                       onChange={(e) => set('order_ref', e.target.value)} /></label>
              <label><span>Device serial number</span>
                <input value={form.serial_number} required placeholder="e.g. SN-AX14-0001 or any serial"
                       onChange={(e) => set('serial_number', e.target.value)} /></label>
              </div>
              <label><span>What best describes the issue?</span>
                <select value={form.issue_type}
                        onChange={(e) => set('issue_type', e.target.value)}>
                  {ISSUE_GROUPS.map((group) => <optgroup key={group.label} label={group.label}>
                    {group.values.map((i) => <option key={i} value={i}>{ISSUE_LABELS[i]}</option>)}
                  </optgroup>)}
                </select></label>
              <label><span>Description</span>
                <textarea value={form.issue_description} required minLength={5}
                          onChange={(e) => set('issue_description', e.target.value)} /></label>
              <label><span>How urgent is this?</span>
                <select value={form.urgency} onChange={(e) => set('urgency', e.target.value)}>
                  {['LOW', 'NORMAL', 'HIGH', 'URGENT'].map((u) => <option key={u} value={u}>{u[0] + u.slice(1).toLowerCase()}</option>)}
                </select>
                <small className="muted">{URGENCY_HELP[form.urgency]}</small></label>
              {(form.urgency === 'URGENT' || form.issue_type === 'BATTERY_SWELLING') &&
                <div className="safety-warning" role="alert"><strong>Safety first</strong><div>Stop using the device, unplug it and keep it away from flammable materials. Do not attempt to open or charge a swollen battery.</div></div>}
              <label><span>Preferred part SKU (optional)</span>
                <input value={form.requested_component_sku} placeholder="e.g. BAT-GENERIC-X"
                       onChange={(e) => set('requested_component_sku', e.target.value)} />
                <small className="muted">
                  A nominated part is checked by the compatibility engine and replaced
                  with a compatible alternative if it is rejected.
                </small></label>
            </>
          )}
          <button type="submit" disabled={busy}>
            {busy ? 'Coordinating repair…' : 'Submit repair request'}
          </button>
        </form>
      </div>
    </>
  )
}
