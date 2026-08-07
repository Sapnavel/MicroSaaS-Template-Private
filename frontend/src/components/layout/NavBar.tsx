import { Link, useNavigate } from "react-router-dom";

import { useAuth } from "../../hooks/useAuth";

/**
 * Persistent top bar rendered by `ProtectedRoute` around every authenticated
 * page (see that component) -- gives every module page a way back to the
 * module directory (`/`, `App.tsx`'s `HomePage`) and a logout control
 * without relying on the browser's back button.
 */
export default function NavBar(): JSX.Element {
  const { user, logout } = useAuth();
  const navigate = useNavigate();

  const handleLogout = async (): Promise<void> => {
    await logout();
    navigate("/login");
  };

  return (
    <header className="navbar">
      <Link to="/" className="navbar-brand">
        HMS
      </Link>
      {user !== null && (
        <div className="navbar-user">
          <span className="navbar-user-name">{user.fullName}</span>
          <span className="badge badge-primary navbar-role-badge">{user.role.replace("_", " ")}</span>
          <button className="button-secondary" type="button" onClick={() => void handleLogout()}>
            Log out
          </button>
        </div>
      )}
    </header>
  );
}
