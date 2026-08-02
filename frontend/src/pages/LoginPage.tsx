import { useState } from "react";
import type { FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";

import { useAuth } from "../hooks/useAuth";
import { extractErrorMessage } from "../utils/errors";

export default function LoginPage(): JSX.Element {
  const { staffLogin } = useAuth();
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
      await staffLogin(email, password);
      // No specific post-login destination was given in the contract; the
      // app only has a placeholder "/" route today, so that's the target.
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
        <h1>Staff Login</h1>

        {error !== null && (
          <p className="auth-error" role="alert">
            {error}
          </p>
        )}

        <label className="auth-label" htmlFor="login-email">
          Email
        </label>
        <input
          id="login-email"
          className="auth-input"
          type="email"
          autoComplete="email"
          value={email}
          onChange={(event) => setEmail(event.target.value)}
          required
        />

        <label className="auth-label" htmlFor="login-password">
          Password
        </label>
        <input
          id="login-password"
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
          Patient? <Link to="/patient/login">Log in here</Link>
        </p>
      </form>
    </main>
  );
}
