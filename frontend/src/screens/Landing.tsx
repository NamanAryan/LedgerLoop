/** Screen 1 — entry and mode picker. */

const STEPS = [
  {
    title: 'Ingest two streams',
    body: 'Gateway records on one side, ledger entries on the other.',
  },
  {
    title: 'Match across 5 layers',
    body: 'Exact, time drift, amount drift, duplicate, then a sweep for the rest.',
  },
  {
    title: 'Review exceptions',
    body: 'Every break, with the reason it broke and room to resolve it.',
  },
]

export function Landing({
  onPickTestData,
  onPickUpload,
}: {
  onPickTestData: () => void
  onPickUpload: () => void
}) {
  return (
    <div className="landing">
      <header className="landing-head">
        <h1 className="landing-title">LedgerLoop</h1>
        <p className="landing-sub">
          Reconcile payment gateway and ledger records, flag every mismatch.
        </p>
      </header>

      <div className="choices">
        <button type="button" className="choice" onClick={onPickTestData}>
          <h2 className="choice-title">Try with test data</h2>
          <p className="choice-body">
            Generate transaction pairs in your browser with defects injected at rates you
            set. The run reports what it injected, so you can check the engine's counts
            against it.
          </p>
          <span className="choice-cta">Configure a run →</span>
        </button>

        <button type="button" className="choice" onClick={onPickUpload}>
          <h2 className="choice-title">Upload your data</h2>
          <p className="choice-body">
            Bring a gateway CSV and a ledger CSV. Map your column names to the five
            fields the engine needs — headers never line up on their own.
          </p>
          <span className="choice-cta">Choose files →</span>
        </button>
      </div>

      <div className="howto">
        {STEPS.map((step, index) => (
          <div className="howto-step" key={step.title}>
            <span className="howto-index">{String(index + 1).padStart(2, '0')}</span>
            <span className="howto-text">
              <strong>{step.title}</strong>
              <span>{step.body}</span>
            </span>
          </div>
        ))}
      </div>

      <p
        className="field-hint"
        style={{ marginTop: 'calc(var(--u) * 8)', textAlign: 'center' }}
      >
        Everything runs in this tab. No file you choose is uploaded anywhere.
      </p>
    </div>
  )
}
