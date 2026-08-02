import { useState } from "react";
import type { FormEvent } from "react";
import { Link } from "react-router-dom";

import { useAuth } from "../hooks/useAuth";
import { extractErrorMessage } from "../utils/errors";

export default function RegisterPage(): JSX.Element {
  const { register } = useAuth();

  const [email, setEmail] = useState<string>("");
  const [password, setPassword] = useState<string>("");
  const [fullName, setFullName] = useState<string>("");
  const [error, setError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState<boolean>(false);
  const [isRegistered, setIsRegistered] = useState<boolean>(false);

  const handleSubmit = async (event: FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault();
    setError(null);
    setIsSubmitting(true);
    try {
      await register(email, password, fullName);
      // POST /auth/register returns the created user but no tokens, so we
      // cannot auto-login here (unspecified by the contract). Send the
      // patient to the patient login page instead of guessing an
      // auto-login flow the backend doesn't support.
      setIsRegistered(true);
    } catch (submitError) {
      setError(extractErrorMessage(submitError, "Registration failed. Please try again."));
    } finally {
      setIsSubmitting(false);
    }
  };

  if (isRegistered) {
    return (
      <main className="auth-page">
        <div className="auth-form">
          <h1>Registration successful</h1>
          <p>You can now log in with your new account.</p>
          <p className="auth-footnote">
            <Link to="/patient/login">Go to patient login</Link>
          </p>
        </div>
      </main>
    );
  }

  return (
    <main className="auth-page">
      <form className="auth-form" onSubmit={(event) => void handleSubmit(event)}>
        <h1>Patient Registration</h1>

        {error !== null && (
          <p className="auth-error" role="alert">
            {error}
          </p>
        )}

        <label className="auth-label" htmlFor="register-full-name">
          Full name
        </label>
        <input
          id="register-full-name"
          className="auth-input"
          type="text"
          autoComplete="name"
          value={fullName}
          onChange={(event) => setFullName(event.target.value)}
          required
        />

        <label className="auth-label" htmlFor="register-email">
          Email
        </label>
        <input
          id="register-email"
          className="auth-input"
          type="email"
          autoComplete="email"
          value={email}
          onChange={(event) => setEmail(event.target.value)}
          required
        />

        <label className="auth-label" htmlFor="register-password">
          Password
        </label>
        <input
          id="register-password"
          className="auth-input"
          type="password"
          autoComplete="new-password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          required
        />

        <button className="auth-submit" type="submit" disabled={isSubmitting}>
          {isSubmitting ? "Registering..." : "Register"}
        </button>

        <p className="auth-footnote">
          Already have an account? <Link to="/patient/login">Log in here</Link>
        </p>
      </form>
    </main>
  );
}
