import { useState, useEffect, useCallback } from "react";

const API = import.meta.env.VITE_API_URL || "http://localhost:8000/api/v1";

const api = {
  get: (path) => fetch(`${API}${path}`).then((r) => r.json()),
  post: (path, body, headers = {}) =>
    fetch(`${API}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...headers },
      body: JSON.stringify(body),
    }).then(async (r) => ({ status: r.status, data: await r.json() })),
  delete: (path) =>
    fetch(`${API}${path}`, { method: "DELETE" }).then(async (r) => ({
      status: r.status,
      data: r.status === 204 ? {} : await r.json(),
    })),
};

const fmt = {
  inr: (paise) =>
    new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", minimumFractionDigits: 2 }).format(paise / 100),
  date: (iso) =>
    new Date(iso).toLocaleString("en-IN", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }),
  shortDate: (iso) => new Date(iso).toLocaleDateString("en-IN", { day: "2-digit", month: "short" }),
};

// ── Badge ────────────────────────────────────────────────────────────────────
function Badge({ status }) {
  const map = {
    pending: "badge-pending", processing: "badge-processing",
    completed: "badge-completed", failed: "badge-failed", cancelled: "badge-cancelled",
  };
  return <span className={`badge ${map[status] || "badge-pending"}`}>{status}</span>;
}

// ── Stat Card ────────────────────────────────────────────────────────────────
function StatCard({ label, value, sub, color }) {
  return (
    <div className="stat-card">
      <span className="stat-label">{label}</span>
      <span className={`stat-value ${color || ""}`}>{value}</span>
      {sub && <span className="stat-sub">{sub}</span>}
    </div>
  );
}

// ── Balance Card ─────────────────────────────────────────────────────────────
function BalanceCard({ balance }) {
  if (!balance) return <div className="card skeleton" style={{ height: 130 }} />;
  return (
    <div className="balance-card">
      <div className="balance-grid">
        <div className="balance-item">
          <span className="balance-label">Available</span>
          <span className="balance-amount available">{fmt.inr(balance.available_paise)}</span>
        </div>
        <div className="balance-divider" />
        <div className="balance-item">
          <span className="balance-label">Held</span>
          <span className="balance-amount held">{fmt.inr(balance.held_paise)}</span>
        </div>
        <div className="balance-divider" />
        <div className="balance-item">
          <span className="balance-label">Total Earned</span>
          <span className="balance-amount total">{fmt.inr(balance.total_credits_paise)}</span>
        </div>
      </div>
    </div>
  );
}

// ── Mini bar chart ───────────────────────────────────────────────────────────
function BarChart({ data }) {
  if (!data || data.length === 0)
    return <p className="empty-state" style={{ padding: "20px 0" }}>No completed payouts in last 30 days</p>;
  const max = Math.max(...data.map((d) => d.total_paise), 1);
  return (
    <div className="chart-wrap">
      {data.map((d, i) => (
        <div key={i} className="chart-col" title={`${fmt.shortDate(d.date)}: ${fmt.inr(d.total_paise)}`}>
          <div className="chart-bar" style={{ height: `${Math.max((d.total_paise / max) * 100, 4)}%` }} />
          {i % Math.ceil(data.length / 6) === 0 && (
            <span className="chart-label">{fmt.shortDate(d.date)}</span>
          )}
        </div>
      ))}
    </div>
  );
}

// ── Analytics Panel ──────────────────────────────────────────────────────────
function AnalyticsPanel({ merchantId }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!merchantId) return;
    setLoading(true);
    api.get(`/merchants/${merchantId}/analytics/`).then((d) => {
      setData(d);
      setLoading(false);
    });
  }, [merchantId]);

  if (loading) return <div className="card skeleton" style={{ height: 220 }} />;
  if (!data) return null;

  const byStatus = data.by_status || {};
  return (
    <div className="card">
      <h3 className="section-title">Analytics</h3>
      <div className="stats-row">
        <StatCard label="Total Payouts" value={data.total_payouts} color="" />
        <StatCard label="Success Rate" value={`${data.success_rate_pct}%`} color="green" />
        <StatCard label="Avg Payout" value={fmt.inr(data.average_payout_paise)} color="" />
        <StatCard label="Volume (completed)" value={fmt.inr(data.completed_volume_paise)} color="green" />
      </div>
      <div className="status-breakdown">
        {Object.entries(byStatus).map(([status, info]) => (
          <div key={status} className="breakdown-item">
            <Badge status={status} />
            <span className="breakdown-count">{info.count} &nbsp;·&nbsp; {fmt.inr(info.total_paise)}</span>
          </div>
        ))}
      </div>
      <div style={{ marginTop: 16 }}>
        <p className="chart-title">Daily Volume — Last 30 Days (completed)</p>
        <BarChart data={data.daily_volume} />
      </div>
    </div>
  );
}

// ── Payout Form ──────────────────────────────────────────────────────────────
function PayoutForm({ merchant, bankAccounts, onSuccess }) {
  const [amount, setAmount] = useState("");
  const [bankId, setBankId] = useState(bankAccounts[0]?.id || "");
  const [note, setNote] = useState("");
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState(null);

  const handleSubmit = async () => {
    const paise = Math.round(parseFloat(amount) * 100);
    if (!paise || paise < 1000) return setResult({ error: "Minimum payout is ₹10" });
    setLoading(true); setResult(null);
    const idempotencyKey = crypto.randomUUID();
    const { status, data } = await api.post(
      "/payouts/",
      { merchant_id: merchant.id, amount_paise: paise, bank_account_id: bankId, note },
      { "Idempotency-Key": idempotencyKey }
    );
    setLoading(false);
    if (status === 201) {
      setResult({ success: true, payout: data });
      setAmount(""); setNote("");
      onSuccess();
    } else {
      setResult({ error: data.error || "Payout failed", detail: data });
    }
  };

  return (
    <div className="card">
      <h3 className="section-title">Request Payout</h3>
      <div className="form-group">
        <label className="form-label">Amount (₹)</label>
        <div className="input-wrap">
          <span className="input-prefix">₹</span>
          <input className="form-input" type="number" min="10" step="0.01" placeholder="0.00"
            value={amount} onChange={(e) => setAmount(e.target.value)} />
        </div>
      </div>
      <div className="form-group">
        <label className="form-label">Bank Account</label>
        <select className="form-select" value={bankId} onChange={(e) => setBankId(e.target.value)}>
          {bankAccounts.map((b) => (
            <option key={b.id} value={b.id}>
              {b.account_holder_name} — ••••{b.account_number.slice(-4)} ({b.ifsc_code})
              {b.is_primary ? " ★" : ""}
            </option>
          ))}
        </select>
      </div>
      <div className="form-group">
        <label className="form-label">Note / Reference (optional)</label>
        <input className="form-input" style={{ paddingLeft: 12 }} type="text"
          placeholder="Invoice #123, Client name…"
          value={note} onChange={(e) => setNote(e.target.value)} maxLength={255} />
      </div>
      <button className="btn-primary" onClick={handleSubmit} disabled={loading}>
        {loading ? <span className="spinner" /> : null}
        {loading ? "Processing…" : "Withdraw Funds"}
      </button>
      {result?.success && (
        <div className="alert alert-success">✓ Payout created — {fmt.inr(result.payout.amount_paise)} is being processed.</div>
      )}
      {result?.error && (
        <div className="alert alert-error">
          ✗ {result.error}
          {result.detail?.available_paise !== undefined && (
            <span> (Available: {fmt.inr(result.detail.available_paise)})</span>
          )}
        </div>
      )}
    </div>
  );
}

// ── Payout History ────────────────────────────────────────────────────────────
function PayoutHistory({ payouts, merchantId, loading, onRefresh }) {
  const [cancelling, setCancelling] = useState(null);
  const [filterStatus, setFilterStatus] = useState("all");

  const handleCancel = async (payoutId) => {
    if (!window.confirm("Cancel this payout? Funds will be returned to your available balance.")) return;
    setCancelling(payoutId);
    await api.post(`/payouts/${payoutId}/cancel/`, { merchant_id: merchantId });
    setCancelling(null);
    onRefresh();
  };

  const filtered = filterStatus === "all" ? payouts : payouts.filter((p) => p.status === filterStatus);

  if (loading) return <div className="card skeleton" style={{ height: 200 }} />;
  return (
    <div className="card">
      <div className="section-header-row">
        <h3 className="section-title" style={{ marginBottom: 0 }}>Payout History</h3>
        <div className="filter-tabs">
          {["all", "pending", "processing", "completed", "failed", "cancelled"].map((s) => (
            <button key={s} className={`filter-tab ${filterStatus === s ? "active" : ""}`}
              onClick={() => setFilterStatus(s)}>{s}</button>
          ))}
        </div>
      </div>
      <div style={{ height: 12 }} />
      {filtered.length === 0 ? (
        <p className="empty-state">No payouts{filterStatus !== "all" ? ` with status "${filterStatus}"` : ""}</p>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Amount</th><th>Status</th><th>Note</th><th>Attempts</th><th>Date</th><th></th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((p) => (
                <tr key={p.id}>
                  <td className="amount-cell">{fmt.inr(p.amount_paise)}</td>
                  <td><Badge status={p.status} /></td>
                  <td className="note-cell">{p.note || p.failure_reason || "—"}</td>
                  <td className="center">{p.attempt_count}</td>
                  <td className="date-cell">{fmt.date(p.created_at)}</td>
                  <td>
                    {p.status === "pending" && (
                      <button className="btn-cancel"
                        onClick={() => handleCancel(p.id)}
                        disabled={cancelling === p.id}>
                        {cancelling === p.id ? "…" : "Cancel"}
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ── Ledger ────────────────────────────────────────────────────────────────────
function LedgerTable({ entries, merchantId, loading }) {
  const handleExport = () => {
    window.open(`${API}/merchants/${merchantId}/ledger/export/`, "_blank");
  };
  if (loading) return <div className="card skeleton" style={{ height: 200 }} />;
  return (
    <div className="card">
      <div className="section-header-row">
        <h3 className="section-title" style={{ marginBottom: 0 }}>Transaction Ledger</h3>
        <button className="btn-export" onClick={handleExport}>↓ Export CSV</button>
      </div>
      <div style={{ height: 12 }} />
      {entries.length === 0 ? <p className="empty-state">No entries yet</p> : (
        <div className="table-wrap">
          <table className="table">
            <thead><tr><th>Type</th><th>Amount</th><th>Description</th><th>Date</th></tr></thead>
            <tbody>
              {entries.map((e) => (
                <tr key={e.id}>
                  <td><span className={`ledger-type ${e.entry_type}`}>{e.entry_type}</span></td>
                  <td className={`amount-cell ${e.entry_type}`}>
                    {e.entry_type === "credit" ? "+" : "−"}{fmt.inr(e.amount_paise)}
                  </td>
                  <td className="desc-cell">{e.description}</td>
                  <td className="date-cell">{fmt.date(e.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ── Add Bank Account ──────────────────────────────────────────────────────────
function AddBankAccountForm({ merchantId, onAdded }) {
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState({ account_number: "", ifsc_code: "", account_holder_name: "", is_primary: false });
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState(null);

  const handleSubmit = async () => {
    setLoading(true); setResult(null);
    const { status, data } = await api.post(`/merchants/${merchantId}/bank-accounts/`, form);
    setLoading(false);
    if (status === 201) {
      setResult({ success: true });
      setForm({ account_number: "", ifsc_code: "", account_holder_name: "", is_primary: false });
      onAdded();
      setTimeout(() => setOpen(false), 1200);
    } else {
      setResult({ error: data.error });
    }
  };

  if (!open)
    return <button className="btn-outline" onClick={() => setOpen(true)}>+ Add Bank Account</button>;

  return (
    <div className="card" style={{ marginTop: 12, border: "1.5px solid var(--accent)" }}>
      <h3 className="section-title">Add Bank Account</h3>
      {["account_number", "ifsc_code", "account_holder_name"].map((field) => (
        <div className="form-group" key={field}>
          <label className="form-label">{field.replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase())}</label>
          <input className="form-input" style={{ paddingLeft: 12 }} type="text"
            value={form[field]} onChange={(e) => setForm({ ...form, [field]: e.target.value })} />
        </div>
      ))}
      <div className="form-group" style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <input type="checkbox" id="isPrimary" checked={form.is_primary}
          onChange={(e) => setForm({ ...form, is_primary: e.target.checked })} />
        <label htmlFor="isPrimary" className="form-label" style={{ marginBottom: 0 }}>Set as primary account</label>
      </div>
      <div style={{ display: "flex", gap: 8 }}>
        <button className="btn-primary" style={{ flex: 1 }} onClick={handleSubmit} disabled={loading}>
          {loading ? <span className="spinner" /> : null} {loading ? "Saving…" : "Save Account"}
        </button>
        <button className="btn-outline" onClick={() => setOpen(false)}>Cancel</button>
      </div>
      {result?.success && <div className="alert alert-success">✓ Bank account added!</div>}
      {result?.error && <div className="alert alert-error">✗ {result.error}</div>}
    </div>
  );
}

// ── Webhook Manager ───────────────────────────────────────────────────────────
function WebhookManager({ merchantId }) {
  const [endpoints, setEndpoints] = useState([]);
  const [deliveries, setDeliveries] = useState([]);
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState({ url: "", secret: "" });
  const [loading, setLoading] = useState(false);
  const [tab, setTab] = useState("endpoints");

  const load = useCallback(() => {
    api.get(`/merchants/${merchantId}/webhooks/`).then(setEndpoints);
    api.get(`/merchants/${merchantId}/webhook-deliveries/`).then(setDeliveries);
  }, [merchantId]);

  useEffect(() => { load(); }, [load]);

  const handleAdd = async () => {
    setLoading(true);
    const { status } = await api.post(`/merchants/${merchantId}/webhooks/`, {
      ...form,
      events: ["payout.completed", "payout.failed", "payout.cancelled"],
    });
    setLoading(false);
    if (status === 201) { setForm({ url: "", secret: "" }); setOpen(false); load(); }
  };

  const handleDelete = async (id) => {
    if (!window.confirm("Delete this webhook endpoint?")) return;
    await api.delete(`/merchants/${merchantId}/webhooks/${id}/`);
    load();
  };

  return (
    <div className="card">
      <div className="section-header-row">
        <h3 className="section-title" style={{ marginBottom: 0 }}>Webhooks</h3>
        <div style={{ display: "flex", gap: 8 }}>
          <button className={`filter-tab ${tab === "endpoints" ? "active" : ""}`} onClick={() => setTab("endpoints")}>Endpoints</button>
          <button className={`filter-tab ${tab === "deliveries" ? "active" : ""}`} onClick={() => setTab("deliveries")}>Deliveries</button>
        </div>
      </div>
      <div style={{ height: 12 }} />

      {tab === "endpoints" && (
        <>
          {endpoints.length === 0 ? (
            <p className="empty-state">No webhook endpoints configured</p>
          ) : (
            <div className="table-wrap">
              <table className="table">
                <thead><tr><th>URL</th><th>Events</th><th>Secret</th><th>Active</th><th></th></tr></thead>
                <tbody>
                  {endpoints.map((e) => (
                    <tr key={e.id}>
                      <td className="desc-cell" style={{ fontFamily: "monospace", fontSize: 11 }}>{e.url}</td>
                      <td className="desc-cell">{(e.events || []).join(", ")}</td>
                      <td className="center">{e.has_secret ? "🔒 Yes" : "—"}</td>
                      <td className="center">{e.is_active ? "✓" : "✗"}</td>
                      <td><button className="btn-cancel" onClick={() => handleDelete(e.id)}>Delete</button></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {!open ? (
            <button className="btn-outline" style={{ marginTop: 10 }} onClick={() => setOpen(true)}>+ Add Endpoint</button>
          ) : (
            <div style={{ marginTop: 12, display: "flex", flexDirection: "column", gap: 10 }}>
              <input className="form-input" style={{ paddingLeft: 12 }} placeholder="https://your-server.com/webhook"
                value={form.url} onChange={(e) => setForm({ ...form, url: e.target.value })} />
              <input className="form-input" style={{ paddingLeft: 12 }} placeholder="HMAC secret (optional)"
                value={form.secret} onChange={(e) => setForm({ ...form, secret: e.target.value })} />
              <div style={{ display: "flex", gap: 8 }}>
                <button className="btn-primary" style={{ flex: 1 }} onClick={handleAdd} disabled={loading}>
                  {loading ? <span className="spinner" /> : null} Save
                </button>
                <button className="btn-outline" onClick={() => setOpen(false)}>Cancel</button>
              </div>
            </div>
          )}
        </>
      )}

      {tab === "deliveries" && (
        <>
          {deliveries.length === 0 ? <p className="empty-state">No webhook deliveries yet</p> : (
            <div className="table-wrap">
              <table className="table">
                <thead><tr><th>Event</th><th>Status</th><th>HTTP</th><th>Attempts</th><th>Date</th></tr></thead>
                <tbody>
                  {deliveries.map((d) => (
                    <tr key={d.id}>
                      <td style={{ fontFamily: "monospace", fontSize: 11 }}>{d.event}</td>
                      <td><Badge status={d.status} /></td>
                      <td className="center">{d.http_status || "—"}</td>
                      <td className="center">{d.attempt_count}</td>
                      <td className="date-cell">{fmt.date(d.created_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </div>
  );
}

// ── Platform Summary Bar ──────────────────────────────────────────────────────
function PlatformSummary() {
  const [data, setData] = useState(null);
  useEffect(() => { api.get("/summary/").then(setData); }, []);
  if (!data) return null;
  return (
    <div className="platform-bar">
      <span>Platform &nbsp;·&nbsp;</span>
      <span><b>{data.total_merchants}</b> merchants &nbsp;·&nbsp;</span>
      <span><b>{data.completed_payouts}</b> payouts completed &nbsp;·&nbsp;</span>
      <span>Volume: <b>{fmt.inr(data.completed_volume_paise)}</b> &nbsp;·&nbsp;</span>
      <span><b>{data.pending_processing_count}</b> in-flight</span>
    </div>
  );
}

// ── Main App ──────────────────────────────────────────────────────────────────
export default function App() {
  const [merchants, setMerchants] = useState([]);
  const [selectedId, setSelectedId] = useState(null);
  const [merchantData, setMerchantData] = useState(null);
  const [payouts, setPayouts] = useState([]);
  const [ledger, setLedger] = useState([]);
  const [loadingMerchant, setLoadingMerchant] = useState(false);
  const [activeTab, setActiveTab] = useState("overview");

  useEffect(() => {
    api.get("/merchants/").then((data) => {
      setMerchants(data);
      if (data.length > 0) setSelectedId(data[0].id);
    });
  }, []);

  const refresh = useCallback(async () => {
    if (!selectedId) return;
    setLoadingMerchant(true);
    const [mData, pData, lData] = await Promise.all([
      api.get(`/merchants/${selectedId}/`),
      api.get(`/merchants/${selectedId}/payouts/`),
      api.get(`/merchants/${selectedId}/ledger/`),
    ]);
    setMerchantData(mData);
    setPayouts(pData);
    setLedger(lData);
    setLoadingMerchant(false);
  }, [selectedId]);

  useEffect(() => {
    refresh();
    const interval = setInterval(refresh, 5000);
    return () => clearInterval(interval);
  }, [refresh]);

  const selectedMerchant = merchants.find((m) => m.id === selectedId);

  return (
    <div className="app">
      <header className="header">
        <div className="header-inner">
          <div className="logo">
            <span className="logo-mark">P</span>
            <span className="logo-text">Playto <span className="logo-accent">Pay</span></span>
          </div>
          <div className="header-tag">Payout Engine</div>
        </div>
      </header>

      <PlatformSummary />

      <main className="main">
        {/* Merchant tabs */}
        <div className="merchant-bar">
          <span className="merchant-label">Merchant:</span>
          <div className="merchant-tabs">
            {merchants.map((m) => (
              <button key={m.id}
                className={`merchant-tab ${m.id === selectedId ? "active" : ""}`}
                onClick={() => setSelectedId(m.id)}>{m.name}</button>
            ))}
          </div>
        </div>
        {selectedMerchant && <div className="merchant-email">{selectedMerchant.email}</div>}

        <BalanceCard balance={merchantData?.balance} />

        {/* Section nav */}
        <div className="section-nav">
          {["overview", "payouts", "ledger", "analytics", "webhooks", "settings"].map((t) => (
            <button key={t} className={`section-tab ${activeTab === t ? "active" : ""}`}
              onClick={() => setActiveTab(t)}>
              {t.charAt(0).toUpperCase() + t.slice(1)}
            </button>
          ))}
        </div>

        {activeTab === "overview" && (
          <div className="two-col">
            <div>
              {merchantData && (
                <PayoutForm merchant={merchantData}
                  bankAccounts={merchantData.bank_accounts || []}
                  onSuccess={refresh} />
              )}
            </div>
            <div>
              <PayoutHistory payouts={payouts.slice(0, 5)} merchantId={selectedId}
                loading={loadingMerchant && payouts.length === 0} onRefresh={refresh} />
            </div>
          </div>
        )}

        {activeTab === "payouts" && (
          <PayoutHistory payouts={payouts} merchantId={selectedId}
            loading={loadingMerchant && payouts.length === 0} onRefresh={refresh} />
        )}

        {activeTab === "ledger" && (
          <LedgerTable entries={ledger} merchantId={selectedId}
            loading={loadingMerchant && ledger.length === 0} />
        )}

        {activeTab === "analytics" && selectedId && (
          <AnalyticsPanel merchantId={selectedId} />
        )}

        {activeTab === "webhooks" && selectedId && (
          <WebhookManager merchantId={selectedId} />
        )}

        {activeTab === "settings" && merchantData && (
          <div>
            <div className="card">
              <h3 className="section-title">Bank Accounts</h3>
              <div className="table-wrap">
                <table className="table">
                  <thead><tr><th>Holder</th><th>Account</th><th>IFSC</th><th>Primary</th></tr></thead>
                  <tbody>
                    {(merchantData.bank_accounts || []).map((b) => (
                      <tr key={b.id}>
                        <td>{b.account_holder_name}</td>
                        <td className="date-cell">{b.account_number_masked}</td>
                        <td className="date-cell">{b.ifsc_code}</td>
                        <td className="center">{b.is_primary ? "★ Primary" : "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <AddBankAccountForm merchantId={selectedId} onAdded={refresh} />
            </div>
          </div>
        )}

        <footer className="footer">
          <span>Ledger invariant enforced · SELECT FOR UPDATE · UUID idempotency · Celery workers · Webhook delivery</span>
        </footer>
      </main>
    </div>
  );
}
