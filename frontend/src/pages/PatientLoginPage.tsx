import { useState } from "react";
import type { FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";

import { useAuth } from "../hooks/useAuth";
import { extractErrorMessage } from "../utils/errors";

export default function PatientLoginPage(): JSX.Element {
  const { patientLogin } = useAuth();
  const navigate = useNavigate();

  const [email, setEmail] = useState<string>("");
  const [password, setPassword] = useState<string>("");
  const [error, setError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState<boolean>(false);

  const handleSubmit = async (event: FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault();
    setError(null);
    setIsSubmitting(true);
    try {
      await patientLogin(email, password);
      // Same guessed destination as staff login — see LoginPage.tsx note.
      navigate("/", { replace: true });
    } catch (submitError) {
      setError(extractErrorMessage(submitError, "Login failed. Please check your credentials."));
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <main className="auth-page">
      <form className="auth-form" onSubmit={(event) => void handleSubmit(event)}>
        <h1>Patient Login</h1>

        {error !== null && (
          <p className="auth-error" role="alert">
            {error}
          </p>
        )}

        <label className="auth-label" htmlFor="patient-login-email">
          Email
        </label>
        <input
          id="patient-login-email"
          className="auth-input"
          type="email"
          autoComplete="email"
          value={email}
          onChange={(event) => setEmail(event.target.value)}
          required
        />

        <label className="auth-label" htmlFor="patient-login-password">
          Password
        </label>
        <input
          id="patient-login-password"
          className="auth-input"
          type="password"
          autoComplete="current-password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          required
        />

        <button className="auth-submit" type="submit" disabled={isSubmitting}>
          {isSubmitting ? "Signing in..." : "Sign in"}
        </button>

        <p className="auth-footnote">
          New patient? <Link to="/register">Register here</Link>
        </p>
        <p className="auth-footnote">
          Staff member? <Link to="/login">Log in here</Link>
        </p>
      </form>
    </main>
  );
}
