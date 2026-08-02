import { api } from "./api";
import type { Role, TokenPair, UserProfile } from "../types";

/**
 * This file is the only place in the frontend allowed to know about the
 * backend's snake_case wire format. Every exported function here converts
 * to/from the camelCase types in `types/index.ts` at the boundary.
 */

interface RegisterRequestWire {
  email: string;
  password: string;
  full_name: string;
}

interface RegisteredUserWire {
  id: string;
  email: string;
  full_name: string;
  role: Role;
}

interface LoginRequestWire {
  email: string;
  password: string;
}

interface TokenPairWire {
  access_token: string;
  refresh_token: string;
  token_type: string;
}

interface RefreshRequestWire {
  refresh_token: string;
}

interface LogoutRequestWire {
  refresh_token: string;
}

interface UserProfileWire {
  id: string;
  email: string;
  full_name: string;
  role: Role;
  branch_id: string | null;
  hospital_group_id: string | null;
  is_active: boolean;
  is_verified: boolean;
}

/** Response of POST /auth/register — note it does NOT include tokens. */
export interface RegisteredUser {
  id: string;
  email: string;
  fullName: string;
  role: Role;
}

function toTokenPair(wire: TokenPairWire): TokenPair {
  return {
    accessToken: wire.access_token,
    refreshToken: wire.refresh_token,
    tokenType: wire.token_type,
  };
}

function toUserProfile(wire: UserProfileWire): UserProfile {
  return {
    id: wire.id,
    email: wire.email,
    fullName: wire.full_name,
    role: wire.role,
    branchId: wire.branch_id,
    hospitalGroupId: wire.hospital_group_id,
    isActive: wire.is_active,
    isVerified: wire.is_verified,
  };
}

export async function register(
  email: string,
  password: string,
  fullName: string,
): Promise<RegisteredUser> {
  const body: RegisterRequestWire = { email, password, full_name: fullName };
  const { data } = await api.post<RegisteredUserWire>("/auth/register", body);
  return {
    id: data.id,
    email: data.email,
    fullName: data.full_name,
    role: data.role,
  };
}

export async function staffLogin(email: string, password: string): Promise<TokenPair> {
  const body: LoginRequestWire = { email, password };
  const { data } = await api.post<TokenPairWire>("/auth/login", body);
  return toTokenPair(data);
}

export async function patientLogin(email: string, password: string): Promise<TokenPair> {
  const body: LoginRequestWire = { email, password };
  const { data } = await api.post<TokenPairWire>("/auth/patient/login", body);
  return toTokenPair(data);
}

export async function refresh(refreshToken: string): Promise<TokenPair> {
  const body: RefreshRequestWire = { refresh_token: refreshToken };
  const { data } = await api.post<TokenPairWire>("/auth/refresh", body);
  return toTokenPair(data);
}

export async function logout(refreshToken: string): Promise<void> {
  const body: LogoutRequestWire = { refresh_token: refreshToken };
  await api.post<void>("/auth/logout", body);
}

export async function getMe(): Promise<UserProfile> {
  const { data } = await api.get<UserProfileWire>("/auth/me");
  return toUserProfile(data);
}
