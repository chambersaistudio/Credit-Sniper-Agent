import { useState } from 'react'
import { api } from '../api'

export default function UserProfile() {
  const [form, setForm] = useState({
    email: '',
    full_name: '',
    address: '',
    city: '',
    state: '',
    zip_code: '',
    ssn_last_four: '',
    date_of_birth: '',
    phone: '',
  })
  const [saved, setSaved] = useState(false)
  const [userId, setUserId] = useState(localStorage.getItem('userId') || null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  const handleSubmit = async (e) => {
    e.preventDefault()
    setLoading(true)
    setError(null)
    try {
      let result
      if (userId) {
        result = await api.updateUser(userId, form)
      } else {
        result = await api.createUser(form)
        localStorage.setItem('userId', result.id)
        setUserId(result.id)
      }
      setSaved(true)
      setTimeout(() => setSaved(false), 3000)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }

  const set = (key) => (e) => setForm(f => ({ ...f, [key]: e.target.value }))

  return (
    <div style={{ padding: 32, maxWidth: 600 }}>
      <div style={{ marginBottom: 24 }}>
        <h1 style={{ fontSize: 22, fontWeight: 700 }}>My Profile</h1>
        <p style={{ color: '#64748b', marginTop: 4, fontSize: 13 }}>
          Your information is used to personalize dispute letters. Stored locally — never shared.
        </p>
      </div>

      <div className="alert alert-warning" style={{ marginBottom: 20 }}>
        ⚠️ This information will appear in your dispute letters. Ensure accuracy — your name, address, and SSN last 4
        are required by FCRA for bureau identification.
      </div>

      {error && <div className="alert alert-error">{error}</div>}
      {saved && <div className="alert alert-success">✅ Profile saved successfully!</div>}

      <div className="card">
        <form onSubmit={handleSubmit}>
          <h3 style={{ fontSize: 14, fontWeight: 600, marginBottom: 16, color: '#64748b' }}>PERSONAL INFORMATION</h3>

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
            <div className="form-group">
              <label>Full Legal Name *</label>
              <input value={form.full_name} onChange={set('full_name')} placeholder="John Michael Smith" required />
            </div>
            <div className="form-group">
              <label>Email *</label>
              <input type="email" value={form.email} onChange={set('email')} placeholder="john@example.com" required />
            </div>
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
            <div className="form-group">
              <label>Date of Birth</label>
              <input value={form.date_of_birth} onChange={set('date_of_birth')} placeholder="01/15/1985" />
            </div>
            <div className="form-group">
              <label>SSN Last 4 Digits</label>
              <input
                value={form.ssn_last_four}
                onChange={set('ssn_last_four')}
                placeholder="1234"
                maxLength={4}
                pattern="\d{4}"
              />
              <div style={{ fontSize: 11, color: '#64748b', marginTop: 4 }}>
                Required for bureau identification — last 4 only
              </div>
            </div>
          </div>

          <div className="divider" />
          <h3 style={{ fontSize: 14, fontWeight: 600, marginBottom: 16, color: '#64748b' }}>CURRENT ADDRESS</h3>

          <div className="form-group">
            <label>Street Address</label>
            <input value={form.address} onChange={set('address')} placeholder="123 Main Street" />
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: '2fr 1fr 1fr', gap: 16 }}>
            <div className="form-group">
              <label>City</label>
              <input value={form.city} onChange={set('city')} placeholder="Anytown" />
            </div>
            <div className="form-group">
              <label>State</label>
              <input value={form.state} onChange={set('state')} placeholder="CA" maxLength={2} />
            </div>
            <div className="form-group">
              <label>ZIP Code</label>
              <input value={form.zip_code} onChange={set('zip_code')} placeholder="90210" maxLength={10} />
            </div>
          </div>

          <div className="form-group">
            <label>Phone</label>
            <input value={form.phone} onChange={set('phone')} placeholder="(555) 123-4567" />
          </div>

          <div className="divider" />

          <button type="submit" className="btn-primary" style={{ width: '100%', padding: 12 }} disabled={loading}>
            {loading ? 'Saving...' : userId ? 'Update Profile' : 'Save Profile'}
          </button>
        </form>
      </div>

      <div className="card" style={{ marginTop: 20 }}>
        <h3 style={{ fontSize: 14, fontWeight: 600, marginBottom: 12 }}>Why This Info Is Needed</h3>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          {[
            { field: 'Full Name', reason: 'Appears on all dispute letters — must match your credit file exactly' },
            { field: 'Address', reason: 'Required for certified mail return receipt and bureau correspondence' },
            { field: 'SSN Last 4', reason: 'Bureau identity verification requirement per FCRA' },
            { field: 'DOB', reason: 'Secondary identity verification used in letters and bureau portal login' },
          ].map(item => (
            <div key={item.field} style={{ display: 'flex', gap: 12, alignItems: 'flex-start' }}>
              <span style={{ color: '#6366f1', fontWeight: 600, fontSize: 12, flexShrink: 0 }}>{item.field}:</span>
              <span style={{ color: '#94a3b8', fontSize: 13 }}>{item.reason}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
