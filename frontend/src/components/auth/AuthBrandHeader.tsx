/**
 * Icon + name + subtitle header shown at the top of every auth card
 * (login/register/patient-login), matching the repeated brand-lockup
 * pattern from the reference design
 * (https://srv1794963.hstgr.cloud/login) -- the same lockup (just smaller)
 * also appears in the persistent NavBar. Kept as one shared component
 * rather than copy-pasted into each auth page so the two never drift.
 */
export default function AuthBrandHeader(): JSX.Element {
  return (
    <div className="brand-lockup brand-lockup--lg">
      <span className="brand-icon" aria-hidden="true">
        H
      </span>
      <span>
        <span className="brand-name">HMS</span>
        <br />
        <span className="brand-subtitle">Hospital Management System</span>
      </span>
    </div>
  );
}
