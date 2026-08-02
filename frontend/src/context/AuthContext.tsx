import { createContext, useCallback, useEffect, useState } from "react";
import type { ReactNode } from "react";
import axios from "axios";

import * as authService from "../services/authService";
import type { RegisteredUser } from "../services/authService";
import type { UserProfile } from "../types";

const ACCESS_TOKEN_KEY = "access_token";
const REFRESH_TOKEN_KEY = "refresh_token";

export interface AuthContextValue {
  user: UserProfile | null;
  isLoading: boolean;
  staffLogin: (email: string, password: string) => Promise<void>;
  patientLogin: (email: string, password: string) => Promise<void>;
  register: (email: string, password: string, fullName: string) => Promise<RegisteredUser>;
  logout: () => Promise<void>;
}

export const AuthContext = createContext<AuthContextValue | undefined>(undefined);

function storeTokens(accessToken: string, refreshToken: string): void {
  localStorage.setItem(ACCESS_TOKEN_KEY, accessToken);
  localStorage.setItem(REFRESH_TOKEN_KEY, refreshToken);
}

function clearTokens(): void {
  localStorage.removeItem(ACCESS_TOKEN_KEY);
  localStorage.removeItem(REFRESH_TOKEN_KEY);
}

interface AuthProviderProps {
  children: ReactNode;
}

export function AuthProvider({ children }: AuthProviderProps): JSX.Element {
  const [user, setUser] = useState<UserProfile | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(true);

  useEffect(() => {
    const hydrate = async (): Promise<void> => {
      const existingToken = localStorage.getItem(ACCESS_TOKEN_KEY);
      if (!existingToken) {
        setIsLoading(false);
        return;
      }
      try {
        const profile = await authService.getMe();
        setUser(profile);
      } catch (error) {
        // Any failure to hydrate (401 or otherwise) means the stored token
        // is no longer usable — clear it and leave the user logged out
        // rather than retrying/looping.
        if (axios.isAxiosError(error) && error.response?.status === 401) {
          clearTokens();
        }
        setUser(null);
      } finally {
        setIsLoading(false);
      }
    };
    void hydrate();
  }, []);

  const staffLogin = useCallback(async (email: string, password: string): Promise<void> => {
    const tokens = await authService.staffLogin(email, password);
    storeTokens(tokens.accessToken, tokens.refreshToken);
    const profile = await authService.getMe();
    setUser(profile);
  }, []);

  const patientLogin = useCallback(async (email: string, password: string): Promise<void> => {
    const tokens = await authService.patientLogin(email, password);
    storeTokens(tokens.accessToken, tokens.refreshToken);
    const profile = await authService.getMe();
    setUser(profile);
  }, []);

  const register = useCallback(
    async (email: string, password: string, fullName: string): Promise<RegisteredUser> => {
      // POST /auth/register returns the created user only — no tokens — so
      // registration does not log the user in. Callers (RegisterPage) send
      // the user to the patient login page afterwards.
      return authService.register(email, password, fullName);
    },
    [],
  );

  const logout = useCallback(async (): Promise<void> => {
    const refreshToken = localStorage.getItem(REFRESH_TOKEN_KEY);
    try {
      if (refreshToken) {
        await authService.logout(refreshToken);
      }
    } catch {
      // Best-effort: the local session is cleared below regardless of
      // whether the network call succeeded (e.g. token already expired).
    } finally {
      clearTokens();
      setUser(null);
    }
  }, []);

  const value: AuthContextValue = { user, isLoading, staffLogin, patientLogin, register, logout };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
