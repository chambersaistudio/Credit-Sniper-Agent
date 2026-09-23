import { useEffect, useState } from 'react'
import { api } from '../api'
import { ErrorBox, Loading, PageHeader, humanize, useAsync } from '../components/ui'

const FIELDS = [
  ['full_name', 'Full legal name', { autoComplete: 'name' }],
  ['email', 'Email', { type: 'email', autoComplete: 'email', inputMode: 'email' }],
  ['address', 'Street address', { autoComplete: 'street-address' }],
  ['city', 'City', { autoComplete: 'address-level2' }],
  ['state', 'State', { autoComplete: 'address-level1', maxLength: 2 }],
  ['zip_code', 'ZIP code', { autoComplete: 'postal-code', inputMode: 'numeric', maxLength: 10 }],
  ['phone', 'Phone', { type: 'tel', autoComplete: 'tel' }],
  ['date_of_birth', 'Date of birth', { placeholder: 'MM/DD/YYYY', autoComplete: 'bday' }],
  ['ssn_last_four', 'Last 4 of SSN', { inputMode: 'numeric', maxLength: 4, pattern: '\\d{4}' }],
]

export default function Profile() {
  const { data, error, loading, reload } = useAsync(() => api.getMe(), [])
  const [form, setForm] = useState(null)
  const [saving, setSaving] = useState(false)
  const [status, setStatus] = useState(null)

  useEffect(() => {
    if (data) setForm(Object.fromEntries(FIELDS.map(([k]) => [k, data[k] || ''])))
  }, [data])

  const save = async e => {
    e.preventDefault()
    setSaving(true); setStatus(null)
    try {
      const updated = await api.updateMe(Object.fromEntries(Object.entries(form).filter(([, v]) => v !== '')))
      setStatus({ ok: true, missing: updated.missing_for_correspondence })
    } catch (err) {
      setStatus({ ok: false, message: err.message })
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="content">
      <PageHeader title="Profile" subtitle="Used only to identify you in dispute letters" />
      {loading && !form && <Loading />}
      {error && <ErrorBox error={error} onRetry={reload} />}
      {form && (
        <form className="stack" onSubmit={save}>
          {data.missing_for_correspondence.length > 0 && (
            <div className="alert alert-warn small">
              Bureaus need your {data.missing_for_correspondence.map(humanize).join(', ')} to find your file. Packages can't be approved without them.
            </div>
          )}
          {FIELDS.map(([key, label, props]) => (
            <div className="field" key={key}>
              <label htmlFor={key}>{label}</label>
              <input id={key} value={form[key]} onChange={e => setForm({ ...form, [key]: e.target.value })} {...props} />
              {key === 'ssn_last_four' && <span className="hint">Only the last four digits are ever stored.</span>}
            </div>
          ))}
          {status?.ok && <div className="alert alert-ok">Saved.{status.missing.length ? ` Still needed: ${status.missing.map(humanize).join(', ')}.` : ''}</div>}
          {status && !status.ok && <div className="alert alert-error">{status.message}</div>}
          <button className="btn btn-primary btn-block" disabled={saving}>{saving ? 'Saving…' : 'Save profile'}</button>
        </form>
      )}
    </div>
  )
}
